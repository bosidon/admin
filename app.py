#!/usr/bin/env python3
"""
自媒体视频创作平台 - 主应用
端口: 3010
核心功能: 视频内容生产流水线（编剧→分镜→资产→视频生成）
"""
import os
import time
import json
import sqlite3
import uuid
import shutil
import requests
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, jsonify, request, g, redirect, send_file

app = Flask(__name__)
app.secret_key = os.urandom(24)

# ============================================================
# 路径配置
# ============================================================
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = DATA_DIR / "config"
PROJECTS_DIR = DATA_DIR / "projects"
LIBRARY_DIR = DATA_DIR / "library"

for d in [DATA_DIR, CONFIG_DIR, PROJECTS_DIR, LIBRARY_DIR]:
    d.mkdir(parents=True, exist_ok=True)

DATABASE = os.environ.get('TEST_DATABASE', str(DATA_DIR / "database.db"))

# 外部API配置
ENV_PATH = Path(__file__).parent / ".env" if (Path(__file__).parent / ".env").exists() else Path("/var/www/.env")
# LLM 参数默认值（默认走 DeepSeek 官网）
LLM_DEFAULTS = {
    "llm_base_url": "https://api.deepseek.com/v1/chat/completions",
    "llm_model": "deepseek-chat",
}
from illustrate import stamp_qr
from autogen import (start_generate, get_job, shutdown_instance, adl_status,
                     read_plan, save_plan, PLAN_PROMPT_DEFAULT, rewrite_plan_item,
                     load_plan_prompt, load_plan_prompt_raw, save_plan_prompt, register_jobs, uid_ok,
                     get_comfy_config, resolve_base_url, comfy_object_info, check_comfy_config,
                     load_model_groups, load_model_config, adl_hosts,
                     style_groups, style_label_map, style_types, get_default_style, ASPECT_SIZE)
import jobs as jobstore
import lines as contentlines
import materials as materialstore

# ============================================================
# 工具函数
# ============================================================
def load_env():
    """读取环境变量"""
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    return env

def get_llm_config():
    """LLM 参数：settings 表优先 → 环境变量 → 默认值（默认 DeepSeek 官网）"""
    cfg = dict(LLM_DEFAULTS)
    try:
        rows = get_db().execute(
            "SELECT key, value FROM settings WHERE key IN "
            "('llm_base_url','llm_model','llm_api_key')").fetchall()
        for r in rows:
            if r["value"]:
                cfg[r["key"]] = r["value"]
    except Exception:
        pass
    if not cfg.get("llm_api_key"):
        cfg["llm_api_key"] = load_env().get("DEEPSEEK_API_KEY", "")
    return cfg

def load_json(path, default=None):
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default if default is not None else {}

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def gen_uuid():
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]

# ============================================================
# 数据库
# ============================================================
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db:
        db.close()

def init_db():
    conn = sqlite3.connect(DATABASE)
    conn.executescript('''
        -- 项目表
-- 脚本表
-- 分镜表
-- 资产表
-- 字幕表
-- 音频轨表
-- 渲染任务表
-- 设置表
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key VARCHAR(100) UNIQUE NOT NULL,
            value TEXT,
            category VARCHAR(50)
        );
    ''')
    conn.commit()
    conn.close()

def _ensure_article_owner_cols():
    """文案表补归属列（幂等；只 ADD COLUMN，不动任何旧数据）"""
    try:
        # 注意：CONTENT_DATABASE 在文件更后面才定义，此处必须自算路径
        c = sqlite3.connect(str(DATA_DIR / "content.db"), timeout=10)
        cols = {r[1] for r in c.execute("PRAGMA table_info(articles)").fetchall()}
        for name, ddl in (("owner_id", "INTEGER"), ("owner", "TEXT DEFAULT ''")):
            if name not in cols:
                c.execute("ALTER TABLE articles ADD COLUMN %s %s" % (name, ddl))
        c.commit()
        c.close()
    except Exception as e:
        print("  ⚠️ 文案归属列初始化失败：", str(e)[:120])


if not os.environ.get('TEST_DATABASE'):
    init_db()
    jobstore.init()                       # 任务表（持久化，重启不丢）
    materialstore.init()                  # 素材表：建表 + 补 owner_id/owner 列（可空，不动旧数据）
    _ensure_article_owner_cols()          # 文案表：补 owner_id/owner 列（幂等，不动旧数据）
    _orphans = jobstore.recover_orphans()  # 上次残留的排队/运行中任务 → 被中断
    register_jobs()                        # 注册各任务执行体 + 启动调度器
    if _orphans:
        print("🔧 启动自检：%d 个残留任务已标记为「被中断」" % _orphans)
        # 上次有任务被中断 → 实例可能还开着，自检关机（防漏计费）
        try:
            if jobstore.queued_count("images") == 0 and (adl_status().get("data") or "") == "running":
                print("  实例仍在运行且无排队任务 → 关机：",
                      shutdown_instance().get("msg"))
        except Exception as _e:
            print("  启动自检关机失败：", str(_e)[:120])

# ============================================================
# 内容库(content.db)连接
# ============================================================
CONTENT_DATABASE = str(DATA_DIR / "content.db")

def get_content_db():
    if 'cdb' not in g:
        g.cdb = sqlite3.connect(CONTENT_DATABASE)
        g.cdb.row_factory = sqlite3.Row
        g.cdb.execute("PRAGMA journal_mode=WAL")
        g.cdb.execute("PRAGMA foreign_keys=ON")
    return g.cdb

@app.teardown_appcontext
def close_content_db(exception):
    db = g.pop('cdb', None)
    if db:
        db.close()

# ============================================================
# 当前登录用户（读统一认证 cookie → auth 服务；失败静默返回空）
# ============================================================
AUTH_API = (load_env().get("AUTH_API") or "http://localhost:3050").rstrip("/")


def current_user():
    """{id,email,nickname,role,plan} 或 {}（未登录/认证服务不可用）"""
    if 'cur_user' in g:
        return g.cur_user
    g.cur_user = {}
    tok = request.cookies.get("xianbao_token") or ""
    if not tok:
        return g.cur_user
    try:
        r = requests.post(AUTH_API + "/api/auth/verify", json={"token": tok}, timeout=5)
        d = r.json()
        if d.get("success") and d.get("user"):
            g.cur_user = d["user"]
    except Exception:
        pass
    return g.cur_user


# ============================================================
# 引流计数（文案编号 → 该文案带来的注册数）
#   数据源：auth 服务 users.referred_src（仅首次归因写入，不重复计）
#   60 秒内存缓存 + 失败静默降级 —— 绝不阻塞页面
# ============================================================
_REF_CACHE = {"t": 0.0, "map": {}}


def ref_counts():
    """{'<文案编号>': 注册数}；auth 不可用时返回上次结果（或空 dict）"""
    now = time.time()
    if now - _REF_CACHE["t"] < 60:
        return _REF_CACHE["map"]
    _REF_CACHE["t"] = now      # 先更新时间：失败也退避 60s，避免每次渲染都等超时
    try:
        tok = (load_env().get("INTERNAL_TOKEN") or "").strip()
        if not tok:
            return _REF_CACHE["map"]
        r = requests.get(AUTH_API + "/api/users/internal/referral-sources",
                         headers={"x-internal-token": tok}, timeout=2)
        d = r.json()
        if d.get("success"):
            m = {}
            for row in ((d.get("data") or {}).get("rows") or []):
                k = str(row.get("src") or "").strip()
                if k.isdigit():
                    m[k] = int(row.get("cnt") or 0)
            _REF_CACHE["map"] = m
    except Exception:
        pass
    return _REF_CACHE["map"]


def with_ref_counts(rows):
    """给文案列表注入 ref_count（无引流 → 0）"""
    rc = ref_counts()
    out = []
    for r in rows:
        d = dict(r)
        d["ref_count"] = rc.get(str(d.get("id")), 0)
        out.append(d)
    return out


# ============================================================
# 门禁：全站需登录，且仅 admin（管理员）/ sales（推广员）可用
# 统一在 before_request 收口 —— 所有页面与接口一次生效
# ============================================================
STAFF_ROLES = ("admin", "sales")
_GATE_FREE = ("/favicon.ico",)   # 仅图标放行：门页自包含（组件走 nginx 的 /assets/），
                                 # /static 下的配图同样需登录才可访问


def _gate_page(u):
    """未登录 / 无权限 时返回的极简门页（自带登录组件，登录后自动回站）"""
    if u:
        name = (u.get("nickname") or u.get("email") or "当前账号")
        tip = "当前账号 <b>%s</b> 无权限<br>仅管理员 / 推广员可用" % name
        btn = "退出登录"
        act = ("XianbaoAuth.logout();setTimeout(function(){location.reload();},800);")
    else:
        tip = "请登录后使用仙宝内容平台"
        btn = "登录 / 注册"
        act = ("var t=setInterval(function(){if(window.XianbaoAuth&&XianbaoAuth.isLoggedIn())"
               "{clearInterval(t);location.reload();}},500);XianbaoAuth.showLogin();")
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<title>\u8bf7\u767b\u5f55 - \u4ed9\u5b9d\u5185\u5bb9\u5e73\u53f0</title>'
        '<style>'
        "body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;"
        "background:#1e1b4b;color:#fff;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif}"
        ".box{text-align:center;padding:0 24px}"
        ".box h1{font-size:20px;margin:0 0 12px}"
        ".box p{color:#94a3b8;font-size:14px;line-height:1.7;margin:0 0 26px}"
        "#gate-btn{padding:10px 30px;background:#7c3aed;border:none;border-radius:8px;color:#fff;"
        "font-size:15px;font-weight:600;cursor:pointer}"
        "#gate-btn:hover{background:#6d28d9}"
        '</style></head><body><div class="box">'
        '<h1>📝 仙宝内容平台</h1>'
        '<p>' + tip + '</p>'
        '<button id="gate-btn">' + btn + '</button>'
        '</div>'
        '<script src="/assets/xianbao/auth-widget.js?v=20260917"></script>'
        '<script>document.getElementById("gate-btn").onclick=function(){' + act + '};</script>'
        '</body></html>'
    )


@app.before_request
def _require_staff():
    """全站门禁：仅 admin / sales 可访问，其余 401 / 403"""
    p = request.path or "/"
    if request.method == "OPTIONS" or p.startswith(_GATE_FREE):
        return None
    u = current_user()
    if (u.get("role") or "") in STAFF_ROLES:
        return None
    if p.startswith("/api/"):
        if not u:
            return jsonify(success=False, error="未登录"), 401
        return jsonify(success=False, error="需要管理员或推广员权限"), 403
    return _gate_page(u), (200 if not u else 403)


def user_label(u=None):
    """展示用用户名：昵称 → 邮箱 → 空"""
    u = u if u is not None else current_user()
    return (u.get("nickname") or u.get("email") or "").strip()


def _can_cancel(j, me_id, me_name):
    """只有任务发起人本人能取消（无 owner 记录的老任务一律不給取消）"""
    if j.get("status") not in ("queued", "running"):
        return False
    oid = str(j.get("owner_id") or "")
    if oid:
        return bool(me_id) and oid == str(me_id)
    return bool(me_name) and (j.get("owner") or "") == me_name


def _is_admin(u=None):
    """管理员豁免：可查看/编辑全部文案与素材"""
    u = u if u is not None else current_user()
    return (u.get("role") or "") == "admin"


def _article_owner(article_id):
    """(owner_id, owner) —— 文案归属；老数据无归属返回 (None, '')"""
    try:
        r = get_content_db().execute('SELECT owner_id, owner FROM articles WHERE id=?',
                                     (int(article_id),)).fetchone()
        if r:
            return (r["owner_id"], r["owner"] or "")
    except Exception:
        pass
    return (None, "")


def _can_access_article(article_id, u=None):
    """文案可见性：admin 全部；其余仅本人；老数据（owner_id 为空）仅 admin 可见"""
    u = u if u is not None else current_user()
    if _is_admin(u):
        return True
    oid = _article_owner(article_id)[0]
    if oid is None:
        return False
    try:
        return int(oid) == int(u.get("id"))
    except Exception:
        return False


def _guard_article(article_id):
    """越权统一按「不存在」处理（不泄漏他人文案是否存在）；通过 → None"""
    if _can_access_article(article_id):
        return None
    return jsonify({"error": "文案不存在"}), 404


def _denied_page():
    """页面越权/不存在时的极简提示页"""
    return ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
            '<title>文案不存在</title></head>'
            '<body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;'
            'background:#f8fafc;font-family:-apple-system,\'PingFang SC\',sans-serif;color:#1e293b">'
            '<div style="text-align:center"><div style="font-size:44px;margin-bottom:14px">🔒</div>'
            '<div style="font-size:17px;font-weight:600;margin-bottom:10px">文案不存在或无权访问</div>'
            '<div style="color:#64748b;font-size:13px;margin-bottom:22px">只能查看自己创建的内容</div>'
            '<a href="/articles" style="color:#7c3aed;font-size:14px;text-decoration:none">← 返回文案列表</a>'
            '</div></body></html>'), 404


def _job_duration(j):
    """耗时（秒）：started_at → finished_at（未完则算到现在）"""
    def _p(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    a = _p(j.get("started_at") or "")
    if not a:
        return 0
    b = _p(j.get("finished_at") or "") or datetime.now()
    return max(0, int((b - a).total_seconds()))

def init_content_db():
    """确保 content.db 的 articles 表存在"""
    conn = sqlite3.connect(CONTENT_DATABASE)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title VARCHAR(200) NOT NULL,
            platform VARCHAR(20) DEFAULT 'wechat',
            content_type VARCHAR(20) DEFAULT 'article',
            book VARCHAR(100),
            topic VARCHAR(100),
            angle VARCHAR(100),
            structure VARCHAR(50),
            hook VARCHAR(50),
            tone VARCHAR(50),
            content_md TEXT,
            content_html TEXT,
            summary VARCHAR(200),
            tags TEXT,
            word_count INTEGER DEFAULT 0,
            status VARCHAR(20) DEFAULT 'pending',
            source VARCHAR(20) DEFAULT 'ai',
            file_path VARCHAR(500),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME
        );
    ''')
    # 出图参数记录（「参数透明化」：每张图提交给 ComfyUI 的提示词与参数）
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS illustration_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL,
            name VARCHAR(120) NOT NULL,
            kind VARCHAR(20) DEFAULT '',
            prompt TEXT,
            neg TEXT,
            style VARCHAR(20) DEFAULT '',
            seed INTEGER,
            steps INTEGER,
            cfg REAL,
            sampler VARCHAR(40) DEFAULT '',
            scheduler VARCHAR(40) DEFAULT '',
            width INTEGER,
            height INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_illu_logs_article ON illustration_logs(article_id);
    ''')
    # Auto-migrate: 补 unet / lora / denoise 列（CREATE TABLE IF NOT EXISTS 对已存在的表是空操作）
    lcols = [r[1] for r in conn.execute("PRAGMA table_info(illustration_logs)").fetchall()]
    for _col, _ddl in (("unet", "VARCHAR(200) DEFAULT ''"),
                       ("lora", "VARCHAR(400) DEFAULT ''"),
                       ("denoise", "REAL")):
        if _col not in lcols:
            conn.execute("ALTER TABLE illustration_logs ADD COLUMN %s %s" % (_col, _ddl))
    conn.commit()
    # Auto-migrate: add platform/content_type if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()]
    if 'platform' not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN platform VARCHAR(20) DEFAULT 'wechat'")
    if 'content_type' not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN content_type VARCHAR(20) DEFAULT 'article'")
    if 'uuid' not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN uuid VARCHAR(36)")
        conn.execute("UPDATE articles SET uuid = hex(randomblob(16)) WHERE uuid IS NULL")
    if 'stage' not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN stage VARCHAR(20) DEFAULT 'done'")
    if 'img_requirements' not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN img_requirements TEXT")
    if 'service_line' not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN service_line VARCHAR(20) DEFAULT ''")
    conn.commit()
    conn.close()

init_content_db()

# ============================================================
# 模板全局函数
# ============================================================
@app.template_global()
def getStatusTag(status):
    colors = {
        'pending': ('#f59e0b', '#fef3c7', '⏳ 待审'),
        'approved': ('#22c55e', '#dcfce7', '✅ 已通过'),
        'rejected': ('#ef4444', '#fee2e2', '❌ 已拒绝'),
        'published': ('#3b82f6', '#dbeafe', '📤 已发布'),
        'draft': ('#64748b', '#f1f5f9', '📝 草稿'),
    }
    fg, bg, label = colors.get(status, ('#64748b', '#f1f5f9', status or '未知'))
    return f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:12px;font-size:12px;">{label}</span>'

# ============================================================
# 路由 - 页面
# ============================================================
@app.route('/')
def home():
    """首页 → 文案列表"""
    return redirect('/articles')


@app.route('/images')
def images_page():
    """配图（阶段 2）"""
    return render_template('images.html')

@app.route('/video')
def video_stage_page():
    """视频制作（阶段 3）"""
    return render_template('video_stage.html')

@app.route('/library')
def library():
    return render_template("library.html")

@app.route('/settings')
def settings_page():
    return render_template("settings.html", style_groups=style_groups())

# ============================================================
# 页面 - 公众号文章
# ============================================================
@app.route('/articles')
def articles_page():
    return render_template("articles.html")

@app.route('/articles/<int:article_id>')
def article_detail_page(article_id):
    return render_template("article_detail.html", article_id=article_id)

# ============================================================
# API - 公众号文章
# ============================================================
@app.route('/api/articles')
def api_list_articles():
    """文章列表（可按状态 status=、业务线 line= 过滤）"""
    db = get_content_db()
    where, args = [], []
    status = request.args.get('status')
    line = request.args.get('line')
    if status:
        where.append('status=?'); args.append(status)
    if line:
        where.append("IFNULL(NULLIF(service_line,''),'lingxiu')=?"); args.append(line)  # 老数据无 service_line → 视为灵性书籍
    sql = 'SELECT * FROM articles'
    _u = current_user()
    if not _is_admin(_u):
        # 数据隔离：只看自己的；老数据（owner_id 为空）只有 admin 能看
        where.append('owner_id = ?')
        args.append((_u or {}).get('id'))
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    rows = db.execute(sql + ' ORDER BY created_at DESC', args).fetchall()
    return jsonify(with_ref_counts(rows))

@app.route('/api/articles/<int:article_id>')
def api_get_article(article_id):
    """文章详情"""
    _g = _guard_article(article_id)
    if _g:
        return _g
    db = get_content_db()
    row = db.execute('SELECT * FROM articles WHERE id=?', (article_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    d = dict(row)
    d['ref_count'] = ref_counts().get(str(d.get('id')), 0)
    return jsonify(d)

@app.route('/api/articles', methods=['POST'])
def api_create_article():
    """创建文章（Job 或手动）"""
    data = request.json
    db = get_content_db()
    content_md = data.get('content_md', '')
    word_count = len(content_md.replace(' ', '').replace('\n', ''))
    _u = current_user()
    cursor = db.execute('''
        INSERT INTO articles (title, book, topic, angle, structure, hook, tone,
                              content_md, content_html, summary, tags, word_count,
                              status, source, file_path, owner_id, owner)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        data.get('title', '无标题'),
        data.get('book', ''),
        data.get('topic', ''),
        data.get('angle', ''),
        data.get('structure', ''),
        data.get('hook', ''),
        data.get('tone', ''),
        content_md,
        data.get('content_html', ''),
        data.get('summary', ''),
        data.get('tags', ''),
        word_count,
        data.get('status', 'pending'),
        data.get('source', 'ai'),
        data.get('file_path', ''),
        (_u or {}).get('id'),
        user_label(_u),
    ))
    db.commit()
    return jsonify({"ok": True, "id": cursor.lastrowid})

@app.route('/api/articles/<int:article_id>', methods=['PUT'])
def api_update_article(article_id):
    """更新文章（状态/内容）"""
    _g = _guard_article(article_id)
    if _g:
        return _g
    data = request.json
    db = get_content_db()
    row = db.execute('SELECT id FROM articles WHERE id=?', (article_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    sets = []
    vals = []
    for field in ['title', 'book', 'topic', 'angle', 'structure', 'hook', 'tone',
                  'content_md', 'content_html', 'summary', 'tags', 'status']:
        if field in data:
            sets.append(f'{field}=?')
            vals.append(data[field])
    if 'content_md' in data:
        sets.append('word_count=?')
        vals.append(len(data['content_md'].replace(' ', '').replace('\n', '')))
    sets.append('updated_at=CURRENT_TIMESTAMP')
    vals.append(article_id)
    db.execute(f'UPDATE articles SET {", ".join(sets)} WHERE id=?', vals)
    db.commit()
    return jsonify({"ok": True})

@app.route('/api/articles/<int:article_id>/status', methods=['POST'])
def api_update_article_status(article_id):
    """更新文章状态"""
    _g = _guard_article(article_id)
    if _g:
        return _g
    data = request.json
    status = data.get('status')
    if status not in ('pending', 'approved', 'rejected', 'published'):
        return jsonify({"error": "invalid status"}), 400
    db = get_content_db()
    db.execute('UPDATE articles SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', (status, article_id))
    db.commit()
    return jsonify({"ok": True})

@app.route('/api/articles/<int:article_id>', methods=['DELETE'])
def api_delete_article(article_id):
    """删除文章（同时清理该文章生成的配图目录）"""
    _g = _guard_article(article_id)
    if _g:
        return _g
    db = get_content_db()
    db.execute('DELETE FROM articles WHERE id=?', (article_id,))
    db.commit()
    removed = 0
    try:
        out_dir = GEN_DIR / str(article_id)
        if out_dir.is_dir():
            removed = len(list(out_dir.iterdir()))
            shutil.rmtree(str(out_dir))
    except Exception:
        pass
    return jsonify({"ok": True, "images_removed": removed})

# ============================================================
# API - 内容选项 + AI生成
# ============================================================
def _load_wheel():
    for p in [Path("/var/www/social-media-admin/data/config/content-wheel.json"),
              Path("/home/bosidon/projects/social-media/gongzhonghao/content-wheel.json")]:
        if p.exists():
            return json.loads(p.read_text())
    return {}

@app.route('/api/options')
def api_options():
    """返回内容轮盘的所有可选项（兼容 articles 和 workbench）"""
    wheel = _load_wheel()

    # 从精读站读取书目和话题
    LINGXIU_DB = '/var/www/lingxiu/data/xianbao.db'
    try:
        import sqlite3
        conn = sqlite3.connect(LINGXIU_DB)
        conn.row_factory = sqlite3.Row
        # 书目
        books_rows = conn.execute("""
            SELECT DISTINCT b.id, b.title, b.author
            FROM books b 
            JOIN ai_deep_categories c ON c.book_id = b.id
            JOIN ai_deep_themes t ON t.category_id = c.id
            ORDER BY b.title
        """).fetchall()
        books = [{'id': b['id'], 'name': b['title'], 'author': b['author']} for b in books_rows]
        # 话题
        topics_rows = conn.execute("""
            SELECT t.title, c.name as category, b.id as book_id, b.title as book_name
            FROM ai_deep_themes t
            JOIN ai_deep_categories c ON t.category_id = c.id
            JOIN books b ON c.book_id = b.id
            ORDER BY c.name, t.title
        """).fetchall()
        topics = [{'name': r['title'], 'category': r['category'], 'book_id': r['book_id'], 'book_name': r['book_name']} for r in topics_rows]
        conn.close()
    except Exception:
        # 备用：从配置文件读取
        books = [{'name': b['name'], 'weight': b.get('weight', 1)} for b in wheel.get('books', {}).get('pool', [])]
        topics = []
        for cat in wheel.get('themes', {}).get('categories', []):
            for ex in cat.get('examples', []):
                topics.append({'name': ex, 'category': cat['name']})
    
    angles = [{'name': '个人故事', 'desc': '用自己的经历切入'}, {'name': '书中金句', 'desc': '引用书中原文'}, {'name': '案例分析', 'desc': '结合现实案例'}, {'name': '提问引导', 'desc': '用问题引发思考'}, {'name': '对比反思', 'desc': '对比常见误区与真相'}, {'name': '热点关联', 'desc': '关联当下热点话题'}, {'name': '情感共鸣', 'desc': '从情感体验出发'}, {'name': '科学解读', 'desc': '结合心理学/神经科学'}]

    structures = [
        {'name': 'standard', 'label': '标准解读型', 'desc': '开头→核心观点→解读+案例→练习→引导'},
        {'name': 'story', 'label': '故事引入型', 'desc': '故事→话题→书中印证→反思→行动'},
        {'name': 'debate', 'label': '观点碰撞型', 'desc': '争议观点→正方→反方→书中答案→立场'},
        {'name': 'listicle', 'label': '清单型', 'desc': '引子→5-7个要点→总结→引导'},
        {'name': 'qa', 'label': '问答型', 'desc': '读者提问→分析→书中观点→方案→鼓励'},
        {'name': 'contrast', 'label': '对比型', 'desc': '两种误解→真相→书中怎么说→领悟→练习'},
        {'name': 'timeline', 'label': '时间线型', 'desc': '过去→现在→未来，按时间线展开'},
        {'name': 'story_list', 'label': '故事+清单', 'desc': '故事引入→清单展开→总结升华'},
    ]
    hooks = ['场景代入', '反常识', '数据/事实', '金句开头', '提问式', '故事式', '对比式', '时间线', '悬念式', '热点蹭流量']
    tones = [
        {'name': 'warm', 'label': '温暖朋友型', 'desc': '「你」「我」，感叹号，偶尔emoji'},
        {'name': 'reflective', 'label': '沉思独白型', 'desc': '「我们」，多问句，像写日记'},
        {'name': 'energetic', 'label': '活力分享型', 'desc': '短句多，破折号，兴奋分享'},
        {'name': 'rational', 'label': '理性分析型', 'desc': '逻辑清晰，数据支撑，客观冷静'},
    ]
    platforms = [
        {'id': 'wechat', 'name': '公众号'},
        {'id': 'xiaohongshu', 'name': '小红书'},
        {'id': 'video_account', 'name': '视频号'},
        {'id': 'douyin', 'name': '抖音'},
        {'id': 'bilibili', 'name': 'B站'},
        {'id': 'kuaishou', 'name': '快手'},
        {'id': 'podcast', 'name': '播客'},
        {'id': 'zhihu', 'name': '知乎'},
        {'id': 'toutiao', 'name': '头条'},
    ]
    content_types = [
        {'id': 'article', 'name': '图文文章'},
        {'id': 'short_video', 'name': '短视频脚本'},
        {'id': 'long_video', 'name': '长视频脚本'},
        {'id': 'speech', 'name': '口播稿'},
    ]

    return jsonify({
        'platforms': platforms, 'content_types': content_types,
        'books': books, 'topics': topics, 'angles': angles,
        'structures': structures, 'hooks': hooks, 'tones': tones,
    })

@app.route('/api/lines')
def api_lines():
    """业务线清单（含各自的选题类型与条数）"""
    out = []
    for l in contentlines.LINES:
        d = {"id": l["id"], "name": l["name"], "icon": l["icon"], "home": l["home"]}
        d["kinds"] = [{"id": k["id"], "name": k["name"],
                       "count": len(contentlines.topics(l["id"], k["id"]))}
                      for k in (l.get("kinds") or [])]
        out.append(d)
    return jsonify({"ok": True, "lines": out})


@app.route('/api/line-topics')
def api_line_topics():
    """某业务线某选题类型的对象清单（读各子站现成资产）"""
    line = request.args.get('line') or ''
    kind = request.args.get('kind') or ''
    if not contentlines.line_of(line):
        return jsonify({"ok": False, "error": "未知业务线", "items": []}), 400
    items = contentlines.topics(line, kind)
    return jsonify({"ok": True, "line": line, "kind": kind, "count": len(items), "items": items})


def _line_prompt(L, line, kind_id, topic_obj, material, plat_name, label, word_spec, fmt, tone):
    """业务线选题 prompt：素材是唯一事实来源，术语必须与素材一致"""
    kn = contentlines.kind_name(line, kind_id)
    return f"""你是「仙宝心灵成长」的内容创作Agent，本次写「{L['name']}」业务线的引流内容。

## 任务
平台：{plat_name}　形态：{label}　字数：{word_spec}
选题：{kn} —— {topic_obj or '自动选择'}
语气：{tone or '温暖、真诚、有洞察'}
输出格式：{fmt}

## 选题素材（**唯一事实来源**）
{material[:4500]}

## 写作要求
1. 只用素材里的术语与说法（如「印记/图腾/音阶/波符」这类原体系词汇），**不要换成星座、占星等其他体系的说法**；不要编造素材里没有的数据、年份、比例或案例
2. 从读者真实困惑切入（"我为什么总是…"这类），不要写成百科词条
3. 结尾给一个可执行的小练习或自我提问
4. 不要输出任何网址或链接（系统会自动在文末追加推广链接）
5. 标签 5-8 个
6. 输出 JSON：{{"title":"标题","content":"正文","summary":"120字摘要","tags":"标签1,标签2,..."}}
"""


PLAT_LABEL = {'wechat': '公众号', 'xiaohongshu': '小红书', 'video_account': '视频号',
              'douyin': '抖音', 'bilibili': 'B站', 'kuaishou': '快手',
              'podcast': '播客', 'zhihu': '知乎', 'toutiao': '头条', 'moments': '朋友圈'}
REWRITE_SPEC = {
    'article': '1000-2000字，Markdown 结构，开头3句抓住读者',
    'short_video': '300-500字，分镜格式，每段标注【画面】【台词】【时长】',
    'long_video': '1500-2500字，分镜格式，每段标注【画面】【台词】【时长】【转场】',
    'speech': '800-1200字，口语化，标注语气停顿和重音',
}


@app.route('/api/articles/<int:article_id>/rewrite', methods=['POST'])
def api_rewrite_article(article_id):
    """把已有文章改写为另一个平台/形态（生成新篇，保留原篇；沿用同一业务线与推广落点）"""
    data = request.json or {}
    platform = data.get('platform') or 'wechat'
    content_type = data.get('content_type') or 'article'
    _g = _guard_article(article_id)
    if _g:
        return _g
    db = get_content_db()
    row = db.execute('SELECT * FROM articles WHERE id=?', (article_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    src = dict(row)
    line = src.get('service_line') or 'lingxiu'
    plat_name = PLAT_LABEL.get(platform, platform)
    spec = REWRITE_SPEC.get(content_type, REWRITE_SPEC['article'])
    body = (src.get('content_md') or '')[:6000]
    prompt = f"""你是「仙宝心灵成长」的内容改写Agent。

## 任务
把下面这篇原文改写成「{plat_name}」平台的{content_type}：{spec}
- 保留原文的核心观点、术语与事实，**不要新增原文没有的数据或案例**
- 保留选题方向：{src.get('topic') or src.get('book') or '（原文自定）'}
- 重新组织结构与节奏以适配目标平台，标题也要重写
- 不要输出任何网址或链接（系统会自动在文末追加推广链接）
- 输出 JSON：{{"title":"标题","content":"正文","summary":"120字摘要","tags":"标签1,标签2,..."}}

## 原文
{body}
"""
    llm = get_llm_config()
    api_key = llm.get('llm_api_key')
    if not api_key:
        return jsonify({"error": "未配置 LLM API Key"}), 400
    try:
        with jobstore.LLM_GATE:
            resp = requests.post(llm['llm_base_url'],
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=dict({"model": llm['llm_model'], "messages": [{"role": "user", "content": prompt}]},
                          **({"user_id": uid_ok("p" + str(src.get('owner_id')))} if src.get('owner_id') else {})),
                timeout=180)
        raw = resp.json()['choices'][0]['message']['content']
        import re
        m = re.search(r'\{[\s\S]*\}', raw)
        art = json.loads(m.group()) if m else {'title': src.get('title') or '未命名', 'content': raw}
    except Exception as e:
        return jsonify({"error": "生成失败：%s" % e}), 500

    content_md = art.get('content', '')
    wc = len(content_md.replace(' ', '').replace('\n', ''))
    # 推广链接在入库拿到「新篇编号」后再拼（src = 文案编号），见下方两步写入
    cur = db.execute("INSERT INTO articles (title, platform, content_type, book, topic, angle,"
                     " structure, hook, tone, content_md, summary, tags, word_count,"
                     " status, source, promo_src, promo_link, service_line,"
                     " owner_id, owner)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'ai', ?, ?, ?, ?, ?)",
                     (art.get('title', '未命名'), platform, content_type, src.get('book'), src.get('topic'),
                      src.get('angle'), src.get('structure'), src.get('hook'), src.get('tone'),
                      content_md, art.get('summary', ''), art.get('tags', ''), wc,
                      '', None, line,
                      src.get('owner_id'), src.get('owner') or ''))
    new_id = cur.lastrowid
    promo_src = str(new_id)                                   # src = 新篇编号
    promo_link = contentlines.promo_link(line, src.get('owner_id'), promo_src)
    if promo_link:
        content_md = content_md.rstrip() + '\n\n---\n\n' + contentlines.guide(line) + '\n👉 ' + promo_link
        wc = len(content_md.replace(' ', '').replace('\n', ''))
    db.execute('UPDATE articles SET content_md=?, word_count=?, promo_src=?, promo_link=? WHERE id=?',
               (content_md, wc, promo_src, promo_link or None, new_id))
    db.commit()
    return jsonify({"ok": True, "id": new_id, "title": art.get('title', '未命名'),
                    "word_count": wc, "from": article_id})


@app.route('/api/articles/generate', methods=['POST'])
def api_generate_article():
    """AI生成文章"""
    data = request.json or {}
    platform = data.get('platform', 'wechat')
    content_type = data.get('content_type', 'article')
    book = data.get('book', '')
    topic = data.get('topic', '')
    angle = data.get('angle', '')
    structure = data.get('structure', '')
    hook = data.get('hook', '')
    tone = data.get('tone', '')
    _u = current_user()                              # 文案归属人
    promo_uid = (_u or {}).get('id')                 # 推广人 ID 就是用户 ID → 取归属人，不再单独填
    # promo_src（来源码）= 文案编号：入库拿到 id 后再赋值（两步写入，见下方）
    line = data.get('line') or 'lingxiu'             # 业务线（lingxiu/maya/tarot/psych）
    topic_kind = data.get('topic_kind') or ''        # 选题类型（如 challenge/seal/daily/kin）
    topic_obj = data.get('topic_obj') or ''          # 选题对象（如「红龙 · 源动力」）

    PLAT_NAME = {'wechat': '公众号', 'xiaohongshu': '小红书', 'video_account': '视频号',
                 'douyin': '抖音', 'bilibili': 'B站', 'kuaishou': '快手',
                 'podcast': '播客', 'zhihu': '知乎', 'toutiao': '头条'}

    # 读品牌指引
    guide_path = Path("/home/bosidon/projects/social-media/gongzhonghao/AGENT_GUIDE.md")
    guide = guide_path.read_text()[:3000] if guide_path.exists() else ''

    # 根据平台+类型构建 prompt（字数由平台和类型共同决定）
    type_specs = {
        'article': {
            'wechat':    '1000-2000字公众号长文',
            'xiaohongshu': '300-1000字小红书笔记',
            'video_account': '800-1500字视频文案',
            'douyin':    '300-500字短视频文案',
            'bilibili':  '1500-3000字深度长文',
            'kuaishou':  '300-800字短视频文案',
            'zhihu':     '800-2000字知乎回答',
            'toutiao':   '800-1500字图文文章',
            'podcast':   '800-2000字播客节目简介',
            '_default':  '800-1500字图文文章',
        },
        'short_video': {
            'wechat':    '对应60-180秒视频的分镜脚本',
            'video_account': '对应15-60秒视频的分镜脚本',
            'douyin':    '对应15-60秒视频的分镜脚本',
            'xiaohongshu': '对应30-90秒视频的分镜脚本',
            'bilibili':  '对应1-3分钟视频的分镜脚本',
            '_default':  '对应30-90秒视频的分镜脚本',
        },
        'long_video': {
            'wechat':    '对应3-10分钟视频的完整分镜脚本',
            'video_account': '对应3-5分钟视频的完整分镜脚本',
            'bilibili':  '对应5-15分钟视频的完整分镜脚本',
            '_default':  '对应5-10分钟视频的完整分镜脚本',
        },
        'speech': {
            'wechat':    '对应2-5分钟口播稿',
            'video_account': '对应60-180秒口播稿',
            'douyin':    '对应30-60秒口播稿',
            '_default':  '对应1-3分钟口播稿',
        },
    }
    type_format = {
        'article':    'Markdown格式，分段清晰',
        'short_video': '分镜格式，每段标注【画面】【台词】【时长】',
        'long_video':  '分镜格式，每段标注【画面】【台词】【时长】【转场】',
        'speech':     '口语化，标注语气停顿和重音',
    }
    # 平台覆盖：小红书按「笔记体」输出（原 xhs_note 的写法，已合并进图文文章）
    fmt_platform = {
        'xiaohongshu': '短句分段，多用 emoji，正文不用 Markdown 标题层级，末尾加标签',
    }
    type_label = {
        'article': '图文文章', 'short_video': '短视频脚本',
        'long_video': '长视频脚本', 'speech': '口播稿',
    }

    spec_group = type_specs.get(content_type, type_specs['article'])
    word_spec = spec_group.get(platform, spec_group.get('_default', '1000-2000字'))
    fmt = type_format.get(content_type, 'Markdown格式')
    fmt = fmt_platform.get(platform, fmt)
    label = type_label.get(content_type, type_label['article'])

    plat_name = PLAT_NAME.get(platform, platform)
    prompt = f"""你是「仙宝心灵成长」的专职内容创作Agent。

## 任务
生成一篇「{plat_name}」平台的{label}。

## 参数
- 自媒体平台：{plat_name}
- 内容类型：{label}
- 书目：{book or '自动选择'}
- 话题：{topic or '自动选择'}
- 角度：{angle or '自动选择'}
- 结构：{structure or '自动选择'}
- 钩子：{hook or '自动选择'}
- 语气：{tone or '自动选择'}
- 字数要求：{word_spec}

## 品牌指引（摘要）
{guide[:2000]}

## 输出要求
1. 输出格式：{fmt}
2. 必须遵守禁用词表
3. 注入至少3项个人元素
4. 结尾提供1-2个可操作练习
5. 不要输出任何网址或链接（系统会自动在文末追加推广链接）
6. 标签5-8个
7. 输出JSON格式：{{"title":"标题","content":"正文","summary":"120字摘要","tags":"标签1,标签2,..."}}
"""

    # 业务线（玛雅/塔罗/心理咨询）：用该选题的原始素材重写 prompt
    # （灵性书籍仍走上面的原有 prompt，一字不改）
    if line != 'lingxiu' and contentlines.line_of(line):
        prompt = _line_prompt(contentlines.line_of(line), line, topic_kind, topic_obj,
                              contentlines.material(line, topic_kind, topic_obj),
                              plat_name, label, word_spec, fmt, tone)

    llm = get_llm_config()
    api_key = llm['llm_api_key']
    if not api_key:
        return jsonify({"error": "未配置 LLM API Key"}), 400

    try:
        with jobstore.LLM_GATE:            # 同步文案也占 LLM 域名额（防绕过限流打爆 key）
            resp = requests.post(llm['llm_base_url'],
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=dict({"model": llm['llm_model'], "messages": [{"role": "user", "content": prompt}]},
                          **({"user_id": uid_ok("p" + str(promo_uid))} if promo_uid else {})),
                timeout=120
            )
        result = resp.json()
        raw = result['choices'][0]['message']['content']

        # 尝试解析JSON
        import re
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if json_match:
            article_data = json.loads(json_match.group())
        else:
            article_data = {'title': '未命名', 'content': raw, 'summary': '', 'tags': ''}

        content_md = article_data.get('content', raw)
        # 推广链接在入库拿到「文案编号」后再拼（src = 文案编号），见下方两步写入

        if line != 'lingxiu':          # 业务线文章：话题列存选题对象
            book, topic = '', (topic_obj or topic)

        word_count = len(content_md.replace(' ', '').replace('\n', ''))

        db = get_content_db()
        cursor = db.execute('''
            INSERT INTO articles (title, platform, content_type, book, topic, angle,
                                  structure, hook, tone, content_md, summary, tags,
                                  word_count, status, source, promo_src, promo_link,
                                  service_line, owner_id, owner)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'ai', ?, ?, ?, ?, ?)
        ''', (
            article_data.get('title', '未命名'),
            platform, content_type,
            book, topic, angle, structure, hook, tone,
            content_md,
            article_data.get('summary', ''),
            article_data.get('tags', ''),
            word_count,
            '', None,                    # promo_src / promo_link：拿到文案编号后回填
            line,
            (_u or {}).get('id'),
            user_label(_u),
        ))
        new_id = cursor.lastrowid
        # ===== 两步写入：src = 文案编号；链接 = 业务线落点 + 归属人 + 编号 =====
        promo_src = str(new_id)
        promo_link = contentlines.promo_link(line, promo_uid, promo_src)
        if promo_link:
            guide_line = '\n\n---\n\n' + contentlines.guide(line) + '\n👉 ' + promo_link
            if promo_link not in content_md:
                content_md = content_md.rstrip() + guide_line
                word_count = len(content_md.replace(' ', '').replace('\n', ''))
        db.execute('UPDATE articles SET content_md=?, word_count=?, promo_src=?, promo_link=? WHERE id=?',
                   (content_md, word_count, promo_src, promo_link or None, new_id))
        db.commit()

        return jsonify({
            "ok": True,
            "id": cursor.lastrowid,
            "title": article_data.get('title', '未命名'),
            "word_count": word_count,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================================================
# API - 设置
# ============================================================
@app.route('/api/settings')
def api_get_settings():
    db = get_db()
    settings = db.execute('SELECT * FROM settings').fetchall()
    return jsonify({s['key']: s['value'] for s in settings})

@app.route('/api/settings', methods=['POST'])
def api_save_settings():
    data = request.json
    db = get_db()
    for key, value in data.items():
        db.execute('''
            INSERT OR REPLACE INTO settings (key, value)
            VALUES (?, ?)
        ''', (key, str(value)))
    db.commit()
    return jsonify({"ok": True})


# ============================================================
# API - 视频制作（阶段3：分镜脚本 / 分镜视频）
# ============================================================
@app.route('/api/video-plans')
def api_list_video_plans():
    """视频计划列表"""
    db = get_content_db()
    _u = current_user()
    _sql = '''
        SELECT v.*, a.title as article_title, a.platform, a.content_type
        FROM video_plans v
        LEFT JOIN articles a ON v.article_id = a.id'''
    _args = []
    if not _is_admin(_u):
        _sql += ' WHERE a.owner_id = ?'
        _args.append((_u or {}).get('id'))
    rows = db.execute(_sql + ' ORDER BY v.updated_at DESC, v.created_at DESC LIMIT 200',
                      _args).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/video-plans', methods=['POST'])
def api_create_video_plan():
    """为文案创建视频计划"""
    data = request.json or {}
    article_id = data.get('article_id')
    if not article_id:
        return jsonify({"error": "article_id 必填"}), 400
    _g = _guard_article(article_id)
    if _g:
        return _g
    db = get_content_db()
    existing = db.execute('SELECT id FROM video_plans WHERE article_id=?', (article_id,)).fetchone()
    if existing:
        return jsonify({"ok": True, "id": existing['id'], "existed": True})
    cur = db.execute(
        'INSERT INTO video_plans (article_id, title, status) VALUES (?, ?, ?)',
        (article_id, data.get('title', ''), 'draft')
    )
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})

@app.route('/api/video-plans/<int:plan_id>')
def api_get_video_plan(plan_id):
    db = get_content_db()
    row = db.execute('SELECT * FROM video_plans WHERE id=?', (plan_id,)).fetchone()
    if not row:
        return jsonify({"error": "不存在"}), 404
    _g = _guard_article(row['article_id'])
    if _g:
        return _g
    return jsonify(dict(row))

@app.route('/api/video-plans/<int:plan_id>', methods=['PUT'])
def api_update_video_plan(plan_id):
    """保存脚本 / 分镜 / 状态"""
    data = request.json or {}
    db = get_content_db()
    _row = db.execute('SELECT article_id FROM video_plans WHERE id=?', (plan_id,)).fetchone()
    if not _row:
        return jsonify({"error": "不存在"}), 404
    _g = _guard_article(_row['article_id'])
    if _g:
        return _g
    sets, vals = [], []
    for f in ('script', 'storyboard', 'status', 'title'):
        if f in data:
            sets.append(f + '=?')
            vals.append(data[f])
    if not sets:
        return jsonify({"error": "无更新字段"}), 400
    sets.append('updated_at=CURRENT_TIMESTAMP')
    vals.append(plan_id)
    db.execute('UPDATE video_plans SET ' + ', '.join(sets) + ' WHERE id=?', vals)
    db.commit()
    return jsonify({"ok": True})

@app.route('/video-plan/<int:plan_id>')
def video_plan_page(plan_id):
    """分镜脚本编辑页"""
    try:
        _row = get_content_db().execute('SELECT article_id FROM video_plans WHERE id=?',
                                        (plan_id,)).fetchone()
    except Exception:
        _row = None
    if not _row or not _can_access_article(_row['article_id']):
        return _denied_page()
    return render_template('video_plan.html', plan_id=plan_id)

# ============================================================
# API - 配图（阶段2：二维码 / 素材选用 / AI 生图）
# ============================================================

GEN_DIR = BASE_DIR / 'static' / 'generated'
GEN_DIR.mkdir(parents=True, exist_ok=True)


@app.route('/static/generated/<path:sub>', endpoint='generated_file')
def generated_file(sub):
    """配图/素材文件：登录用户只能取自己文案目录下的（admin 全部）"""
    try:
        p = (GEN_DIR / sub).resolve()
        if not str(p).startswith(str(GEN_DIR.resolve())) or not p.is_file():
            return '', 404
    except Exception:
        return '', 404
    art = sub.split('/', 1)[0]
    _u = current_user()
    _url = '/static/generated/' + sub
    if art.isdigit():
        # 文章归属 或 素材归属 任一放行（推广员可能持有别人文案目录下的素材）
        if not _can_access_article(int(art), _u) and \
                not materialstore.can_view_url(_url, (_u or {}).get('id')):
            return '', 404
    elif not _is_admin(_u) and not materialstore.can_view_url(_url, (_u or {}).get('id')):
        return '', 404
    resp = send_file(str(p))
    resp.headers['Cache-Control'] = 'no-cache, must-revalidate'
    return resp


@app.route('/api/articles/<int:article_id>/images', methods=['GET'])
def api_get_article_images(article_id):
    """获取文案的配图列表"""
    _g = _guard_article(article_id)
    if _g:
        return _g
    db = get_content_db()
    row = db.execute('SELECT images_json FROM articles WHERE id=?', (article_id,)).fetchone()
    if not row:
        return jsonify({"error": "文案不存在"}), 404
    try:
        imgs = json.loads(row['images_json'] or '[]')
    except Exception:
        imgs = []
    return jsonify({"ok": True, "images": imgs})


@app.route('/api/prompts/plan', methods=['GET'])
def api_get_plan_prompt():
    """配图 Agent 的 System Prompt（文件 prompts/image_agent.md）"""
    return jsonify({"ok": True, "content": load_plan_prompt_raw(),
                    "path": "prompts/image_agent.md"})


@app.route('/api/prompts/plan', methods=['POST'])
def api_save_plan_prompt():
    """保存 System Prompt（写文件）· body {content} 或 {reset:true} 恢复出厂默认"""
    data = request.json or {}
    ok, err = save_plan_prompt(PLAN_PROMPT_DEFAULT) if data.get('reset') \
        else save_plan_prompt(data.get('content') or '')
    if not ok:
        return jsonify({"error": err}), 400
    return jsonify({"ok": True, "content": load_plan_prompt_raw()})


@app.route('/api/articles/<int:article_id>/illustrations/logs')
def api_illustration_logs(article_id):
    """每张配图生成时提交的提示词与参数（出图排查用）"""
    _g = _guard_article(article_id)
    if _g:
        return _g
    db = get_content_db()
    try:
        rows = db.execute(
            'SELECT name, kind, prompt, neg, style, seed, steps, cfg, sampler, scheduler, '
            'width, height, unet, lora, denoise, created_at FROM illustration_logs WHERE article_id=? '
            'ORDER BY id DESC', (article_id,)).fetchall()
    except Exception:
        rows = []
    logs = {}
    for r in rows:
        d = dict(r)
        logs.setdefault(d.pop('name'), d)          # 同名只留最新一条
    return jsonify({"ok": True, "logs": logs})


@app.route('/illustrate/<int:article_id>')
def illustrate_page(article_id):
    """配图编辑页"""
    if not _can_access_article(article_id):
        return _denied_page()
    return render_template('illustrate.html', article_id=article_id,
                           style_groups=style_groups(),
                           style_label_map=style_label_map())

# ============================================================
# API - 一键配图（AI 分析文案 → 自动生成配图方案）
# ============================================================
PLATFORM_CARD = {
    'xiaohongshu': ('xiaohongshu', 4),   # 尺寸, 建议张数
    'moments':     ('moments', 3),
    'wechat':      ('wechat', 2),
    'video_account': ('story', 3),
    'douyin':      ('story', 3),
    'bilibili':    ('xiaohongshu', 3),
    'zhihu':       ('wechat', 2),
    'toutiao':     ('wechat', 2),
    'kuaishou':    ('story', 3),
    'podcast':     ('moments', 2),
}
@app.route('/api/articles/<int:article_id>/image-script', methods=['GET'])
def api_get_image_script(article_id):
    """读取配图方案（金句组 + 场景组）"""
    _g = _guard_article(article_id)
    if _g:
        return _g
    return jsonify({"ok": True, "plan": read_plan(article_id)})


@app.route('/api/articles/<int:article_id>/image-script', methods=['POST'])
def api_save_image_script(article_id):
    """保存配图方案"""
    _g = _guard_article(article_id)
    if _g:
        return _g
    data = request.json or {}
    plan = data.get('plan')
    if not isinstance(plan, dict):
        return jsonify({"error": "plan 必须是对象"}), 400
    return jsonify({"ok": True, "plan": save_plan(article_id, plan)})


# ============================================================
# API - 一键生成配图（AutoDL ComfyUI · 按需开关机）
# ============================================================
@app.route('/api/illustrate/generate', methods=['POST'])
def api_illustrate_generate():
    """启动生成任务（金句卡 + 场景配图，一次开机一次关机）"""
    data = request.json or {}
    article_id = data.get('article_id')
    if not article_id:
        return jsonify({"error": "缺少 article_id"}), 400
    _g = _guard_article(article_id)
    if _g:
        return _g
    row = get_content_db().execute('SELECT platform FROM articles WHERE id=?',
                                   (int(article_id),)).fetchone()
    card_size, card_want = PLATFORM_CARD.get(
        (row['platform'] if row else '') or 'wechat', ('xiaohongshu', 3))
    opts = {"cards": bool(data.get('cards')),
            "scenes": bool(data.get('scenes')),
            "replan": bool(data.get('replan')),
            "plan_only": bool(data.get('plan_only'))}
    u = current_user()
    return jsonify(start_generate(int(article_id), opts, get_llm_config(),
                                  card_size, card_want, int(data.get('count') or 3),
                                  owner=user_label(u), owner_id=str(u.get("id") or "")))


@app.route('/api/illustrate/job/<job_id>')
def api_illustrate_job(job_id):
    """查询生成任务进度"""
    j = get_job(job_id)
    if not j:
        return jsonify({"error": "任务不存在（可能已被清理）"}), 404
    j["log"] = j.get("log_lines") or []
    return jsonify(dict(ok=True, **j))


@app.route('/jobs')
def jobs_page():
    """任务看板（只查看）"""
    return render_template("jobs.html")


@app.route('/api/hosts')
def api_hosts():
    """AutoDL 主机（只读）：账号下每台实例的状态/规格/单价 + 正在跑的任务"""
    try:
        hosts = adl_hosts()
    except Exception as e:
        return jsonify({"ok": False, "error": "查询主机失败：%s" % e, "hosts": []})
    runs = {}
    for j in jobstore.list_jobs(active=True, limit=50):
        if j.get("status") == "running" and j.get("host"):
            runs.setdefault(j["host"], j)
    for h in hosts:
        r = runs.get(h["uuid"])
        if not r:
            continue
        title = ""
        try:
            row = get_content_db().execute('SELECT title FROM articles WHERE id=?',
                                           (r.get("article_id"),)).fetchone()
            if row:
                title = row[0] or ""
        except Exception:
            pass
        h["job"] = {"kind_label": r.get("kind_label") or "", "article_title": title,
                    "done": r.get("done") or 0, "total": r.get("total") or 0,
                    "status_label": r.get("status_label") or "",
                    "stage_label": r.get("stage_label") or ""}
    return jsonify({"ok": True, "hosts": hosts})


@app.route('/api/jobs')
def api_jobs():
    """任务列表：active=1 只看排队/进行中；article_id 过滤；默认最近 20 条"""
    active = request.args.get('active') in ('1', 'true', 'yes')
    aid = request.args.get('article_id')
    try:
        limit = max(1, min(100, int(request.args.get('limit') or 20)))
    except Exception:
        limit = 20
    out = []
    st = request.args.get('status') or None
    if st not in ('queued', 'running', 'done', 'failed', 'canceled', 'interrupted'):
        st = None
    state = request.args.get('state') or None
    if state not in ('active', 'done'):
        state = None
    me = current_user()
    me_id, me_name = str(me.get("id") or ""), user_label(me)
    _adm = _is_admin(me)
    # 归属权限：活跃任务（排队/进行中）全员可见；终态任务（完成/失败/取消/中断）只看自己，
    #           管理员看全部并可 ?owner=<uid> 筛选；带 article_id（配图页）时文章归属已校验，不受限
    owner_id, me_only = None, None
    if not aid:
        if _adm:
            ow = (request.args.get('owner') or '').strip()
            if ow:
                owner_id = me_id if ow == 'me' else ow
        else:
            me_only = me_id
    for j in jobstore.list_jobs(active=active, article_id=int(aid) if aid else None,
                                limit=limit, status=st, state=state,
                                owner_id=owner_id, me_id=me_only):
        j["log"] = j.get("log_lines") or []
        out.append(j)
    # 补文案名称（任务行显示《标题》；取不到留空，前端回落 #id）
    ids = sorted({j.get("article_id") for j in out if j.get("article_id")})
    titles = {}
    if ids:
        try:
            cdb = get_content_db()
            q = ",".join("?" * len(ids))
            for r in cdb.execute("SELECT id, title FROM articles WHERE id IN (%s)" % q, ids).fetchall():
                titles[r["id"]] = (r["title"] or "").strip()
        except Exception:
            titles = {}
    for j in out:
        j["article_title"] = titles.get(j.get("article_id"), "")
        j["can_cancel"] = _can_cancel(j, me_id, me_name)
        j["duration"] = _job_duration(j)
    # 用时统计（已完成）：口径 finished-started，含开机等待；范围跟随当前可见范围，不受 limit 限制
    if _adm:
        t_owner = owner_id if not aid else None
    else:
        t_owner = me_id          # 非管理员：用时统计永远只算自己（与「终态只看自己」一致）
    return jsonify({"ok": True, "jobs": out, "summary": jobstore.active_summary(),
                    "stats": jobstore.stats_today(owner_id=(None if (aid or _adm) else me_id)),
                    "timing": jobstore.timing_summary(owner_id=t_owner),
                    "me": {"id": me_id, "name": me_name, "admin": _adm},
                    "owners": jobstore.owner_options() if (_adm and not aid) else []})


@app.route('/api/jobs/<job_id>/cancel', methods=['POST'])
def api_job_cancel(job_id):
    """取消任务（只能取消自己发起的）：排队中直接撤销；进行中的出图任务会中断"""
    j = get_job(job_id)
    if not j:
        return jsonify({"ok": False, "error": "任务不存在或已结束"}), 404
    me = current_user()
    if not _can_cancel(j, str(me.get("id") or ""), user_label(me)):
        return jsonify({"ok": False, "error": "只能取消自己发起的任务"}), 403
    job_ok = jobstore.cancel(job_id)
    return jsonify({"ok": job_ok, "error": "" if job_ok else "任务不存在或已结束"})


@app.route('/api/illustrate/rewrite-item', methods=['POST'])
def api_rewrite_item():
    """只让 LLM 重写方案里某一条（纯 LLM · 不开机 · 不整组重跑）"""
    data = request.json or {}
    article_id, index = data.get('article_id'), data.get('index')
    if not article_id or index is None:
        return jsonify({"error": "参数不完整（article_id / index）"}), 400
    try:
        index = int(index)
        article_id = int(article_id)
    except Exception:
        return jsonify({"error": "index / article_id 必须是数字"}), 400
    _g = _guard_article(article_id)
    if _g:
        return _g
    item, err = rewrite_plan_item(article_id, index,
                                  data.get('hint') or '', get_llm_config())
    if err:
        return jsonify({"error": err})
    return jsonify({"ok": True, "item": item, "index": index})


@app.route('/api/illustrate/instance', methods=['GET'])
def api_instance_status():
    """查询 AutoDL 应用实例状态"""
    r = adl_status()
    return jsonify({"ok": r.get("code") == "Success",
                    "status": r.get("data") or "",
                    "msg": r.get("msg") or r.get("code") or ""})


@app.route('/api/illustrate/shutdown', methods=['POST'])
def api_instance_shutdown():
    """手动关闭实例（兜底）"""
    return jsonify(shutdown_instance())


@app.route('/api/comfy/check', methods=['POST'])
def api_comfy_check():
    """检测「自由填写」的模型 / 采样参数：拉实例清单逐项核对（只报告，不改配置）

    接收当前表单值（未保存也能检测）；实例没开机 / 地址不通 → ok=False + reason"""
    cfg = get_comfy_config()
    form = request.json or {}
    for k, v in form.items():
        if str(k).startswith('comfy_'):
            cfg[k] = '' if v is None else str(v)
    base = resolve_base_url()
    if not base:
        return jsonify({"ok": False, "reason": "拿不到 ComfyUI 地址（实例未开机或配置为空）"})
    try:
        lists = comfy_object_info(base)
    except Exception as e:
        msg = str(e)
        if msg.startswith("HTTP"):
            reason = "实例未运行（网关返回 %s）—— 出图时会自动开机" % msg.split()[1]
        else:
            reason = "实例未运行或地址不通（%s）" % type(e).__name__
        return jsonify({"ok": False, "base": base, "reason": reason})
    r = check_comfy_config(cfg, lists)
    r["ok"] = True
    r["base"] = base
    return jsonify(r)


@app.route('/api/models/groups')
def api_model_groups():
    """配图「模型组」+「生成档位」配置（数据源 configs/models.json）+ 当前生效的三件套"""
    cfg = get_comfy_config()
    mc = load_model_config()
    return jsonify({"groups": mc["groups"], "presets_default": mc["presets_default"],
                    "current": {"unet": cfg.get("comfy_unet") or "",
                                "clip": cfg.get("comfy_clip") or "",
                                "vae": cfg.get("comfy_vae") or ""}})


@app.route('/api/illustrate/overlay-qr', methods=['POST'])
def api_overlay_qr():
    """给选中的已配图叠加推广二维码（纯 PIL · 不开机 · 另存新图）"""
    data = request.json or {}
    article_id = data.get('article_id')
    imgs = data.get('images')
    if not article_id or not isinstance(imgs, list) or not imgs:
        return jsonify({"error": "缺少 article_id 或未选择图片"}), 400
    article_id = int(article_id)
    db = get_content_db()
    art = db.execute('SELECT owner_id, promo_src, promo_link, images_json '
                     'FROM articles WHERE id=?', (article_id,)).fetchone()
    if not art:
        return jsonify({"error": "文案不存在"}), 404
    if art['promo_link']:
        link = art['promo_link']
    elif art['owner_id']:
        link = 'https://xianbao.love/?ref=%s&src=%s' % (
            art['owner_id'], art['promo_src'] or str(article_id))
    else:
        link = None
    if not link:
        return jsonify({"error": "该文案没有推广链接"}), 400
    try:
        cur = json.loads(art['images_json'] or '[]')
        if not isinstance(cur, list):
            cur = []
    except Exception:
        cur = []
    owned = set(cur)
    prefix = '/static/generated/%d/' % article_id
    out_dir = GEN_DIR / str(article_id)
    done, skipped = [], []
    for u in imgs:
        name = u[len(prefix):] if isinstance(u, str) and u.startswith(prefix) else ''
        if (not name or u not in owned or '/' in name or '..' in name
                or not name.lower().endswith('.png') or name.endswith('_q.png')):
            skipped.append(u)
            continue
        src = out_dir / name
        if not src.exists():
            skipped.append(u)
            continue
        try:
            new_path = stamp_qr(str(src), link)
        except Exception:
            skipped.append(u)
            continue
        done.append(prefix + os.path.basename(new_path))
    if done:
        for u in done:
            if u not in cur:
                cur.append(u)
        db.execute('UPDATE articles SET images_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                   (json.dumps(cur, ensure_ascii=False), article_id))
        db.commit()
    return jsonify({"ok": True, "count": len(done), "images": done, "skipped": skipped})


@app.route('/api/illustrate/delete-image', methods=['POST'])
def api_delete_image():
    """删除配图：物理删除文件 + 从 images_json 移除"""
    data = request.json or {}
    article_id = data.get('article_id')
    url = data.get('url')
    if not article_id or not isinstance(url, str) or not url:
        return jsonify({"error": "缺少 article_id 或 url"}), 400
    article_id = int(article_id)
    db = get_content_db()
    art = db.execute('SELECT images_json FROM articles WHERE id=?',
                     (article_id,)).fetchone()
    if not art:
        return jsonify({"error": "文案不存在"}), 404
    try:
        cur = json.loads(art['images_json'] or '[]')
        if not isinstance(cur, list):
            cur = []
    except Exception:
        cur = []
    prefix = '/static/generated/%d/' % article_id
    name = url[len(prefix):] if url.startswith(prefix) else ''
    if not name or '/' in name or '..' in name or url not in cur:
        return jsonify({"error": "非法图片地址"}), 400
    deleted = False
    try:
        p = GEN_DIR / str(article_id) / name
        if p.is_file():
            p.unlink()
            deleted = True
    except Exception:
        pass
    try:
        db.execute('DELETE FROM illustration_logs WHERE article_id=? AND name=?',
                   (article_id, name))
    except Exception:
        pass
    cur = [u for u in cur if u != url]
    db.execute('UPDATE articles SET images_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (json.dumps(cur, ensure_ascii=False), article_id))
    db.commit()
    return jsonify({"ok": True, "deleted": deleted, "name": name, "count": len(cur)})


# ============================================================
# API - 素材库
# ============================================================
@app.route('/api/library')
def api_get_library():
    """素材库：我的素材（含历史未归属的老素材）/ 平台共享（暂未启用）"""
    library_type = request.args.get('type', 'all')     # all / image / audio / video
    scope = request.args.get('scope', 'mine')          # mine / shared
    u = current_user()
    rows = materialstore.list_for(uid=(u or {}).get('id'), mtype=library_type,
                                  scope=scope, is_admin=_is_admin(u))
    # 归属文案标题（素材库按文案归类用；跨库查 content.db，取不到留空由前端回落「文案 #id」）
    ids = sorted({int(r.get("article_id") or 0) for r in rows if (r.get("article_id") or 0)})
    titles = {}
    if ids:
        try:
            cdb = get_content_db()
            q = ",".join("?" * len(ids))
            for t in cdb.execute("SELECT id, title, IFNULL(NULLIF(service_line,''),'lingxiu') AS line "
                                 "FROM articles WHERE id IN (%s)" % q, ids).fetchall():
                titles[t["id"]] = ((t["title"] or "").strip(), t["line"] or "")
        except Exception:
            titles = {}
    for r in rows:
        t = titles.get(int(r.get("article_id") or 0)) or ("", "")
        r["article_title"] = t[0]
        r["article_line"] = t[1]
    return jsonify(rows)


@app.route('/api/materials/options')
def api_materials_options():
    """面板选项（抠图模型/背景、编辑功能/画风、拼版分辨率、文生图风格/比例）—— 服务端下发，前端不硬编码"""
    o = materialstore.options()
    o["txt2img"] = {                     # 文生图：风格分组 + 比例表（与「文章配图」同一份 styles.json / ASPECT_SIZE）
        "styles": style_groups(),
        "aspects": [{"id": k, "label": "%s（%d×%d）" % (k, v[0], v[1])} for k, v in ASPECT_SIZE.items()],
        "default": {"style": get_default_style(), "aspect": "3:4"},
    }
    return jsonify(o)


@app.route('/api/materials/thumb')
def api_materials_thumb():
    """素材缩略图（网格用，360px）：避免列表页直接加载几 MB 原图"""
    u = current_user()
    url_arg = request.args.get('u') or ''
    if not _is_admin(u) and not materialstore.can_view_url(url_arg, (u or {}).get('id')):
        return '', 404
    p = materialstore.url_to_path(url_arg)
    if not p or not p.is_file():
        return '', 404
    t = materialstore.ensure_thumb(p)
    if not t:
        t = p
    resp = send_file(str(t), mimetype='image/jpeg', conditional=True)
    resp.headers['Cache-Control'] = 'no-cache, must-revalidate'
    return resp


@app.route('/api/materials/upload', methods=['POST'])
def api_materials_upload():
    """上传素材：图片（压缩后存）、音频、视频"""
    u = current_user() or {}
    if not u.get('id'):
        return jsonify({"error": "请先登录后再上传"}), 401
    files = request.files.getlist('file')
    if not files:
        return jsonify({"error": "没有收到文件"}), 400
    items, errors = [], []
    for fs in files:
        try:
            row = materialstore.save_upload(fs, owner_id=u.get('id'), owner=user_label(u))
            if row:
                items.append(row)
        except ValueError as e:
            errors.append("%s：%s" % (fs.filename or '?', e))
        except Exception as e:
            errors.append("%s：上传失败(%s)" % (fs.filename or '?', str(e)[:80]))
    return jsonify({"ok": True, "items": items, "errors": errors})


@app.route('/api/materials/txt2img', methods=['POST'])
def api_materials_txt2img():
    """文生图：提词 + 风格 + 比例 → GPU 队列（出图链路与「文章配图」同一套）"""
    u = current_user() or {}
    body = request.get_json(silent=True) or {}
    prompt = (body.get('prompt') or '').strip()
    style = (body.get('style') or '').strip()
    aspect = (body.get('aspect') or '').strip()
    if not prompt:
        return jsonify({"error": "请填写提词"}), 400
    if len(prompt) > 500:
        return jsonify({"error": "提词太长（最多 500 字）"}), 400
    if style and style not in style_types():
        return jsonify({"error": "风格不存在"}), 400
    if aspect not in ASPECT_SIZE:
        return jsonify({"error": "比例不存在"}), 400
    cfg = get_comfy_config()
    if not cfg.get('comfy_instance_uuid') or not cfg.get('comfy_api_token'):
        return jsonify({"error": "未配置应用实例 UUID / Token，请去「设置」页填写"}), 400
    payload = {"prompt": prompt, "style": style or get_default_style(), "aspect": aspect}
    jid, reused = jobstore.DISPATCHER.enqueue('txt2img', None, payload, 1, priority=10,
                                             owner=user_label(u), owner_id=u.get('id'))
    return jsonify({"ok": True, "job_id": jid, "reused": reused,
                    "queue_pos": jobstore.queue_pos(jid)})


@app.route('/api/materials/download', methods=['POST'])
def api_materials_download():
    """批量下载所选素材（打包 zip）；只允许自己的（admin 豁免）"""
    import io as _io
    import zipfile
    u = current_user() or {}
    body = request.get_json(silent=True) or {}
    ids = body.get('ids') or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "请先选择素材"}), 400
    rows = [r for r in (materialstore.get_material(i) for i in ids) if r]
    if not rows:
        return jsonify({"error": "素材不存在或已删除"}), 400
    if not _is_admin(u):
        uid = u.get('id')
        for r in rows:
            own = r.get('owner_id')
            try:
                same = own is not None and int(own) == int(uid)
            except Exception:
                same = False
            if not same:
                return jsonify({"error": "只能下载自己的素材"}), 403
    buf = _io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        used = set()
        for r in rows:
            fp = materialstore.url_to_path(r.get('file_path'))
            if not fp or not fp.is_file():
                continue
            name = fp.name
            k = 2
            while name in used:                       # 重名文件加序号，避免 zip 内互相覆盖
                name = "%s_%d%s" % (fp.stem, k, fp.suffix)
                k += 1
            used.add(name)
            z.write(str(fp), name)
            n += 1
    if not n:
        return jsonify({"error": "文件都不在了，无法打包"}), 400
    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name="materials_%s.zip" % time.strftime("%Y%m%d_%H%M%S"))


@app.route('/api/materials/delete', methods=['POST'])
def api_materials_delete():
    """删除素材：只能删自己的（含历史未归属的）"""
    u = current_user() or {}
    body = request.get_json(silent=True) or {}
    mid = body.get('id')
    if not mid:
        return jsonify({"error": "缺少 id"}), 400
    r = materialstore.delete_material(mid, uid=u.get('id'), is_admin=_is_admin(u))
    if not r.get('ok'):
        return jsonify(r), (r.get('code') or 400)
    return jsonify(r)


@app.route('/api/materials/process', methods=['POST'])
def api_materials_process():
    """素材加工入队：抠图(cutout) / 图生图(edit) / 拼版(stitch) —— GPU 队列，看板可见"""
    u = current_user() or {}
    body = request.get_json(silent=True) or {}
    action = (body.get('action') or '').strip()
    ids = body.get('ids') or []
    params = body.get('params') or {}
    if action not in ('cutout', 'edit', 'stitch'):
        return jsonify({"error": "未知的加工类型"}), 400
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "请先选择素材"}), 400
    rows = [r for r in (materialstore.get_material(i) for i in ids) if r]
    if len(rows) != len(ids):
        return jsonify({"error": "部分素材不存在或已删除，请刷新"}), 400
    uid = u.get('id')
    if not _is_admin(u):                 # 越权保护：只能加工自己的（admin 豁免）
        for r in rows:
            own = r.get('owner_id')
            try:
                same = own is not None and int(own) == int(uid)
            except Exception:
                same = False
            if not same:
                return jsonify({"error": "只能加工自己的素材"}), 403
    err = materialstore.validate(action, rows, params)
    if err:
        return jsonify({"error": err}), 400
    cfg = get_comfy_config()
    if not cfg.get('comfy_instance_uuid') or not cfg.get('comfy_api_token'):
        return jsonify({"error": "未配置应用实例 UUID / Token，请去「设置」页填写"}), 400
    payload = {"ids": [int(r['id']) for r in rows], "params": params}
    total = 1 if action == 'stitch' else len(rows)
    jid, reused = jobstore.DISPATCHER.enqueue(action, None, payload, total, priority=10,
                                             owner=user_label(u), owner_id=uid)
    return jsonify({"ok": True, "job_id": jid, "reused": reused,
                    "queue_pos": jobstore.queue_pos(jid)})

# ============================================================
# 启动
# ============================================================
if __name__ == '__main__':
    print("🚀 自媒体视频创作平台启动")
    print(f"   地址: http://localhost:3010")
    print(f"   按 Ctrl+C 停止")
    app.config['TEMPLATES_AUTO_RELOAD'] = True      # 只改模板时不用重启（减少打断任务）
    app.run(host='0.0.0.0', port=3010, debug=False, threaded=True)   # 生产环境：关掉 debug/热重载
