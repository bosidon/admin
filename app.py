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
from flask import Flask, render_template, jsonify, request, g, redirect

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
                     load_plan_prompt, load_plan_prompt_raw, save_plan_prompt)

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

if not os.environ.get('TEST_DATABASE'):
    init_db()

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
    return render_template("settings.html")

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
    """文章列表"""
    db = get_content_db()
    status = request.args.get('status')
    if status:
        rows = db.execute(
            'SELECT * FROM articles WHERE status=? ORDER BY created_at DESC', (status,)
        ).fetchall()
    else:
        rows = db.execute('SELECT * FROM articles ORDER BY created_at DESC').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/articles/<int:article_id>')
def api_get_article(article_id):
    """文章详情"""
    db = get_content_db()
    row = db.execute('SELECT * FROM articles WHERE id=?', (article_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(row))

@app.route('/api/articles', methods=['POST'])
def api_create_article():
    """创建文章（Job 或手动）"""
    data = request.json
    db = get_content_db()
    content_md = data.get('content_md', '')
    word_count = len(content_md.replace(' ', '').replace('\n', ''))
    cursor = db.execute('''
        INSERT INTO articles (title, book, topic, angle, structure, hook, tone,
                              content_md, content_html, summary, tags, word_count,
                              status, source, file_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    ))
    db.commit()
    return jsonify({"ok": True, "id": cursor.lastrowid})

@app.route('/api/articles/<int:article_id>', methods=['PUT'])
def api_update_article(article_id):
    """更新文章（状态/内容）"""
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
        {'id': 'xhs_note', 'name': '小红书笔记'},
        {'id': 'short_video', 'name': '短视频脚本'},
        {'id': 'long_video', 'name': '长视频脚本'},
        {'id': 'speech', 'name': '口播稿'},
        {'id': 'podcast_script', 'name': '播客脚本'},
        {'id': 'qa', 'name': '问答'},
    ]

    return jsonify({
        'platforms': platforms, 'content_types': content_types,
        'books': books, 'topics': topics, 'angles': angles,
        'structures': structures, 'hooks': hooks, 'tones': tones,
    })

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
    promo_uid = data.get('promo_uid') or ''          # 推广人 uid
    promo_src = data.get('promo_src') or ('c' + str(int(time.time()))[-6:])   # 内容来源码

    # 读品牌指引
    guide_path = Path("/home/bosidon/projects/social-media/gongzhonghao/AGENT_GUIDE.md")
    guide = guide_path.read_text()[:3000] if guide_path.exists() else ''

    # 根据平台+类型构建 prompt（字数由平台和类型共同决定）
    type_specs = {
        'article': {
            'wechat':    '1000-2000字公众号长文',
            'xiaohongshu': '500-1000字图文笔记',
            'video_account': '800-1500字视频文案',
            'douyin':    '300-500字短视频文案',
            'bilibili':  '1500-3000字深度长文',
        },
        'xhs_note': {
            'xiaohongshu': '300-800字小红书笔记，多用emoji，末尾加标签',
            '_default':   '300-800字短笔记，多用emoji，末尾加标签',
        },
        'short_video': {
            'wechat':    '对应60-180秒视频的分镜脚本',
            'video_account': '对应15-60秒视频的分镜脚本',
            'douyin':    '对应15-60秒视频的分镜脚本',
            'xiaohongshu': '对应30-90秒视频的分镜脚本',
            'bilibili':  '对应1-3分钟视频的分镜脚本',
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
        'xhs_note':   'Markdown，多用emoji，末尾加标签',
        'short_video': '分镜格式，每段标注【画面】【台词】【时长】',
        'long_video':  '分镜格式，每段标注【画面】【台词】【时长】【转场】',
        'speech':     '口语化，标注语气停顿和重音',
    }
    type_label = {
        'article': '图文文章', 'xhs_note': '小红书笔记',
        'short_video': '短视频脚本', 'long_video': '长视频脚本', 'speech': '口播稿',
    }

    spec_group = type_specs.get(content_type, type_specs['article'])
    word_spec = spec_group.get(platform, spec_group.get('_default', '1000-2000字'))
    fmt = type_format.get(content_type, 'Markdown格式')
    label = type_label.get(content_type, content_type)

    prompt = f"""你是「仙宝心灵成长」的专职内容创作Agent。

## 任务
生成一篇「{dict(wechat='公众号',xiaohongshu='小红书',video_account='视频号',douyin='抖音',bilibili='B站').get(platform, platform)}」平台的{label}。

## 参数
- 自媒体平台：{dict(wechat='公众号',xiaohongshu='小红书',video_account='视频号',douyin='抖音',bilibili='B站').get(platform, platform)}
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

    llm = get_llm_config()
    api_key = llm['llm_api_key']
    if not api_key:
        return jsonify({"error": "未配置 LLM API Key"}), 400

    try:
        resp = requests.post(llm['llm_base_url'],
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": llm['llm_model'], "messages": [{"role": "user", "content": prompt}]},
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

        # ===== 自动嵌入推广链接 =====
        promo_link = ''
        if promo_uid:
            promo_link = 'https://xianbao.love/?ref=%s&src=%s' % (promo_uid, promo_src)
            guide_line = '\n\n---\n\n🌙 想了解更多心灵成长内容？\n👉 ' + promo_link
            if promo_link not in content_md:
                content_md = content_md.rstrip() + guide_line

        word_count = len(content_md.replace(' ', '').replace('\n', ''))

        db = get_content_db()
        cursor = db.execute('''
            INSERT INTO articles (title, platform, content_type, book, topic, angle,
                                  structure, hook, tone, content_md, summary, tags,
                                  word_count, status, source, promo_uid, promo_src, promo_link)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'ai', ?, ?, ?)
        ''', (
            article_data.get('title', '未命名'),
            platform, content_type,
            book, topic, angle, structure, hook, tone,
            content_md,
            article_data.get('summary', ''),
            article_data.get('tags', ''),
            word_count,
            promo_uid or None, promo_src, promo_link or None,
        ))
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
    rows = db.execute('''
        SELECT v.*, a.title as article_title, a.platform, a.content_type
        FROM video_plans v
        LEFT JOIN articles a ON v.article_id = a.id
        ORDER BY v.updated_at DESC, v.created_at DESC
        LIMIT 200
    ''').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/video-plans', methods=['POST'])
def api_create_video_plan():
    """为文案创建视频计划"""
    data = request.json or {}
    article_id = data.get('article_id')
    if not article_id:
        return jsonify({"error": "article_id 必填"}), 400
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
    return jsonify(dict(row))

@app.route('/api/video-plans/<int:plan_id>', methods=['PUT'])
def api_update_video_plan(plan_id):
    """保存脚本 / 分镜 / 状态"""
    data = request.json or {}
    db = get_content_db()
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
    return render_template('video_plan.html', plan_id=plan_id)

# ============================================================
# API - 配图（阶段2：二维码 / 素材选用 / AI 生图）
# ============================================================

GEN_DIR = BASE_DIR / 'static' / 'generated'
GEN_DIR.mkdir(parents=True, exist_ok=True)


@app.route('/api/articles/<int:article_id>/images', methods=['GET'])
def api_get_article_images(article_id):
    """获取文案的配图列表"""
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
    db = get_content_db()
    try:
        rows = db.execute(
            'SELECT name, kind, prompt, neg, style, seed, steps, cfg, sampler, scheduler, '
            'width, height, created_at FROM illustration_logs WHERE article_id=? '
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
    return render_template('illustrate.html', article_id=article_id)

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
    return jsonify({"ok": True, "plan": read_plan(article_id)})


@app.route('/api/articles/<int:article_id>/image-script', methods=['POST'])
def api_save_image_script(article_id):
    """保存配图方案"""
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
    row = get_content_db().execute('SELECT platform FROM articles WHERE id=?',
                                   (int(article_id),)).fetchone()
    card_size, card_want = PLATFORM_CARD.get(
        (row['platform'] if row else '') or 'wechat', ('xiaohongshu', 3))
    opts = {"cards": bool(data.get('cards')),
            "scenes": bool(data.get('scenes')),
            "replan": bool(data.get('replan')),
            "plan_only": bool(data.get('plan_only'))}
    return jsonify(start_generate(int(article_id), opts, get_llm_config(),
                                  card_size, card_want, int(data.get('count') or 3)))


@app.route('/api/illustrate/job/<job_id>')
def api_illustrate_job(job_id):
    """查询生成任务进度"""
    j = get_job(job_id)
    if not j:
        return jsonify({"error": "任务不存在或已过期"}), 404
    return jsonify(dict(ok=True, **j))


@app.route('/api/illustrate/rewrite-item', methods=['POST'])
def api_rewrite_item():
    """只让 LLM 重写某一条的 bg/prompt（纯 LLM · 不开机 · 不整组重跑）"""
    data = request.json or {}
    article_id, kind, index = data.get('article_id'), data.get('kind'), data.get('index')
    if not article_id or kind not in ('quote', 'scene') or index is None:
        return jsonify({"error": "参数不完整（article_id / kind / index）"}), 400
    try:
        index = int(index)
        article_id = int(article_id)
    except Exception:
        return jsonify({"error": "index / article_id 必须是数字"}), 400
    item, err = rewrite_plan_item(article_id, kind, index,
                                  data.get('hint') or '', get_llm_config())
    if err:
        return jsonify({"error": err})
    return jsonify({"ok": True, "item": item, "kind": kind, "index": index})


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
    art = db.execute('SELECT promo_uid, promo_src, promo_link, images_json '
                     'FROM articles WHERE id=?', (article_id,)).fetchone()
    if not art:
        return jsonify({"error": "文案不存在"}), 404
    if art['promo_link']:
        link = art['promo_link']
    elif art['promo_uid']:
        link = 'https://xianbao.love/?ref=%s&src=%s' % (
            art['promo_uid'], art['promo_src'] or ('c%d' % article_id))
    else:
        link = None
    if not link:
        return jsonify({"error": "该文案没有推广链接（请先在文案里填写推广人 ID）"}), 400
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
    """素材库（materials 表：私有 + 平台共享）"""
    library_type = request.args.get('type', 'all')     # all / image / audio / video / template
    scope = request.args.get('scope', 'all')           # all / private / shared
    db = get_db()
    where = ["status = 'approved'"]
    params = []
    if library_type != 'all':
        where.append('type = ?')
        params.append(library_type)
    if scope != 'all':
        where.append('scope = ?')
        params.append(scope)
    rows = db.execute(
        'SELECT * FROM materials WHERE ' + ' AND '.join(where) + ' ORDER BY created_at DESC LIMIT 200',
        params
    ).fetchall()
    return jsonify([dict(r) for r in rows])

# ============================================================
# 启动
# ============================================================
if __name__ == '__main__':
    print("🚀 自媒体视频创作平台启动")
    print(f"   地址: http://localhost:3010")
    print(f"   按 Ctrl+C 停止")
    app.run(host='0.0.0.0', port=3010, debug=True)
