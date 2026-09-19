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
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
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

EDIT_MODES = [
    {"id": "bg", "label": "换背景"},
    {"id": "keep", "label": "保人物一致"},
    {"id": "angle", "label": "多角度"},
]
EDIT_PRESETS = {
    "bg": "把主体完整保留，替换背景为：{text}。保持主体光线、质感、比例不变。",
    "keep": "严格保持主体（人物）长相、发型、服装与姿态完全一致，只按以下要求调整：{text}。",
    "angle": "保持主体（人物）长相与服装完全一致，改为以下视角重新呈现：{text}。",
}

STITCH_MODES = [{"id": "grid3", "label": "九宫格 3×3"}, {"id": "column", "label": "长图竖拼"}]
STITCH_RES = [1080, 1440, 2048]
STITCH_PADS = [0, 10, 20, 40]
STITCH_MAX = 9                 # 长图最多 9 张

# Qwen-Image-Edit 默认件（可用 settings 的 comfy_edit_unet / comfy_edit_lora / comfy_edit_steps 覆盖）
EDIT_UNET_DEFAULT = "qwen_image_edit_2511_fp8_e4m3fn.safetensors"
EDIT_LORA_DEFAULT = "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors"
ANGLE_LORA = "qwen-image-edit-2511-multiple-angles-lora.safetensors"
EDIT_CLIP_DEFAULT = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
EDIT_VAE_DEFAULT = "qwen_image_vae.safetensors"


# 「风格化」：给编辑链路再追加一个编辑类 LoRA（纯文生图无效，必须走图生图）
# 实测（2026-09-20，同一输入图 + 同一提示词 + 系统编辑参数 4 步，逐张看图判定）：
#   ✅ manga1 漫画·厚涂 / manga2 漫画·清线 / chibi Q版手办 / qtoanime 转动画 / angle 多角度 —— 明显生效且画质可用
#   ❌ whitebg 白底转场景（实拍图与纯白底图两种输入都产出全噪点，LoRA 本身跑不出图）
#   ❌ realalpha 转真人（素材基本都是真人所拍/AI 写实图 → 挂上无任何变化；它面向的是动画输入）
#   ❌ pose 姿态跟随（无部位参考图时不生效）、putithere 物品放置（把场景抹成白底，非预期）
# 想加回来：真机各出一张确认可用后再加，别只按文件名猜
EDIT_STYLES = [
    {"id": "", "label": "无", "lora": "", "prompt": ""},
    {"id": "manga1", "label": "漫画·厚涂", "lora": "20 (1qwen漫画1).safetensors", "prompt": "把画面转成日式漫画风格"},
    {"id": "manga2", "label": "漫画·清线", "lora": "20qwen漫画2.safetensors", "prompt": "把画面转成日式漫画风格"},
    {"id": "chibi", "label": "Q版手办", "lora": "to-chibi_v1.safetensors", "prompt": "把主体变成可爱的Q版手办形象"},
    {"id": "qtoanime", "label": "转动画", "lora": "kontext-qtorealanime.safetensors", "prompt": "把画面转成日式动画风格"},
    {"id": "angle", "label": "多角度", "lora": "qwen-image-edit-2511-multiple-angles-lora.safetensors", "prompt": "换成另一个视角重新呈现"},
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
    """按 file_path 幂等入库：已有则只更新可读信息（不新增行）"""
    c = _conn()
    owner_id = _norm_uid(owner_id)
    row = c.execute("SELECT id FROM materials WHERE file_path=?", (file_path,)).fetchone()
    if row:
        c.execute("UPDATE materials SET type=?, name=?, category=?,"
                  " article_id=COALESCE(?, article_id),"
                  " owner_id=COALESCE(?, owner_id),"
                  " owner=CASE WHEN ? <> '' THEN ? ELSE owner END,"
                  " tags=? WHERE id=?",
                  (mtype, name or "", category or "", article_id, owner_id,
                   owner or "", owner or "", tags or "", row["id"]))
        mid = row["id"]
    else:
        cur = c.execute("INSERT INTO materials (article_id,type,name,category,file_path,"
                        "source,scope,status,tags,owner_id,owner) "
                        "VALUES (?,?,?,?,?,?,?,'approved',?,?,?)",
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


def list_for(uid=None, mtype="all", scope="mine", limit=200):
    """素材列表：mine = 我的 + 未归属的历史素材；shared = 平台共享（暂未启用）"""
    c = _conn()
    where, args = ["status = 'approved'"], []
    if scope == "shared":
        where.append("scope = 'shared'")
    else:
        where.append("(owner_id IS NULL OR owner_id = ?)")
        args.append(int(uid or 0))
    if mtype and mtype != "all":
        where.append("type = ?")
        args.append(mtype)
    rows = c.execute("SELECT * FROM materials WHERE " + " AND ".join(where) +
                     " ORDER BY created_at DESC, id DESC LIMIT ?", args + [int(limit)]).fetchall()
    c.close()
    return [dict(r) for r in rows]


def delete_material(mid, uid=None):
    """删除素材：只允许删自己的（或历史未归属的）；同时删磁盘文件"""
    c = _conn()
    r = c.execute("SELECT * FROM materials WHERE id=?", (int(mid),)).fetchone()
    if not r:
        c.close()
        return {"ok": False, "error": "素材不存在"}
    own = r["owner_id"]
    if own is not None and int(own) != int(uid or 0):
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
    if owner_id is None:
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
                  use_angle_lora=False, style=""):
    """图生图（Qwen-Image-Edit 2511 + Lightning 4步）：
    LoadImage×N → TextEncodeQwenImageEditPlus(可带参考图) → ReferenceLatent → KSampler → SaveImage"""
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
    img_refs = []
    for nm in (imgs or [])[:3]:
        wf[str(nid)] = {"class_type": "LoadImage", "inputs": {"image": nm}}
        img_refs.append([str(nid), 0])
        nid += 1
    if not img_refs:
        raise ValueError("图生图至少需要 1 张输入图")
    wf[str(nid)] = {"class_type": "VAEEncode",
                    "inputs": {"pixels": img_refs[0], "vae": ["3", 0]}}
    lat_ref = [str(nid), 0]
    nid += 1
    pos = {"class_type": "TextEncodeQwenImageEditPlus",
           "inputs": {"clip": clip_ref, "prompt": prompt, "vae": ["3", 0]}}
    neg_in = {"class_type": "TextEncodeQwenImageEditPlus",
              "inputs": {"clip": clip_ref, "prompt": neg or "", "vae": ["3", 0]}}
    for i, ref in enumerate(img_refs):
        pos["inputs"]["image%d" % (i + 1)] = ref
        neg_in["inputs"]["image%d" % (i + 1)] = ref
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


def build_wf(action, imgs, params=None, cfg=None, prompt="", seed=0, prefix="mat"):
    """统一入口：action = cutout / edit / stitch"""
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
                             style=params.get("style") or "")
    if action == "stitch":
        return build_stitch_wf(imgs, mode=params.get("mode") or "grid3",
                               res=params.get("res") or 1080,
                               pad=params.get("pad") or 0, prefix=prefix)
    raise ValueError("未知的加工类型：%s" % action)


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
    # 用户没填要求时：优先用风格指令占位，避免出现「自然协调；转成漫画风格」这种别扭句
    if "{text}" in tpl:
        p = tpl.replace("{text}", text or extra or "自然协调")
    else:
        p = tpl
    if extra and extra not in p:
        p = p + "；" + extra
    return p


def options():
    """加工面板的全部选项（前端拉这个，不硬编码）"""
    return {
        "cutout": {"models": CUTOUT_MODELS, "backgrounds": BACKGROUNDS,
                   "default": {"model": "RMBG-2.0", "background": "Alpha", "color": "#FFFFFF"}},
        "edit": {"modes": EDIT_MODES, "presets": EDIT_PRESETS, "styles": EDIT_STYLES,
                 "default": {"mode": "bg", "text": "", "style": ""}, "max_refs": 3},
        "stitch": {"modes": STITCH_MODES, "resolutions": STITCH_RES, "pads": STITCH_PADS,
                   "max_images": STITCH_MAX,
                   "default": {"mode": "grid3", "res": 1080, "pad": 0}},
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
        if (params.get("mode") or "bg") not in [m["id"] for m in EDIT_MODES]:
            return "图生图模式不合法"
        if (params.get("style") or "") not in EDIT_STYLE_MAP:
            return "图生图风格不合法"
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
