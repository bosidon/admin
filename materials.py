# -*- coding: utf-8 -*-
"""素材库（materials 表）：入库 / 回填 / 上传 / 加工工作流构建

职责边界
- 本模块只管「素材」本身：建表、入库（幂等）、历史回填、上传落盘、删除、查询
- 以及三个加工动作的 **ComfyUI 工作流构建（纯函数，无副作用，可单测）**
- GPU 执行（开机/提交/等待/关机）在 autogen.py 的 run_material_job 里做 —— 本模块不碰网络

设计要点
- file_path 存 URL 相对路径（/static/generated/...），前端直接 <img src>
- 幂等：同一 file_path 只留一行（配图固定文件名天然不重复；加工产物带参数短哈希）
- 只增不删：配图产物/failure 都不清空已有素材
- 加工产物另存 static/generated/materials/<文章ID>/，与配图目录隔离，互不干扰
"""
import os
import sqlite3
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
# 与 app.py 同规则：测试可用 TEST_DATABASE 隔离，避免误写生产库
DATABASE = os.environ.get("TEST_DATABASE") or str(DATA_DIR / "database.db")
CONTENT_DB = str(DATA_DIR / "content.db")
GEN_DIR = BASE_DIR / "static" / "generated"
UPLOAD_DIR = GEN_DIR / "uploads"
URL_PREFIX = "/static/generated"

IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")
VIDEO_EXT = (".mp4", ".mov", ".webm", ".mkv", ".avi")
MAX_IMAGE_SIDE = 2048          # 上传图片压缩：最长边上限
JPEG_QUALITY = 86

LINE_LABEL = {"lingxiu": "灵性书籍", "maya": "玛雅天赋", "tarot": "塔罗", "psych": "心理咨询"}

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS materials (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id  INTEGER,
    type        TEXT NOT NULL DEFAULT 'image',
    name        TEXT,
    category    TEXT,
    file_path   TEXT,
    source      TEXT DEFAULT 'ai',
    scope       TEXT DEFAULT 'private',
    status      TEXT DEFAULT 'approved',
    tags        TEXT,
    created_at  DATETIME DEFAULT (datetime('now','localtime')),
    owner_id    INTEGER,
    owner       TEXT DEFAULT ''
);
"""

# ---------- 加工动作的选项（服务端下发，前端不硬编码）----------
# 抠图模型 → 用哪个节点（RMBG / BiRefNetRMBG 两个节点的 model 枚举不同）
CUTOUT_MODELS = [
    {"id": "RMBG-2.0", "node": "RMBG", "label": "RMBG-2.0（通用最稳）"},
    {"id": "BEN2", "node": "RMBG", "label": "BEN2"},
    {"id": "INSPYRENET", "node": "RMBG", "label": "InSPyReNet（发丝细节）"},
    {"id": "BiRefNet-general", "node": "BiRefNetRMBG", "label": "BiRefNet 通用"},
    {"id": "BiRefNet-portrait", "node": "BiRefNetRMBG", "label": "BiRefNet 人像"},
    {"id": "BiRefNet-matting", "node": "BiRefNetRMBG", "label": "BiRefNet 精细抠像"},
]
CUTOUT_MODEL_NODE = {m["id"]: m["node"] for m in CUTOUT_MODELS}
BACKGROUNDS = [{"id": "Alpha", "label": "透明背景"}, {"id": "Color", "label": "纯色背景"}]

# 编辑 = 4 个功能（互斥）：换背景 / 换服饰 / 换角度 / 换风格
# 后 3 个（含换风格本身）都可再叠加一个「画风」LoRA（见 EDIT_STYLES）
EDIT_MODES = [
    {"id": "bg", "label": "换背景"},
    {"id": "dress", "label": "换服饰"},
    {"id": "angle", "label": "换角度"},
    {"id": "style", "label": "换风格"},
]
EDIT_PRESETS = {
    "bg": "把主体完整保留，替换背景为：{text}。保持主体光线、质感、比例不变。",
    "dress": "严格保持人物长相、发型、姿态与背景不变，只把服装换成：{text}。保持光线与画面质感不变。",
    "angle": "保持主体（人物）长相与服装完全一致，改为以下视角重新呈现：{text}。",
    "style": "保持主体与构图不变，{text}",
}
EDIT_MODES_REQUIRE_STYLE = ("style",)      # 只有「换风格」必须选画风

STITCH_MODES = [{"id": "grid3", "label": "九宫格 3×3"}, {"id": "column", "label": "长图竖拼"}]
STITCH_RES = [1080, 1440, 2048]
STITCH_PADS = [0, 10, 20, 40]
STITCH_MAX = 9                 # 长图最多 9 张

# 图生图 / 首尾帧的文本编码节点：槽位 = 主体图 1 + 参考图 N（节点能力，不许越界）
EDIT_ENCS = {
    "core": {"node": "TextEncodeQwenImageEditPlus",          "slots": 3, "ref_cap": 2,
             "names": ["image1", "image2", "image3"]},
    # lrz5 有 5 个槽，但实测喂满 5 张（1 主体 + 4 参考）会出点块状伪影、场景与人数全丢
    # → 参考额度按实测封在 3（即总输入 4 张），槽位够不等于能用
    "lrz5": {"node": "TextEncodeQwenImageEditPlus_lrzjason",  "slots": 5, "ref_cap": 3,
             "names": ["image1", "image2", "image3", "image4", "image5"]},
    # 注：TextEncodeQwenImageEditPlusAdvance_lrzjason 看着有 6 个图槽，实际源码把 vae_images 硬解包成 3 个输出
    #     （nodes.py: o_image1,o_image2,o_image3 = vae_images）→ 喂第 4 张就报
    #     "too many values to unpack (expected 3)"（已真机复现）。它不是 6 槽，只是 3 槽 + 摆位选择 → 不采用。
}


def edit_enc(enc="auto", n=0):
    """选文本编码节点：core(3 槽) / lrz5(5 槽) / lrz6(Advance 6 槽)。
    auto = 输入图 ≤3 张用官方核心节点，>3 张用 5 槽变体；节点不存在时由实例能力校验（wf_needs）拦下"""
    mode = (enc or "auto").strip().lower()
    if mode not in EDIT_ENCS:
        mode = "core" if int(n or 0) <= 3 else "lrz5"
    d = EDIT_ENCS[mode]
    return mode, d["node"], d["slots"], list(d["names"]), d["ref_cap"]

# Qwen-Image-Edit 默认件（可用 settings 的 comfy_edit_unet / comfy_edit_lora / comfy_edit_steps 覆盖）
EDIT_UNET_DEFAULT = "qwen_image_edit_2511_fp8_e4m3fn.safetensors"
EDIT_LORA_DEFAULT = "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors"
ANGLE_LORA = "qwen-image-edit-2511-multiple-angles-lora.safetensors"
EDIT_CLIP_DEFAULT = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
EDIT_VAE_DEFAULT = "qwen_image_vae.safetensors"

# ── 分镜片段（首尾帧图生视频）：Wan2.1-I2V-14B-480P 原生节点链 ──
CLIP_UNET_DEFAULT = "Wan2_1-I2V-14B-480P_fp8_e4m3fn.safetensors"
CLIP_TEXT_DEFAULT = "umt5_xxl_fp16.safetensors"
CLIP_VAE_DEFAULT = "wan_2.1_vae.safetensors"
CLIP_FPS = 16
CLIP_MAX_LEN = 145                      # 单条上限（≈9 秒 @16fps）；再长要拆段
CLIP_SIZES = {"9:16": (480, 832), "16:9": (832, 480), "1:1": (640, 640),
              "4:3": (640, 480), "3:4": (480, 640)}

# ── 分镜片段 · H3 引擎（MiniMax-H3 首尾帧「音视频联合」模型，全池只有 6000D 装了它）──
# 模板 = 6000D `/root/zealman-app/dist/U02-minimax_h3_lightX2v首尾帧图生视频加速版V2.json`
# 已经入仓（wf/h3_u02_fl2v_v2.json）：整条链都在模板里，运行时只改输入，不手搓节点。
H3_WF_DEFAULT = "wf/h3_u02_fl2v_v2.json"
H3_FPS = 24                     # 模板固定 24fps；帧数由模板内的表达式按秒数换算（≡5 mod 17）
H3_MAX_SEC = 10.0               # 单条上限；实测 5s ≈ 421s（带 2× 超分）
H3_SIZES = {"9:16": (768, 1344), "16:9": (1344, 768), "1:1": (1024, 1024)}
H3_TPL_NODES = {"first": "114", "last": "128", "preset": "122", "prompt": "136",
                "vhs": "143", "supres": "144", "secs": "105:111", "seed": "105:15",
                "steps": "105:9", "lora": "148", "decode": "145"}



# 「风格化」：给编辑链路再追加一个编辑类 LoRA（纯文生图无效，必须走图生图）
# 实测（2026-09-20，同一输入图 + 同一提示词 + 系统编辑参数 4 步，逐张看图判定）：
#   ✅ manga1 漫画·厚涂 / manga2 漫画·清线 / chibi Q版手办 / qtoanime 转动画 / angle 多角度 —— 明显生效且画质可用
#   ❌ whitebg 白底转场景（实拍图与纯白底图两种输入都产出全噪点，LoRA 本身跑不出图）
#   ❌ realalpha 转真人（素材基本都是真人所拍/AI 写实图 → 挂上无任何变化；它面向的是动画输入）
#   ❌ pose 姿态跟随（无部位参考图时不生效）、putithere 物品放置（把场景抹成白底，非预期）
# 想加回来：真机各出一张确认可用后再加，别只按文件名猜
# 「画风」= 4 个实测可用的编辑类 LoRA（换风格时必选其一；换背景/换服饰/换角度时可选叠加一个）
# （「无」「多角度」已按用户口径去掉：无 = 不挂 LoRA；多角度 = 功能 3，且是同一个 LoRA）
EDIT_STYLES = [
    {"id": "manga1", "label": "漫画·厚涂", "lora": "20 (1qwen漫画1).safetensors", "prompt": "把画面转成日式漫画风格"},
    {"id": "manga2", "label": "漫画·清线", "lora": "20qwen漫画2.safetensors", "prompt": "把画面转成日式漫画风格"},
    {"id": "chibi", "label": "Q版手办", "lora": "to-chibi_v1.safetensors", "prompt": "把主体变成可爱的Q版手办形象"},
    {"id": "qtoanime", "label": "转动画", "lora": "kontext-qtorealanime.safetensors", "prompt": "把画面转成日式动画风格"},
]
EDIT_STYLE_MAP = {s["id"]: s for s in EDIT_STYLES}



# ============================================================
# 连接与建表
# ============================================================
def _conn():
    c = sqlite3.connect(DATABASE, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA busy_timeout=8000")
    except Exception:
        pass
    return c


def init():
    """建表（老库已存在则跳过）+ 轻量 ALTER 补 owner_id / owner（不动旧数据）"""
    c = _conn()
    c.executescript(TABLE_DDL)
    cols = {r[1] for r in c.execute("PRAGMA table_info(materials)")}
    for name, ddl in (("owner_id", "INTEGER"), ("owner", "TEXT DEFAULT ''")):
        if name not in cols:
            c.execute("ALTER TABLE materials ADD COLUMN %s %s" % (name, ddl))
    # 自愈：历史遗留的空串/0 归属 → NULL（否则这些素材「谁的都看不到」）；幂等
    c.execute("UPDATE materials SET owner_id=NULL WHERE owner_id IS NOT NULL "
              "AND (CAST(owner_id AS TEXT)='' OR CAST(owner_id AS TEXT)='0')")
    c.commit()
    c.close()
    return True


# ============================================================
# 工具
# ============================================================
def url_to_path(url):
    """素材 URL → 本地路径（strict：只接受 static/generated 下的相对路径）"""
    u = (url or "").split("?")[0]
    if not u.startswith(URL_PREFIX + "/"):
        return None
    try:
        p = (GEN_DIR / u[len(URL_PREFIX) + 1:]).resolve()
    except Exception:
        return None
    if not str(p).startswith(str(GEN_DIR.resolve())):
        return None
    return p


THUMB_SIDE = 360                # 素材库网格缩略图最长边


def thumb_path_of(src_path):
    """缩略图缓存路径：<原目录>/.thumbs/<名>_t.jpg"""
    p = Path(src_path)
    return p.parent / ".thumbs" / (p.stem + "_t.jpg")


def ensure_thumb(src_path):
    """生成/复用缩略图（源图更新则重建）。失败返回 None（前端回落原图）

    为什么必须有：配图是 1~2MB 的大 PNG，网格直接加载原图会把 HTTP 连接占满、
    整页几十 MB —— 缩略图 360px 约 30KB，快 50 倍。"""
    try:
        p = Path(src_path)
        if not p.is_file() or p.suffix.lower() not in IMG_EXT:
            return None
        t = thumb_path_of(p)
        if t.is_file() and t.stat().st_mtime >= p.stat().st_mtime:
            return t
        from PIL import Image
        im = Image.open(str(p))
        im.load()
        w, h = im.size
        s = min(1.0, float(THUMB_SIDE) / float(max(w, h) or 1))
        if s < 1:
            im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            base = Image.new("RGB", im.size, (255, 255, 255))     # 透明底铺白（否则 JPEG 变黑块）
            base.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
            im = base
        else:
            im = im.convert("RGB")
        t.parent.mkdir(parents=True, exist_ok=True)
        im.save(str(t), "JPEG", quality=82, optimize=True)
        return t
    except Exception:
        return None


def _norm_uid(v):
    """归属 ID 归一化：'' / 0 / None / 非数字 → None（老任务表里 owner_id 可能是空串，
    若原样写进素材表，那张素材会变成「谁的都看不到」）"""
    try:
        if v is None or str(v).strip() == "":
            return None
        n = int(v)
        return n if n > 0 else None
    except Exception:
        return None


def _count():
    c = _conn()
    n = c.execute("SELECT COUNT(*) FROM materials").fetchone()[0]
    c.close()
    return int(n)


def _article_meta(article_id):
    """(标题, 业务线) —— 从 content.db 取；取不到不报错"""
    try:
        c = sqlite3.connect(CONTENT_DB, timeout=10)
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT title, service_line FROM articles WHERE id=?",
                      (int(article_id),)).fetchone()
        c.close()
        if r:
            return (r["title"] or "", r["service_line"] or "")
    except Exception:
        pass
    return ("", "")


def _job_owner(article_id):
    """历史素材归属：从 jobs 表找这篇文章最近一次出图任务的人（可能没有）"""
    try:
        c = _conn()
        r = c.execute("SELECT owner_id, owner FROM jobs WHERE article_id=? AND owner_id IS NOT NULL"
                      " ORDER BY created_at DESC LIMIT 1", (int(article_id),)).fetchone()
        c.close()
        if r:
            oid = _norm_uid(r["owner_id"])
            return (oid, (r["owner"] or "") if oid else "")
    except Exception:
        pass
    return (None, "")


def article_owner(article_id):
    """(owner_id, owner) —— 文案归属（content.db articles）；老数据无归属 → (None, '')"""
    try:
        c = sqlite3.connect(CONTENT_DB, timeout=10)
        r = c.execute("SELECT owner_id, owner FROM articles WHERE id=?", (int(article_id),)).fetchone()
        c.close()
        if r:
            return (_norm_uid(r[0]), (r[1] or ""))
    except Exception:
        pass
    return (None, "")


def article_owner_uid(article_id):
    """文案归属 uid；老数据无归属 → None"""
    return article_owner(article_id)[0]


def can_view_url(url, uid):
    """按素材 URL 判断该用户能否查看（admin 由调用方豁免）"""
    url = (url or "").split("?")[0]
    if not url:
        return False
    uid = _norm_uid(uid)
    try:
        c = _conn()
        r = c.execute("SELECT owner_id, article_id FROM materials WHERE file_path=?", (url,)).fetchone()
        c.close()
    except Exception:
        r = None
    if r:
        own = _norm_uid(r["owner_id"])
        if own is None:                       # 老数据无归属 → 仅 admin（上层已放行）
            return False
        return own == uid
    # 未入库的文件（新生成还没 sync / 缩略图缓存）→ 按文案归属判定
    parts = url.split("/")
    if len(parts) >= 4 and parts[1] == "static" and parts[2] == "generated" and parts[3].isdigit():
        return article_owner_uid(parts[3]) == uid
    return False


def _label(title, stem):
    t = (title or "").strip()
    if not t:
        return stem
    return (t[:26] + "…" if len(t) > 26 else t) + " · " + stem


def kind_of(filename):
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in IMG_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    return ""


# ============================================================
# 入库 / 查询 / 删除
# ============================================================
def upsert(file_path, mtype="image", name="", category="", source="ai", scope="private",
           owner_id=None, owner="", article_id=None, tags=""):
    """按 file_path 幂等入库：已有则只更新可读信息（不新增行）

    归属规则：**有文案的素材（配图产物 / 基于配图的加工产物）一律跟随文案归属**；
    只有无文案的上传 / 独立加工才归操作人。这样素材库归属永远和文案一致。"""
    c = _conn()
    owner_id = _norm_uid(owner_id)
    if article_id:                              # 跟随文案归属（改文案归属后可重新 sync 自愈）
        _aoid, _aonm = article_owner(article_id)
        if _aoid:
            owner_id, owner = _aoid, _aonm
    row = c.execute("SELECT id FROM materials WHERE file_path=?", (file_path,)).fetchone()
    if row:
        c.execute("UPDATE materials SET type=?, name=?, category=?,"
                  " article_id=COALESCE(?, article_id),"
                  " owner_id=COALESCE(?, owner_id),"
                  " owner=CASE WHEN ? <> '' THEN ? ELSE owner END,"
                  " tags=?, created_at=datetime('now','localtime') WHERE id=?",
                  (mtype, name or "", category or "", article_id, owner_id,
                   owner or "", owner or "", tags or "", row["id"]))
        mid = row["id"]
    else:
        cur = c.execute("INSERT INTO materials (article_id,type,name,category,file_path,"
                        "source,scope,status,tags,owner_id,owner,created_at) "
                        "VALUES (?,?,?,?,?,?,?,'approved',?,?,?,datetime('now','localtime'))",
                        (article_id, mtype, name or "", category or "", file_path,
                         source or "ai", scope or "private", tags or "", owner_id, owner or ""))
        mid = cur.lastrowid
    c.commit()
    c.close()
    return mid


def get_material(mid):
    c = _conn()
    r = c.execute("SELECT * FROM materials WHERE id=?", (int(mid),)).fetchone()
    c.close()
    return dict(r) if r else None


def list_for(uid=None, mtype="all", scope="mine", limit=200, is_admin=False):
    """素材列表：mine = 只有自己的（老数据无归属 → 仅 admin）；shared = 平台共享（暂未启用）"""
    c = _conn()
    where, args = ["status = 'approved'"], []
    if scope == "shared":
        where.append("scope = 'shared'")
    elif is_admin:
        pass                                   # admin 看全部
    else:
        where.append("owner_id = ?")
        args.append(int(_norm_uid(uid) or 0))
    if mtype and mtype != "all":
        where.append("type = ?")
        args.append(mtype)
    rows = c.execute("SELECT * FROM materials WHERE " + " AND ".join(where) +
                     " ORDER BY created_at DESC, id DESC LIMIT ?", args + [int(limit)]).fetchall()
    c.close()
    return [dict(r) for r in rows]


def delete_material(mid, uid=None, is_admin=False):
    """删除素材：只允许删自己的（admin 豁免）；同时删磁盘文件"""
    c = _conn()
    r = c.execute("SELECT * FROM materials WHERE id=?", (int(mid),)).fetchone()
    if not r:
        c.close()
        return {"ok": False, "error": "素材不存在"}
    own = _norm_uid(r["owner_id"])
    if not is_admin and own != _norm_uid(uid):
        c.close()
        return {"ok": False, "error": "只能删除自己的素材", "code": 403}
    path = url_to_path(r["file_path"])
    removed = False
    try:
        if path and path.is_file():
            path.unlink()
            removed = True
    except Exception:
        pass
    c.execute("DELETE FROM materials WHERE id=?", (int(mid),))
    c.commit()
    c.close()
    return {"ok": True, "file_removed": removed, "name": r["name"] or ""}


# ============================================================
# 配图产物入库（自动 + 历史回填，均幂等）
# ============================================================
def sync_article_images(article_id, owner_id=None, owner=""):
    """把某篇文章的配图目录同步进素材库（只增不删）。返回入库文件数"""
    d = GEN_DIR / str(article_id)
    if not d.is_dir():
        return 0
    title, line = _article_meta(article_id)
    owner_id = _norm_uid(owner_id)
    if owner_id is None:                      # 没传 → 跟文案归属，再回落历史任务
        owner_id = article_owner_uid(article_id)
        if owner_id is not None:
            owner = ""
        else:
            owner_id, owner = _job_owner(article_id)
    n = 0
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.suffix.lower() not in IMG_EXT:
            continue
        upsert(URL_PREFIX + "/%d/%s" % (int(article_id), p.name), "image",
               name=_label(title, p.stem), category=line or "", source="ai",
               article_id=int(article_id), owner_id=owner_id, owner=owner, tags=p.name)
        n += 1
    return n


def backfill():
    """历史回填：扫 static/generated/<数字目录>/ 全部图片 → 入库（幂等，可重复跑）"""
    out = {"dirs": 0, "files": 0, "added": 0, "skipped": 0}
    before = _count()
    if not GEN_DIR.is_dir():
        return out
    for d in sorted(GEN_DIR.iterdir()):
        if not d.is_dir() or not d.name.isdigit():
            continue                      # uploads/ materials/ 等非配图目录跳过
        out["dirs"] += 1
        aid = int(d.name)
        title, line = _article_meta(aid)
        oid = article_owner_uid(aid)
        if oid is not None:
            onm = ""
        else:
            oid, onm = _job_owner(aid)
        for p in sorted(d.iterdir()):
            if not p.is_file():
                continue
            if p.suffix.lower() not in IMG_EXT:
                out["skipped"] += 1
                continue
            upsert(URL_PREFIX + "/%d/%s" % (aid, p.name), "image",
                   name=_label(title, p.stem), category=line or "", source="ai",
                   article_id=aid, owner_id=oid, owner=onm, tags=p.name)
            out["files"] += 1
    out["added"] = _count() - before
    return out


# ============================================================
# 上传（图片压缩后存；视频/音频原样存）
# ============================================================
def _save_image(fs, stem):
    """图片压缩落盘：最长边 ≤ MAX_IMAGE_SIDE；有透明通道存 PNG，否则存 JPEG"""
    from PIL import Image                     # 延迟导入：只有上传图片才需要
    try:
        im = Image.open(fs.stream)
        im.load()
    except Exception as e:
        raise ValueError("图片解析失败：%s" % str(e)[:80])
    w, h = im.size
    scale = float(MAX_IMAGE_SIDE) / float(max(w, h) or 1)
    if scale < 1:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    if alpha:
        out = UPLOAD_DIR / (stem + ".png")
        im.convert("RGBA").save(str(out), "PNG", optimize=True)
    else:
        out = UPLOAD_DIR / (stem + ".jpg")
        im.convert("RGB").save(str(out), "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out


def save_upload(fs, owner_id=None, owner=""):
    """保存一个上传文件 → 素材行 dict。类型不支持/过大 → raise ValueError"""
    fname = (fs.filename or "").strip()
    mtype = kind_of(fname)
    if not mtype:
        raise ValueError("不支持的文件类型：%s" % (fname or "?"))
    try:
        if fs.content_length and fs.content_length > 50 * 1024 * 1024:
            raise ValueError("文件超过 50MB")
    except ValueError:
        raise
    except Exception:
        pass
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stem = "u" + uuid.uuid4().hex[:12]
    if mtype == "image":
        out = _save_image(fs, stem)
    else:
        ext = os.path.splitext(fname)[1].lower()
        out = UPLOAD_DIR / (stem + ext)
        fs.save(str(out))
    url = URL_PREFIX + "/uploads/" + out.name
    mid = upsert(url, mtype, name=fname, source="upload",
                 owner_id=owner_id, owner=owner, tags=fname)
    return get_material(mid)


# ============================================================
# 加工产物命名（同名覆盖：同一素材 + 同一参数 = 同一文件，URL 恒定不产生重复卡）
# ============================================================
def out_name(action, src_url, params=None, extra=""):
    """ai_01.png + cutout(模型RMBG-2.0/透明) → ai_01_cut_3f2a.png"""
    stem = Path((src_url or "mat").split("/")[-1]).stem or "mat"
    tag = {"cutout": "cut", "edit": "edit", "stitch": "stitch"}.get(action, action)
    key = "|".join(sorted("%s=%s" % (k, v) for k, v in (params or {}).items()))
    h = ("%08x" % (abs(hash(key + "|" + extra)) & 0xFFFFFFFF))[:4]
    return "%s_%s_%s.png" % (stem, tag, h)


def out_dir(article_id=None):
    """加工产物目录：与配图目录隔离"""
    d = GEN_DIR / "materials" / str(article_id or 0)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============================================================
# 分镜首/尾帧：出图前的拼装（文 + 图）
# ============================================================
FRAME_SIZE = {"9:16": (768, 1344), "16:9": (1344, 768), "1:1": (1024, 1024)}


def frame_size(aspect=None):
    """首/尾帧画幅（决定图生图产物尺寸：图生图输出尺寸跟第 1 张输入图）"""
    return FRAME_SIZE.get((aspect or "9:16").strip(), FRAME_SIZE["9:16"])


def fit_cover(src_path, out_path, w, h):
    """按目标画幅 cover 裁切（不拉伸、不补边）：作为图生图的主体图，保证产物比例稳定"""
    from PIL import Image
    im = Image.open(str(src_path)).convert("RGB")
    sw, sh = im.size or (w, h)
    k = max(w / float(sw or 1), h / float(sh or 1))
    nw, nh = max(w, int(round(sw * k))), max(h, int(round(sh * k)))
    if (nw, nh) != (sw, sh):
        im = im.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    im = im.crop((left, top, left + w, top + h))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    im.save(str(out_path), "PNG", optimize=True)
    return str(out_path)


def stitch_refs(paths, out_path, cell=(416, 728), gap=8):
    """把多个人物素材拼成 1 张横向参考图（ComfyUI 的 Qwen-Image-Edit 节点最多吃 3 张图 =
    主体 1 + 参考 2，多人镜头靠拼图才能把全部人物的外观带进去）。
    单元格按 cover 裁切、不拉伸；顺序 = 调用方给定的出场顺序，与提词里的人物顺序一致。"""
    from PIL import Image
    ps = [p for p in (paths or []) if p][:4]
    if not ps:
        raise ValueError("拼图需要至少 1 张图")
    cw, ch = cell
    canvas = Image.new("RGB", (cw * len(ps) + gap * (len(ps) - 1), ch), (24, 24, 24))
    for i, p in enumerate(ps):
        im = Image.open(str(p)).convert("RGB")
        sw, sh = im.size or (cw, ch)
        k = max(cw / float(sw or 1), ch / float(sh or 1))
        nw, nh = max(cw, int(round(sw * k))), max(ch, int(round(sh * k)))
        if (nw, nh) != (sw, sh):
            im = im.resize((nw, nh), Image.LANCZOS)
        l, t = (nw - cw) // 2, (nh - ch) // 2
        canvas.paste(im.crop((l, t, l + cw, t + ch)), (i * (cw + gap), 0))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    canvas.save(str(out_path), "PNG", optimize=True)
    return str(out_path)


def frame_looks(reqs):
    """外观文字的唯一来源 = 素材需求行的 desc（短外观；不带「纯色背景/半身像」等素材图构图指令）——
    分镜本身不再复述外观（避免同一人物两套形象描述）"""
    parts = []
    for r in (reqs or []):
        d = (r.get("desc") or "").strip() or (r.get("prompt_en") or "").strip()
        nm = (r.get("name") or "").strip()
        if nm and d:
            parts.append("%s：%s" % (nm, d))
    return "；".join(parts)


def frame_prompt(shot, reqs, frame="start", aspect="9:16", style="",
                 main_label=None, ref_labels=None):
    """首帧/尾帧出图提词（全中文，便于调试）：
    首帧 = 素材外观 + 起始画面（动作 + 运镜起幅）
    尾帧 = 同批素材外观 + 收尾状态 end_state + 「与首帧同人同景、只变机位与姿态」
    main_label / ref_labels = 实际送进模型的参考图「形态 + 顺序」（见 autogen._frame_target_rows）：
    分槽单图时写死「第 N 张是谁」，只有真的拼了图才提「拼图从左到右」——
    否则模型没有「谁对应谁」的依据，会把一个人的面部特征（如胡须）串到另一个人脸上（实测踩过）"""
    shot = shot or {}
    look = frame_looks(reqs)
    vis = (shot.get("visual") or "")
    persons, shows = [], []           # 本镜真正出场的人物 / 道具（按出场先后 = 参考拼图从左到右的顺序）
    for _r in (reqs or []):
        _nm = (_r.get("name") or "").strip()
        if not _nm or _nm not in vis:
            continue
        if (_r.get("kind") or "").strip() == "persona":
            persons.append(_nm)
        elif (_r.get("kind") or "").strip() == "prop":
            shows.append(_nm)
    persons.sort(key=lambda n: vis.find(n))
    shows.sort(key=lambda n: vis.find(n))
    kind = (shot.get("shot_type") or "中景").strip()
    move = (shot.get("camera_move") or "").strip()
    face = {"front": "正面朝向镜头", "side": "侧面朝镜头", "back": "背对镜头"}.get(
        (shot.get("shot_face") or "front").strip(), "")
    w, h = frame_size(aspect)
    p = ["电影感单帧画面，真人实拍质感，构图完整、主体清晰"]
    if look:
        p.append("画面中的人物、场景、道具必须与下列外观完全一致，不得改动长相、服装、场景与画风：" + look)
    if persons:
        _lbl = [l for l in (ref_labels or []) if isinstance(l, dict)]
        _strip = next((l for l in _lbl if l.get("kind") == "strip"), None)
        if _strip:
            _nm = [n for n in (_strip.get("names") or []) if n]
            p.append("画面中必须出现 %d 个人物：%s；参考拼图里的人像从左到右依次就是这 %d 个人（%s），"
                     "每个人都要清晰可分辨，不得增减人数、不得把两个人物合成一张脸"
                     % (len(persons), "、".join(persons), len(_nm) or len(persons),
                        "、".join(_nm) or "、".join(persons)))
        elif main_label or _lbl:
            pairs, _i = [], 0
            if main_label:
                _i = 1
                mk, mn = (main_label.get("kind") or ""), (main_label.get("name") or "")
                if mk == "persona":
                    pairs.append("第 1 张是人物「%s」" % mn)
                elif mk == "prev":
                    pairs.append("第 1 张是该镜的首帧画面（本帧底图，同人同景）")
                else:
                    pairs.append("第 1 张是场景「%s」（底图）" % mn)
            for l in _lbl:
                _i += 1
                k, n = (l.get("kind") or ""), (l.get("name") or "")
                if k == "persona":
                    pairs.append("第 %d 张是人物「%s」" % (_i, n))
                elif k == "prop":
                    pairs.append("第 %d 张是道具「%s」" % (_i, n))
                else:
                    pairs.append("第 %d 张是「%s」" % (_i, n))
            p.append("画面中必须出现 %d 个人物：%s；参考图与内容一一对应（%s），"
                     "每个人物只能照着自己那张参考图画长相、胡须、发型与服装，"
                     "禁止把另一个人物的胡须、眉毛、发际线等面部特征挪到这个人脸上，"
                     "也不得把两个人物合成一张脸，不得增减人数"
                     % (len(persons), "、".join(persons), "；".join(pairs)))
        else:
            p.append("画面中必须出现 %d 个人物：%s；每个人物只能照着自己那张参考图画长相、胡须、发型与服装，"
                     "禁止把一个人物的面部特征挪到另一个人脸上，也不得把两个人物合成一张脸，不得增减人数"
                     % (len(persons), "、".join(persons)))
    if shows:
        p.append("画面中必须出现的道具：" + "、".join(shows))
    if frame == "end":
        body = (shot.get("end_state") or "").strip().rstrip("。.；; ") or "延续同一动作的收尾姿态，动作刚完成"
        p.append("这是同一镜头连续画面里的「最后一帧」：" + body)
        p.append("必须与参考图（该镜首帧画面）保持同一人物、同一服装、同一场景、同一画面轴向，"
                 "只改变机位距离与角度以及该时刻的姿态，能和首帧自然衔接")
    else:
        body = (shot.get("visual") or "").strip().rstrip("。.；; ") or "镜头开始的静态画面"
        p.append("这是镜头的「第一帧」（起始画面）：" + body)
    if kind:
        p.append("景别：" + kind)
    if move:
        p.append("镜头运动倾向（决定构图透视）：" + move)
    if face:
        p.append("人物朝向：" + face)
    if (style or "").strip():
        p.append("画风：" + style.strip())
    p.append("画面比例 %s（%d×%d），高清细节" % (aspect, w, h))
    p.append("不要文字、不要字幕、不要水印、不要边框、不要拼贴、不要多手多指")
    return "。".join(p)


# ============================================================
# ComfyUI 工作流构建（纯函数）
# ============================================================
def build_cutout_wf(img, model="RMBG-2.0", background="Alpha", color="#FFFFFF",
                    prefix="mat_cut"):
    """抠图：LoadImage → RMBG/BiRefNetRMBG → (透明:JoinImageWithAlpha) → SaveImage"""
    node = CUTOUT_MODEL_NODE.get(model) or "RMBG"
    # mask_offset 在 object_info 里是可选，但节点代码会直接取它 → 不传就报
    # 「Error in image processing: 'mask_offset'」（实测踩过）
    ins = {"image": ["1", 0], "model": model, "mask_blur": 0, "mask_offset": 0,
           "invert_output": False, "refine_foreground": False, "background": background}
    if node == "RMBG":
        ins.update({"sensitivity": 1.0, "process_res": 1024})
    if background == "Color":
        ins["background_color"] = color or "#FFFFFF"
    # ⚠️ RMBG / BiRefNetRMBG 的**输出 0 就是抠好的图**：
    #   background='Alpha' → 已是带透明通道的图；background='Color' → 已合成好底色。
    #   别再叠 JoinImageWithAlpha（实测把极性弄反：人物变透明、背景留下）。
    wf = {"1": {"class_type": "LoadImage", "inputs": {"image": img}},
          "2": {"class_type": node, "inputs": ins},
          "3": {"class_type": "SaveImage",
                "inputs": {"filename_prefix": prefix, "images": ["2", 0]}}}
    return wf


def build_edit_wf(imgs, prompt, neg="", seed=0, cfg=None, prefix="mat_edit",
                  use_angle_lora=False, style="", enc="auto"):
    """图生图（Qwen-Image-Edit 2511 + Lightning 4步）：
    LoadImage×N → 文本编码节点(带参考图) → ReferenceLatent → KSampler → SaveImage
    enc = core(3 槽) / lrz5(5 槽) / lrz6(6 槽) / auto（按输入图数自动挑）"""
    cfg = cfg or {}
    unet = cfg.get("comfy_edit_unet") or EDIT_UNET_DEFAULT
    lora = cfg.get("comfy_edit_lora") or EDIT_LORA_DEFAULT
    clip_name = cfg.get("comfy_clip") or EDIT_CLIP_DEFAULT
    vae = cfg.get("comfy_vae") or EDIT_VAE_DEFAULT
    try:
        steps = int(cfg.get("comfy_edit_steps") or 4)
    except Exception:
        steps = 4
    try:
        cfgv = float(cfg.get("comfy_edit_cfg") or 1.0)
    except Exception:
        cfgv = 1.0
    sampler = cfg.get("comfy_edit_sampler") or "euler"
    scheduler = cfg.get("comfy_edit_scheduler") or "simple"

    wf = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": clip_name, "type": "qwen_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
    }
    model_ref, clip_ref = ["1", 0], ["2", 0]
    nid = 4
    wf["4"] = {"class_type": "LoraLoader",
               "inputs": {"lora_name": lora, "strength_model": 1.0, "strength_clip": 1.0,
                          "model": model_ref, "clip": clip_ref}}
    model_ref, clip_ref = ["4", 0], ["4", 1]
    nid = 5
    if use_angle_lora:
        wf[str(nid)] = {"class_type": "LoraLoader",
                        "inputs": {"lora_name": ANGLE_LORA, "strength_model": 1.0,
                                   "strength_clip": 1.0, "model": model_ref, "clip": clip_ref}}
        model_ref, clip_ref = [str(nid), 0], [str(nid), 1]
        nid += 1
    _st = EDIT_STYLE_MAP.get(style or "") or {}      # 「风格化」LoRA（串在加速 LoRA 之后）
    if _st.get("lora"):
        wf[str(nid)] = {"class_type": "LoraLoader",
                        "inputs": {"lora_name": _st["lora"], "strength_model": 1.0,
                                   "strength_clip": 1.0, "model": model_ref, "clip": clip_ref}}
        model_ref, clip_ref = [str(nid), 0], [str(nid), 1]
        nid += 1
    _enc_mode, _enc_node, _enc_slots, _enc_names, _enc_refcap = edit_enc(enc, len(imgs or []))
    img_refs = []
    for nm in (imgs or [])[:_enc_slots]:
        wf[str(nid)] = {"class_type": "LoadImage", "inputs": {"image": nm}}
        img_refs.append([str(nid), 0])
        nid += 1
    if not img_refs:
        raise ValueError("图生图至少需要 1 张输入图")
    wf[str(nid)] = {"class_type": "VAEEncode",
                    "inputs": {"pixels": img_refs[0], "vae": ["3", 0]}}
    lat_ref = [str(nid), 0]
    nid += 1
    pos = {"class_type": _enc_node,
           "inputs": {"clip": clip_ref, "prompt": prompt, "vae": ["3", 0]}}
    neg_in = {"class_type": _enc_node,
              "inputs": {"clip": clip_ref, "prompt": neg or "", "vae": ["3", 0]}}
    for i, ref in enumerate(img_refs):
        if i >= len(_enc_names):
            break
        pos["inputs"][_enc_names[i]] = ref          # lrz6 的槽位名不是 imageN，按登记表映射
        neg_in["inputs"][_enc_names[i]] = ref
    wf[str(nid)] = pos
    pos_id = str(nid)
    nid += 1
    wf[str(nid)] = neg_in
    neg_id = str(nid)
    nid += 1
    wf[str(nid)] = {"class_type": "ReferenceLatent",
                    "inputs": {"conditioning": [pos_id, 0], "latent": lat_ref}}
    pos_ref = [str(nid), 0]          # ⚠️ 必须是 [节点id, 输出序号]，写成字符串 "9"
    nid += 1                        #    会原样传给 KSampler → 「string index out of range」
    wf[str(nid)] = {"class_type": "ReferenceLatent",
                    "inputs": {"conditioning": [neg_id, 0], "latent": lat_ref}}
    neg_ref = [str(nid), 0]
    nid += 1
    wf[str(nid)] = {"class_type": "KSampler",
                    "inputs": {"seed": int(seed or 0), "steps": steps, "cfg": cfgv,
                               "sampler_name": sampler, "scheduler": scheduler,
                               "denoise": 1.0, "model": model_ref,
                               "positive": pos_ref, "negative": neg_ref,
                               "latent_image": lat_ref}}
    ks = str(nid)
    nid += 1
    wf[str(nid)] = {"class_type": "VAEDecode", "inputs": {"samples": [ks, 0], "vae": ["3", 0]}}
    dec = str(nid)
    nid += 1
    wf[str(nid)] = {"class_type": "SaveImage",
                    "inputs": {"filename_prefix": prefix, "images": [dec, 0]}}
    return wf


def build_stitch_wf(imgs, mode="grid3", res=1080, pad=0, prefix="mat_stitch"):
    """拼版：LoadImage×N → ImageBatchMulti → ImageGrid(九宫格) / ImageConcatFromBatch(长图) → SaveImage"""
    imgs = list(imgs or [])
    if len(imgs) < 2:
        raise ValueError("拼版至少需要 2 张素材")
    if mode == "grid3" and len(imgs) != 9:
        raise ValueError("九宫格需要正好 9 张素材（当前 %d 张）" % len(imgs))
    wf = {}
    n = len(imgs)
    for i, nm in enumerate(imgs, 1):
        wf[str(i)] = {"class_type": "LoadImage", "inputs": {"image": nm}}
    batch = {"class_type": "ImageBatchMulti", "inputs": {"inputcount": n}}
    for i in range(1, n + 1):
        batch["inputs"]["image_%d" % i] = [str(i), 0]
    wf[str(n + 1)] = batch
    try:
        res = int(res or 1080)
    except Exception:
        res = 1080
    if mode == "grid3":
        cell = max(64, res // 3)
        wf[str(n + 2)] = {"class_type": "ImageGrid",
                          "inputs": {"images": [str(n + 1), 0], "columns": 3,
                                     "cell_width": cell, "cell_height": cell,
                                     "padding": int(pad or 0)}}
    else:
        wf[str(n + 2)] = {"class_type": "ImageConcatFromBatch",
                          "inputs": {"images": [str(n + 1), 0], "num_columns": 1,
                                     "match_image_size": True, "max_resolution": res}}
    wf[str(n + 3)] = {"class_type": "SaveImage",
                      "inputs": {"filename_prefix": prefix, "images": [str(n + 2), 0]}}
    return wf


def clip_size(aspect=None):
    """片段的出图尺寸（Wan 480P 训练分辨率；比例按剧本 aspect）"""
    return CLIP_SIZES.get((aspect or "9:16").strip(), (480, 832))


def clip_length(duration_s, fps=CLIP_FPS, max_len=CLIP_MAX_LEN):
    """时长 → 帧数：Wan 要求 length 满足 4n+1，再夹到 [17, max_len]"""
    try:
        fps = int(fps or CLIP_FPS)
    except Exception:
        fps = CLIP_FPS
    if fps <= 0:
        fps = CLIP_FPS
    try:
        dur = float(duration_s or 0)
    except Exception:
        dur = 0.0
    if dur <= 0:
        dur = 3.0
    n = int(round(dur * fps))
    n -= (n - 1) % 4                       # 归到最近的 4n+1
    if n < 17:
        n = 17
    n = min(int(max_len or CLIP_MAX_LEN), n)
    return n - ((n - 1) % 4)               # 夹完再归一次，保证仍是 4n+1


def clip_prompt(shot, reqs=None, dur=5):
    """片段提词：动作 + 运镜 + 落点（外观由首帧图决定，不再用文字重复描述外观）"""
    shot = shot or {}
    p = []
    for k, pre in (("visual", ""), ("shot_type", "景别："), ("camera_move", "运镜：")):
        v = (shot.get(k) or "").strip()
        if v:
            p.append(pre + v)
    if (shot.get("line") or "").strip():
        p.append("此段台词（只做说话的口型动作，画面里不要出现字幕或文字）：" + shot["line"].strip())
    if (shot.get("end_state") or "").strip():
        p.append("镜头结束时：" + shot["end_state"].strip())
    p.append("人物的外貌、服装、发型与画面风格前后保持一致，动作连贯自然，中间不要跳变")
    try:
        p.append("整段约 %.0f 秒" % float(dur or 5))
    except Exception:
        pass
    p.append("不要文字、不要字幕、不要水印、不要边框、不要画面撕裂、不要人物变形")
    # 分镜字段自带句号 → 逐段去尾再拼，避免「。。」（同类修过 frame_prompt）
    return "。".join([x.strip().rstrip("。；;. ") for x in p if x and x.strip()])


def build_clip_wf(imgs, prompt, neg="", seed=0, cfg=None, prefix="shot_clip",
                  width=480, height=832, length=49, fps=CLIP_FPS):
    """首尾帧图生视频（ComfyUI 原生 Wan 节点链）：
    LoadImage(首/尾) → WanFirstLastFrameToVideo → KSampler → VAEDecode → VHS_VideoCombine(mp4)

    三个必对项（在别的实例上踩全过）：① length 必须 4n+1；② VHS 的 loop_count / pingpong /
    save_output 是必填；③ CLIPLoader 的 type 必须是 wan，且文本编码器要用**原生** umt5_xxl_fp16
    （umt5-xxl-enc-* 是 WanVideoWrapper 格式，核心 CLIPLoader 认不了 → KSampler 报矩阵形状错）"""
    cfg = cfg or {}
    imgs = list(imgs or [])
    if len(imgs) < 2:
        raise ValueError("首尾帧图生视频需要 2 张输入图（首帧 + 尾帧）")
    unet = cfg.get("comfy_clip_unet") or CLIP_UNET_DEFAULT
    clip_name = cfg.get("comfy_clip_text") or CLIP_TEXT_DEFAULT
    vae = cfg.get("comfy_clip_vae") or CLIP_VAE_DEFAULT
    try:
        steps = int(cfg.get("comfy_clip_steps") or 20)
    except Exception:
        steps = 20
    try:
        cfgv = float(cfg.get("comfy_clip_cfg") or 6.0)
    except Exception:
        cfgv = 6.0
    try:
        length = int(length or 49)
    except Exception:
        length = 49
    if (length - 1) % 4:
        length = length - ((length - 1) % 4)
    try:
        fps = int(fps or CLIP_FPS)
    except Exception:
        fps = CLIP_FPS
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": clip_name, "type": "wan"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt or "", "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": neg or "", "clip": ["2", 0]}},
        "6": {"class_type": "LoadImage", "inputs": {"image": imgs[0]}},
        "7": {"class_type": "LoadImage", "inputs": {"image": imgs[1]}},
        "8": {"class_type": "WanFirstLastFrameToVideo",
              "inputs": {"positive": ["4", 0], "negative": ["5", 0], "vae": ["3", 0],
                         "width": int(width or 480), "height": int(height or 832),
                         "length": length, "batch_size": 1,
                         "start_image": ["6", 0], "end_image": ["7", 0]}},
        "9": {"class_type": "KSampler",
              "inputs": {"seed": int(seed or 0), "steps": steps, "cfg": cfgv,
                         "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
                         "model": ["1", 0], "positive": ["8", 0], "negative": ["8", 1],
                         "latent_image": ["8", 2]}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "VHS_VideoCombine",
               "inputs": {"images": ["10", 0], "frame_rate": fps, "loop_count": 0,
                          "filename_prefix": prefix, "format": "video/h264-mp4",
                          "pingpong": False, "save_output": True,
                          "crf": 16, "pix_fmt": "yuv420p"}},
    }


def h3_size(aspect=None):
    return H3_SIZES.get((aspect or "9:16").strip(), (768, 1344))


def h3_seconds(duration_s, max_sec=H3_MAX_SEC):
    """H3 单条时长（秒）：按分镜时长，超上限就截到上限（模板里的表达式再换算成合法帧数）"""
    try:
        sec = float(duration_s or 0)
    except Exception:
        sec = 0.0
    if sec <= 0:
        sec = 5.0
    return round(min(float(max_sec or H3_MAX_SEC), max(3.0, sec)), 2)


def h3_template(cfg=None):
    """读 H3 模板（API 格式）并深拷一份：只留节点键，保留 subgraph 子图 id（105:14 这类，丢了必报
    KeyError）。路径可用设置项 comfy_h3_wf 覆盖（相对路径 = 项目根）。"""
    import copy, json, re
    cfg = cfg or {}
    p = str(cfg.get("comfy_h3_wf") or H3_WF_DEFAULT).strip()
    path = Path(p) if os.path.isabs(p) else (Path(__file__).resolve().parent / p)
    if not path.is_file():
        raise ValueError("H3 模板不存在：%s" % path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    wf = {k: v for k, v in raw.items() if re.match(r"^\d+(:\d+)*$", str(k))}
    if not wf:
        raise ValueError("H3 模板里没有节点（%s）" % path)
    return copy.deepcopy(wf)


def build_h3_wf(imgs, prompt, neg="", seed=0, cfg=None, prefix="shot_clip",
                width=768, height=1344, seconds=5.0, fps=H3_FPS, steps=0, lora=0.0,
                superres=-1):
    """H3 首尾帧出片段（MiniMax-H3 FL2V turbo；音视频联合 → 产物带音轨）
    模板整条链已固定：LoadImage(首/尾) → ImageResizeKJv2(lanczos crop 到 768×1344) →
    MiniMaxH3ImageToVideo(宽高来自 WJILatentPreset、时长来自 PrimitiveFloat 表达式) →
    SamplerCustomAdvanced(+H3SigmaRefiner) → VAEDecode/VAEDecodeAudio → 超分 → VHS_VideoCombine
    运行时只改 6 处输入：首帧 / 尾帧 / 宽高 / 秒数 / 提示词 / 输出前缀（+ 可选 seed、步数、LoRA 强度、超分倍数）"""
    cfg = cfg or {}
    imgs = list(imgs or [])
    if len(imgs) < 2:
        raise ValueError("H3 首尾帧出片段需要 2 张输入图（首帧 + 尾帧）")
    wf = h3_template(cfg)
    N = H3_TPL_NODES
    for k, tag in (("first", "首帧 LoadImage"), ("last", "尾帧 LoadImage"),
                   ("preset", "WJILatentPreset"), ("prompt", "CR Prompt Text"),
                   ("vhs", "VHS_VideoCombine"), ("secs", "PrimitiveFloat(秒)"),
                   ("seed", "RandomNoise(seed)")):
        if N[k] not in wf:
            raise ValueError("H3 模板缺少节点 %s（%s）——模板版本变了，请重新导出" % (N[k], tag))
    wf[N["first"]]["inputs"]["image"] = imgs[0]
    wf[N["last"]]["inputs"]["image"] = imgs[1]
    wf[N["preset"]]["inputs"]["自定义宽"] = int(width or 768)
    wf[N["preset"]]["inputs"]["自定义高"] = int(height or 1344)
    wf[N["prompt"]]["inputs"]["prompt"] = prompt or ""
    wf[N["secs"]]["inputs"]["value"] = float(seconds or 5.0)
    wf[N["seed"]]["inputs"]["noise_seed"] = int(seed or 0)
    wf[N["vhs"]]["inputs"]["filename_prefix"] = prefix
    wf[N["vhs"]]["inputs"]["frame_rate"] = int(fps or H3_FPS)
    if steps and N["steps"] in wf:
        wf[N["steps"]]["inputs"]["steps"] = int(steps)          # turbo LoRA 够用时 8 步
    if lora and N["lora"] in wf:
        wf[N["lora"]]["inputs"]["strength_model"] = float(lora)
    # 超分：默认沿用模板（2×）；显式传 1 或 0 = 旁路（把 VHS 的输入直接接到解码后，跳过超分）
    try:
        sr = int(superres)
    except Exception:
        sr = -1
    if sr >= 0:
        if sr <= 1:
            # 旁路超分：VHS 直接吃解码输出（145 = 解码后的清理节点），删掉 144/151
            if N["vhs"] in wf and N["decode"] in wf:
                wf[N["vhs"]]["inputs"]["images"] = [N["decode"], 0]
            wf.pop(N["supres"], None)
            wf.pop("151", None)
        elif N["supres"] in wf and "resize_type.scale" in wf[N["supres"]]["inputs"]:
            wf[N["supres"]]["inputs"]["resize_type.scale"] = sr
    return wf


def clip_prompt_h3(shot, reqs=None, dur=5):
    """H3 提词：H3 用 qwen3vl-32B 文本编码器，中文可直接用；<Picture 1>/<Picture 2> = 首帧/尾帧"""
    shot = shot or {}
    p = ["subject_definitions:",
         "<Picture 1> 是目标视频的第一帧，<Picture 2> 是最后一帧，两帧之间要自然连贯地过渡。"]
    if (shot.get("visual") or "").strip():
        p.append("画面内容：" + shot["visual"].strip())
    for k, pre in (("shot_type", "景别："), ("camera_move", "运镜：")):
        v = (shot.get(k) or "").strip()
        if v:
            p.append(pre + v)
    if (shot.get("line") or "").strip():
        p.append("此段台词（只做说话的口型动作，画面里不要出现字幕或文字）：" + shot["line"].strip())
    if (shot.get("end_state") or "").strip():
        p.append("镜头结束时：" + shot["end_state"].strip())
    p.append("保持 <Picture 1> 里人物的外貌、服装、发型、场景与画面风格前后一致，动作连贯自然，中间不要跳变")
    try:
        p.append("整段约 %.0f 秒" % float(dur or 5))
    except Exception:
        pass
    p.append("不要文字、不要字幕、不要水印、不要边框、不要画面撕裂、不要人物变形")
    return "\n".join([x.strip() for x in p if x and x.strip()])


def build_wf(action, imgs, params=None, cfg=None, prompt="", seed=0, prefix="mat"):
    """统一入口：action = cutout / edit / stitch / clip / h3clip"""
    params = params or {}
    if action == "cutout":
        return build_cutout_wf(imgs[0], model=params.get("model") or "RMBG-2.0",
                               background=params.get("background") or "Alpha",
                               color=params.get("color") or "#FFFFFF", prefix=prefix)
    if action == "edit":
        return build_edit_wf(imgs, prompt or params.get("instruction") or "",
                             neg=params.get("negative") or "", seed=seed, cfg=cfg,
                             prefix=prefix,
                             use_angle_lora=(params.get("mode") == "angle"),
                             style=params.get("style") or "",
                             enc=params.get("enc") or "auto")
    if action == "stitch":
        return build_stitch_wf(imgs, mode=params.get("mode") or "grid3",
                               res=params.get("res") or 1080,
                               pad=params.get("pad") or 0, prefix=prefix)
    if action == "clip":
        return build_clip_wf(imgs, prompt or params.get("prompt") or "",
                             neg=params.get("negative") or "", seed=seed, cfg=cfg, prefix=prefix,
                             width=params.get("width") or 480, height=params.get("height") or 832,
                             length=params.get("length") or 49,
                             fps=params.get("fps") or CLIP_FPS)
    if action == "h3clip":
        return build_h3_wf(imgs, prompt or params.get("prompt") or "",
                           neg=params.get("negative") or "", seed=seed, cfg=cfg, prefix=prefix,
                           width=params.get("width") or 768, height=params.get("height") or 1344,
                           seconds=params.get("seconds") or 5.0,
                           fps=params.get("fps") or H3_FPS,
                           steps=params.get("steps") or 0, lora=params.get("lora") or 0.0,
                           superres=params.get("superres", -1))
    raise ValueError("未知的加工类型：%s" % action)


_MODEL_FIELDS = ("unet_name", "clip_name", "vae_name", "lora_name", "ckpt_name", "model_name")


def wf_needs(wf):
    """从一份 workflow 里取出「要实例具备什么」：节点类型集合 + 模型名集合
    节点是硬判据（没装就是 HTTP 400）；模型只用于日志告警 —— 实例间有 fit_name
    「同名文件在不同子目录」的适配逻辑，硬判会把本来能跑的机器误杀"""
    nodes, models = set(), set()
    for n in (wf or {}).values():
        if not isinstance(n, dict):
            continue
        if n.get("class_type"):
            nodes.add(str(n["class_type"]))
        for k, v in (n.get("inputs") or {}).items():
            if k in _MODEL_FIELDS and isinstance(v, str) and v.strip():
                models.add(v.strip())
    return {"nodes": sorted(nodes), "models": sorted(models)}


def needs_for(action, params=None, cfg=None, prompt="", n_imgs=1):
    """该加工动作需要的节点/模型（用占位图名调同一个 build_wf：纯内存、不联网、不上机器）"""
    params = params or {}
    n = max(1, int(n_imgs or 1))
    # 拼版/片段对张数有硬要求（九宫格=9；首尾帧视频=2）
    tries = [n] + ([9, 4, 2] if action == "stitch"
                    else ([2] if action in ("clip", "h3clip") else []))
    last = None
    for cnt in tries:
        imgs = ["__need_check_%d__.png" % i for i in range(cnt)]
        try:
            wf = build_wf(action, imgs, params=params, cfg=cfg or {},
                          prompt=prompt or params.get("prompt") or "", seed=0,
                          prefix="__need_check__")
            return wf_needs(wf)
        except Exception as e:            # 张数不符 → 换一个张数再试
            last = e
    raise last or ValueError("推导失败")


def make_prompt(action, params):
    """编辑：把用户输入套进子模式模板，并追加「风格化」指令；拼版/抠图无需提示词"""
    params = params or {}
    if action != "edit":
        return ""
    mode = params.get("mode") or "bg"
    text = (params.get("text") or "").strip()
    st = EDIT_STYLE_MAP.get(params.get("style") or "") or {}
    extra = (st.get("prompt") or "").strip()
    tpl = EDIT_PRESETS.get(mode) or "{text}"
    # 「换风格」的正文就是画风指令；其余功能没填要求时用「自然协调」占位
    fill = (extra or text) if mode in EDIT_MODES_REQUIRE_STYLE else (text or "自然协调")
    p = tpl.replace("{text}", fill) if "{text}" in tpl else tpl
    if extra and extra not in p:
        p = p.rstrip("。；;. ") + "；" + extra     # 模板以「。」结尾 → 先去尾再拼，避免「。；」
    return p


def options():
    """加工面板的全部选项（前端拉这个，不硬编码）"""
    return {
        "cutout": {"models": CUTOUT_MODELS, "backgrounds": BACKGROUNDS,
                   "default": {"model": "RMBG-2.0", "background": "Alpha", "color": "#FFFFFF"}},
        "edit": {"modes": EDIT_MODES, "presets": EDIT_PRESETS, "styles": EDIT_STYLES,
                 "style_required": list(EDIT_MODES_REQUIRE_STYLE),
                 "default": {"mode": "bg", "text": "", "style": ""}, "max_refs": 3},
        "stitch": {"modes": STITCH_MODES, "resolutions": STITCH_RES, "pads": STITCH_PADS,
                   "max_images": STITCH_MAX,
                   "default": {"mode": "grid3", "res": 1080, "pad": 0}},
        # 分镜片段两个引擎：wan = 快而便宜、无音轨；h3 = 慢而贵、带音轨 + 2× 超分（只有 6000D 装了）
        "clip": {"engines": [
            {"id": "wan", "name": "Wan 2.1 · 480P", "size": "480×832", "fps": CLIP_FPS,
             "audio": False, "note": "任何出图机都能跑；约 5–15 分钟/条"},
            {"id": "h3", "name": "MiniMax-H3 · 768P + 2×超分", "size": "768×1344 → 1536×2688",
             "fps": H3_FPS, "audio": True, "note": "只有 6000D（¥6.46/h）；实测 5s≈421s，按 5–11 分钟/条报"}],
            "default_engine": "wan", "max_sec": H3_MAX_SEC},
    }


def validate(action, rows, params):
    """加工前校验：返回错误字符串或 None"""
    params = params or {}
    n = len(rows or [])
    if action == "cutout":
        if n != 1:
            return "抠图请选 1 张素材（当前 %d 张）" % n
        if (params.get("background") or "Alpha") not in ("Alpha", "Color"):
            return "背景参数不合法"
        if (params.get("model") or "RMBG-2.0") not in CUTOUT_MODEL_NODE:
            return "抠图模型不存在"
    elif action == "edit":
        if n < 1 or n > 3:
            return "图生图请选 1-3 张素材（第 1 张为主体，其余为参考）"
        mode = params.get("mode") or "bg"
        if mode not in [m["id"] for m in EDIT_MODES]:
            return "编辑功能不合法"
        style_id = params.get("style") or ""
        if style_id and style_id not in EDIT_STYLE_MAP:
            return "画风不存在"
        if mode in EDIT_MODES_REQUIRE_STYLE and not style_id:
            return "「换风格」请选择一个画风"
    elif action == "stitch":
        if n < 2:
            return "拼版至少选 2 张素材"
        if n > STITCH_MAX:
            return "拼版最多 %d 张素材（当前 %d 张）" % (STITCH_MAX, n)
        mode = params.get("mode") or "grid3"
        if mode not in [m["id"] for m in STITCH_MODES]:
            return "拼版模式不合法"
        if mode == "grid3" and n != 9:
            return "九宫格需要正好 9 张素材（当前 %d 张）" % n
    else:
        return "未知的加工类型"
    for r in rows or []:
        if (r.get("type") or "") != "image":
            return "抠图 / 图生图 / 拼版 只支持图片素材"
        p = url_to_path(r.get("file_path"))
        if not p or not p.is_file():
            return "素材文件缺失：%s" % (r.get("name") or r.get("file_path") or r.get("id"))
    return None
