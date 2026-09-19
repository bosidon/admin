#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 生图：AutoDL 应用实例 按需开关机 + ComfyUI(Qwen-Image) 出图

设计要点
- 配置读 data/database.db 的 settings 表，键前缀 comfy_（不入 git）
- 任务在后台线程串行执行，状态存内存 JOBS（PM2 fork 单进程）
- 同一时间只允许一个生成任务（RUN_LOCK），避免抢显存
- 任务结束（成功/失败/超时）一律尝试关机，防止漏计费
"""
import hashlib
import json
import re
import random
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import requests

from illustrate import SIZES, make_qrcode, compose_card_over_bg

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = str(DATA_DIR / "database.db")
CONTENT_DB = str(DATA_DIR / "content.db")
GEN_DIR = BASE_DIR / "static" / "generated"

ADL_HOST = "https://www.autodl.art"
ADL_P = "/api/v1/adl_dev/dev/instance/pro"

# settings 表默认值
DEFAULTS = {
    "comfy_api_token": "",
    "comfy_instance_uuid": "",
    "comfy_base_url": "",
    "comfy_unet": "qwen_image_fp8_e4m3fn.safetensors",
    "comfy_clip": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
    "comfy_vae": "qwen_image_vae.safetensors",
    "comfy_steps": "20",
    "comfy_auto_shutdown": "1",
}

# aspect → (宽, 高)，与 illustrate.py 的 SIZES 对齐
ASPECT_SIZE = {
    "3:4":    (1080, 1440),
    "1:1":    (1080, 1080),
    "9:16":   (1080, 1920),
    "16:9":   (1920, 1080),
    "2.35:1": (1080, 460),
}

# 图型：统一清单 images[] 的元素类型（① 生成方案 的输出契约）
IMAGE_TYPES = ("cover", "quote", "points", "photo")
ITYPE_LABEL = {"cover": "封面卡", "quote": "金句卡", "points": "要点卡", "photo": "画面"}

# 文案类型（articles.content_type）→ 写进用户消息，让 LLM 对上「文案类型档位」表
CTYPE_LABEL = {
    "article": "图文文章", "short_video": "短视频脚本",
    "long_video": "长视频脚本", "speech": "口播稿",
}

NEG_QUALITY = ("模糊, 低质量, 文字错误, 错别字, 多余的文字, 水印, 重复文字, 变形, 杂乱, "
               "引号, 双引号")

# 内置默认整条（文件里那段被整段删掉时回落到它）
NEG = NEG_QUALITY

# 采样参数（build_workflow 出图与「参数透明化」日志共用，避免两处漂移）
SAMPLER_CFG = 2.5
SAMPLER_NAME = "euler"
SCHEDULER = "simple"
DENOISE = 1.0

# 配图风格（14 选 1）：人工在 ① 区下拉选定；出图时把英文风格词追加到正向提示词
STYLE_PROMPT = {
    "realistic": "photorealistic, natural light, shallow depth of field, 50mm lens",
    "cinematic": "cinematic still, anamorphic lens, moody rim light, film grain",
    "commercial": "clean studio product photography, softbox lighting, seamless backdrop",
    "illustration": "flat editorial illustration, soft gradients, clean shapes",
    "watercolor": "watercolor painting, wet-on-wet washes, soft paper texture",
    "ink": "Chinese ink wash painting, xuan paper texture, generous negative space",
    "anime": "anime key visual, cel shading, clean line art",
    "3d": "3D render, soft studio light, clay material, subsurface scattering",
    "concept": "concept art, matte painting, dramatic scale, volumetric light",
    "artistic": "ethereal dreamscape, mist, soft light rays, surreal calm",
    "mystic": "dark mystic atmosphere, deep shadows, candlelight, quiet symbolism",
    "minimal": "minimalist composition, vast negative space, muted tones",
    "editorial": "editorial magazine layout, strong grid, space for bold typography",
    "collage": "cut-out paper collage, torn paper edges, layered textures",
}
STYLE_LABEL = {
    "realistic": "写实风", "cinematic": "电影感", "commercial": "商业质感",
    "illustration": "插画风", "watercolor": "水彩风", "ink": "水墨国风", "anime": "动漫风",
    "3d": "3D风", "concept": "概念艺术",
    "artistic": "意境风", "mystic": "暗黑神秘", "minimal": "极简留白",
    "editorial": "杂志排版", "collage": "拼贴杂志",
}
STYLE_TYPES = tuple(STYLE_PROMPT)          # 顺序 = ① 区下拉顺序 = 设置页「默认视觉风格」顺序
DEFAULT_STYLE = "artistic"                 # 未指定 / 不认识的风格回落到它

# 金句没写 bg 时的兜底背景词（不含画风，画风由 STYLE_PROMPT 按当前风格补）
FALLBACK_BG = ("Mystical serene minimal full-bleed background, soft light, "
               "no text, no letters, no people")


def _with_style(prompt, style):
    """给正向提示词补上风格英文词；没有对应风格就原样返回"""
    frag = STYLE_PROMPT.get(style or "")
    p2 = (prompt or "").strip()
    if not frag:
        return p2
    return (p2.rstrip(" ,") + ", " + frag) if p2 else frag


# 配图 Agent 的 System Prompt：出厂默认在下面，实际使用 prompts/image_agent.md（设置页可编辑）
PROMPT_DIR = BASE_DIR / "prompts"
PLAN_PROMPT_FILE = PROMPT_DIR / "image_agent.md"
PLAN_PROMPT_DEFAULT = """# 角色

你是「自媒体文案配图 Agent」——资深自媒体视觉策划 + AI 绘画提示词工程师。任务：读用户给的文案，结合系统给出的**平台**、**文案类型**与**配图风格**，判断受众、情绪与配图目标，直接输出一份可执行的配图方案与生图提示词。

# 输入（固定格式）

任务以一条用户消息给出，固定四段，你只从这四段读信息：

- `## 任务`：本次要做什么（产出配图方案）
- `## 输入参数`：固定四行，取值一律「中文名（id: id）」
  - 平台：小红书（id: xiaohongshu）
  - 文案类型：图文文章（id: article）
  - 配图风格：意境风（id: artistic）
  - 文案标题：睡前十分钟，把夜晚还给自己
- `## 文案正文`：正文由 `<<<CONTENT` 与 `CONTENT` 前后包裹，只读包裹内的内容
- `## 输出要求`：本次输出的字段与取值约束（与本文档一致）

# 运行口径

出图后端是 AutoDL ComfyUI（Qwen-Image）：

- **提示词英文优先**：`bg` 以英文为主（英文对风格词的命中率更稳）；要让 AI 直接画中文文字时，在 `bg` 里原样写出那段中文
- **画幅 5 选 1**：`3:4`（小红书/封面）· `1:1`（朋友圈/知乎/微博）· `9:16`（抖音/快手/视频号/直播）· `16:9`（长视频/B站/公众号内页）· `2.35:1`（公众号头图）
- **4 种图型**（每张图必须标 `type`）：
  - `cover` 封面卡：主标题 + 副标题，AI 出满版背景，文字由程序叠加（也可让 AI 直接画在画面里）
  - `quote` 金句卡：一句金句，居中大字
  - `points` 要点卡：标题 + 3-5 条编号要点
  - `photo` 纯画面：以画面为主，可以不含文字，也可以让 AI 直接画出少量文字
- **图上文字两条路都可用**：写进 `texts`（程序精确叠加，**要求中文准确时优先**），或直接在 `bg` 里描述让 AI 画出来（适合装饰性文字、字母/英文、艺术字形）。**同一段文字不要既写 `texts` 又写进 `bg`**（会叠两遍）
- **文字上限**：cover 主标题 ≤ 12 字 + 副标题 ≤ 18 字；quote ≤ 28 字；points 标题 ≤ 14 字、每条要点 ≤ 16 字；整张不超过 5 行；图上文字不要用广告法绝对化用词（最 / 第一 / 100% / 包治 等）
- **`texts` 里只写正文，不要写序号或项目符号**（「1.」「一、」「·」一律不要，编号由程序自动加）
- **配图风格由用户在 ① 区选定（14 选 1，系统会在输入参数里告诉你本次用哪一个）**：写实风 `realistic` · 电影感 `cinematic` · 商业质感 `commercial` · 插画风 `illustration` · 水彩风 `watercolor` · 水墨国风 `ink` · 动漫风 `anime` · 3D风 `3d` · 概念艺术 `concept` · 意境风 `artistic` · 暗黑神秘 `mystic` · 极简留白 `minimal` · 杂志排版 `editorial` · 拼贴杂志 `collage`
  - 该风格的**画风英文词由系统自动追加**到每条 `bg` 后面 —— 你不用写画风词，**也不要输出 `style` 字段**；但画面内容、光线、构图、主体规模要跟它匹配（例：`minimal` 极简留白 → 主体小、留白多、色调克制；`mystic` 暗黑神秘 → 深阴影、烛光、静谧符号感；`watercolor` 水彩风 → 柔和晕染、纸质质感）
- 同一篇文案的所有图保持同一视觉主线与情绪（画风由系统统一追加，不必重复写）

# 工作流程

1. 读文案 → 主题、核心观点/卖点、受众、情绪、关键词、行动号召
2. 判断配图目标：点击率 / 信息传达 / 情绪共鸣 / 转化 / 品牌记忆
3. 定视觉主线：按下方「生图提示词公式」的要素定主体、场景、构图、光线、情绪
4. 按「文案类型档位」出清单：几张、什么图型、各用什么画幅
5. 逐条给：中文画面描述 + 图上文字 + 英文提示词
6. 信息不足就**自行合理假设**，不要反问用户

# 平台适配（内容调性 · 图文文章画幅）

| 平台 | 画幅 | 内容调性 |
|---|---|---|
| 小红书 | 3:4 为主，1:1 备用 | 生活感、真实、口语化、强标题 |
| 朋友圈 | 1:1 | 生活感、干净、留白 |
| 公众号 | 2.35:1 头图 + 16:9 内页 | 简洁、品牌感 |
| 抖音/快手/视频号 | 9:16 | 强冲击、大字、动态感 |
| B站 | 16:9 | 科技、极简、有趣 |
| 知乎 | 1:1 或 16:9 | 理性、低饱和、数据感 |
| 微博 | 1:1 或 9:16 | 热点感、话题感 |
| 头条/百家号 | 2.35:1 或 16:9 | 新闻感、真实感 |
| 播客 | 1:1 封面 + 16:9 章节 | 沉静、声音感、抽象 |
| LinkedIn | 1:1 或 16:9 | 商务、专业、干净 |
| YouTube | 16:9 | 质感、信息感、封面大字 |
| Twitter/X | 16:9 或 1:1 | 简洁、观点感 |
| Instagram | 1:1 或 3:4 | 生活感、画面优先 |

- **画幅优先级**：以「文案类型档位」为准；只有**图文文章**（article）按本表选画幅
- 表里没有的平台：按调性最接近的一档处理
- 题材（种草 / 测评 / 教程 / 观点 / 故事 / 情感 / 职场 / 节日…）只影响画面内容，不改变画幅与图型档位

# 文案类型档位（按输入参数里的「文案类型」选一档）

| 类型 | 画幅 | 图型配比 | 数量 |
|---|---|---|---|
| 图文文章（article） | 按「平台适配」表给：小红书 3:4 · 公众号 2.35:1 头图 + 16:9 内页 · 抖音/视频号 9:16 · B站/知乎 16:9 | cover 1 + quote 或 points + photo | 小红书 6-9 · 公众号 2-4 · 其它 1-3 |
| 短视频脚本（short_video） | 9:16 | cover 1 + quote(章节) + photo(关键帧) | 3-8 |
| 长视频脚本（long_video） | 16:9 | cover 1 + points(章节卡) + photo(B-roll) | 时长(分)×1.2~1.5 |
| 口播稿（speech） | 9:16 短 / 16:9 长 | cover 1 + points + photo | 按分钟 ×1 |

- 表里的数量是常规档位；内容不适合配图时可以是 0 张，但不要凑数
- 图文文章发**小红书**时按笔记体处理：3:4 竖图为主、首图即封面、文字更短更口语、6-9 张
- 类型不在上表时：按最接近的一档处理
- 长视频 / 口播：先给 1-2 张**角色卡或主场景卡**作为全片视觉锚点，后续镜头沿用同一主体描述与色调

# 生图提示词公式

`bg`（英文）：主体 + 动作/状态 + 场景 + 构图 + 镜头 + 光线 + 色彩 + 材质 + 情绪 + 画质（**画风不用写，系统会按所选风格自动追加**；画幅由系统按 `aspect` 设定，不用写 `--ar` 之类的工具参数）；**画面不需要任何文字时**，再追加 `no text, no letters, no watermark`

`cn`（中文）：一句话说清画面（10-40 字），要具体、可画、能一眼判断画面对不对

# 输出格式（唯一格式）

只输出**一个严格 JSON 对象**：不要 Markdown、不要解释、不要代码块围栏、不要多余字段。

顶层固定 2 个字段，缺一不可：`platform` · `images`。

{
  "platform": "xiaohongshu",
  "images": [
    {"type": "cover", "aspect": "3:4", "section": "", "at": "",
     "cn": "中文画面描述（背景画什么）", "texts": ["主标题", "副标题"],
     "bg": "english background prompt"},
    {"type": "quote", "aspect": "3:4", "section": "", "at": "",
     "cn": "中文画面描述", "texts": ["金句"], "bg": "english background prompt"},
    {"type": "points", "aspect": "3:4", "section": "", "at": "",
     "cn": "中文画面描述", "texts": ["标题", "要点一", "要点二", "要点三"], "bg": "english background prompt"},
    {"type": "photo", "aspect": "3:4", "section": "", "at": "",
     "cn": "中文画面描述", "texts": [], "bg": "english image prompt"}
  ]
}

字段规则：

| 字段 | 类型 | 必填 | 取值 / 说明 |
|---|---|---|---|
| `platform` | 字符串 | 是 | 平台 id，取自输入参数 |
| `images` | 数组 | 是 | 每张图一个对象，按出图顺序排列；没有配图填 `[]` |
| `images[].type` | 字符串 | 是 | `cover` / `quote` / `points` / `photo` 4 选 1 |
| `images[].aspect` | 字符串 | 是 | `3:4` / `1:1` / `9:16` / `16:9` / `2.35:1` 5 选 1 |
| `images[].texts` | 字符串数组 | 是 | cover = [主标题, 副标题]；quote = [金句]；points = [标题, 要点1, 要点2…]；photo = `[]` |
| `images[].section` | 字符串 | 是 | 短视频脚本/长视频脚本/口播稿：填段落或镜号（如 `"第2段"`）；图文文章：填 `""` |
| `images[].at` | 字符串 | 是 | 短视频脚本/长视频脚本/口播稿：填时间点（如 `"00:45"`）；图文文章：填 `""` |
| `images[].cn` | 字符串 | 是 | 中文画面描述 10-40 字，给人看 |
| `images[].bg` | 字符串 | 是 | 英文生图提示词；要让 AI 画进画面的文字可用自然语言描述（中英文均可）。中文文字**建议同时写进 `texts`**（AI 画中文容易出错，程序叠字更稳） |

- 短视频脚本 / 长视频脚本 / 口播稿：**每一条**都要填 `section` 与 `at`（封面卡填 `"开头"` 与 `"00:00"`）；图文文章一律填 `""`

# 负面提示词（提交给 ComfyUI · 系统读取，不发给 LLM）

模糊, 低质量, 文字错误, 错别字, 多余的文字, 水印, 重复文字, 变形, 杂乱, 引号, 双引号"""

# 用户消息模板：本次任务参数（System Prompt 里不出现占位符）
PLAN_USER_TEMPLATE = (
    "## 任务\n"
    "为下面这篇自媒体文案产出配图方案。\n"
    "\n"
    "## 输入参数\n"
    "- 平台：{platform}（id: {platform_id}）\n"
    "- 文案类型：{ctype_label}（id: {ctype_id}）\n"
    "- 配图风格：{style_label}（id: {style_id}）\n"
    "- 文案标题：{title}\n"
    "\n"
    "## 文案正文\n"
    "<<<CONTENT\n"
    "{content}\n"
    "CONTENT\n"
    "\n"
    "## 输出要求\n"
    "- 只输出一个 JSON 对象（不要 Markdown、不要解释、不要代码块围栏）\n"
    "- 顶层字段只有两个：platform / images（不要输出 style / note / reason，配图风格已由系统指定）\n"
    "- `images[]` 里写哪些字段、取什么值，**以系统提示词为准**\n"
    "- 数量与图型配比按系统提示词的「文案类型档位」自行决定（可以是 0 张）\n"
    "- platform 填平台 id（如 xiaohongshu）"
)

# 平台标识 → 中文名（写进用户消息，让 LLM 能对上平台适配表）
PLATFORM_LABEL = {
    "xiaohongshu": "小红书", "moments": "朋友圈", "wechat": "公众号",
    "video_account": "视频号", "douyin": "抖音", "kuaishou": "快手",
    "bilibili": "B站", "zhihu": "知乎", "weibo": "微博", "toutiao": "头条",
    "baijiahao": "百家号", "linkedin": "LinkedIn", "youtube": "YouTube",
    "twitter": "Twitter/X", "instagram": "Instagram", "podcast": "播客",
}


# 文件里「负面提示词」段的标题（该段由系统读取，不发给 LLM）
NEG_HEADING_RE = re.compile(r"^#+\s*负面提示词.*$", re.M)
NEXT_HEADING_RE = re.compile(r"^#+\s+", re.M)


def load_plan_prompt_raw():
    """整份文件内容（设置页显示/编辑用，含负面提示词段）；缺失/读失败 → 出厂默认并自动补回"""
    try:
        t = PLAN_PROMPT_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    except Exception:
        pass
    save_plan_prompt(PLAN_PROMPT_DEFAULT)
    return PLAN_PROMPT_DEFAULT.strip()


def _split_neg_section(raw):
    """(发给 LLM 的 System Prompt, 负面提示词段文本)；没有该段则负面段为 ""。"""
    m = NEG_HEADING_RE.search(raw or "")
    if not m:
        return (raw or "").strip(), ""
    head = raw[:m.start()].strip()
    tail = raw[m.end():]
    m2 = NEXT_HEADING_RE.search(tail)          # 该段到下一个标题为止
    if m2:
        tail = tail[:m2.start()]
    return head, tail.strip()


def load_plan_prompt():
    """发给 LLM 的 System Prompt：整份文件去掉「负面提示词」段"""
    return _split_neg_section(load_plan_prompt_raw())[0]


def load_negative_prompt():
    """提交给 ComfyUI 的负面提示词：文件里「负面提示词」段的内容；该段缺失时回落到内置默认"""
    words = _split_neg_section(load_plan_prompt_raw())[1]
    words = ", ".join(ln.strip() for ln in words.splitlines()
                      if ln.strip() and not ln.strip().startswith("#"))
    return words or NEG


def save_plan_prompt(text):
    """写 System Prompt 文件（设置页可编辑）；返回 (ok, 错误文案)"""
    t = (text or "").strip()
    if not t:
        return False, "内容不能为空"
    try:
        PROMPT_DIR.mkdir(parents=True, exist_ok=True)
        PLAN_PROMPT_FILE.write_text(t + "\n", encoding="utf-8")
        return True, None
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, str(e)[:120])


save_plan_prompt(load_plan_prompt_raw())    # 确保 prompts/image_agent.md 存在（缺失即补回）

# 运行期状态（内存）
JOBS = {}
JOBS_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()          # 同一时间只跑一个任务
JOB_TTL = 3600 * 6


# ============================================================
# 配置
# ============================================================
def _settings_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def get_comfy_config():
    """读 settings 表里的 comfy_* 配置（缺失用默认值）"""
    cfg = dict(DEFAULTS)
    try:
        conn = _settings_db()
        for r in conn.execute("SELECT key, value FROM settings WHERE key LIKE 'comfy\\_%' ESCAPE '\\'"):
            if r["value"]:
                cfg[r["key"]] = r["value"]
        conn.close()
    except Exception:
        pass
    return cfg


def get_default_style():
    """配图默认风格：settings.default_style（设置页「默认视觉风格」）；空 / 不认识 → DEFAULT_STYLE"""
    v = ""
    try:
        conn = _settings_db()
        r = conn.execute("SELECT value FROM settings WHERE key='default_style'").fetchone()
        conn.close()
        v = (r["value"] if r else "") or ""
    except Exception:
        pass
    v = str(v).strip().lower()
    return v if v in STYLE_TYPES else DEFAULT_STYLE


# ============================================================
# AutoDL 应用实例 控制面
# ============================================================
def _adl(method, path, body=None):
    cfg = get_comfy_config()
    tok = cfg.get("comfy_api_token", "")
    if not tok:
        return {"code": "NoToken", "msg": "未配置 comfy_api_token"}
    try:
        if method == "POST":
            r = requests.post(ADL_HOST + ADL_P + path,
                              headers={"Authorization": tok, "Content-Type": "application/json"},
                              json=body or {}, timeout=40)
        else:
            r = requests.get(ADL_HOST + ADL_P + path,
                             headers={"Authorization": tok}, params=body or {}, timeout=40)
        return r.json()
    except Exception as e:
        return {"code": "Exception", "msg": "%s: %s" % (type(e).__name__, e)}


def adl_status():
    cfg = get_comfy_config()
    return _adl("GET", "/status", {"instance_uuid": cfg.get("comfy_instance_uuid", "")})


def adl_snapshot():
    cfg = get_comfy_config()
    return _adl("GET", "/snapshot", {"instance_uuid": cfg.get("comfy_instance_uuid", "")})


def adl_power_on():
    cfg = get_comfy_config()
    return _adl("POST", "/power_on",
                {"instance_uuid": cfg.get("comfy_instance_uuid", ""), "payload": "gpu"})


def adl_power_off():
    cfg = get_comfy_config()
    return _adl("POST", "/power_off", {"instance_uuid": cfg.get("comfy_instance_uuid", "")})


def resolve_base_url(logger=None):
    """ComfyUI 访问地址：settings 优先，空则从 snapshot 的 service_6006_domain 取"""
    cfg = get_comfy_config()
    base = (cfg.get("comfy_base_url") or "").strip().rstrip("/")
    if not base:
        snap = adl_snapshot().get("data") or {}
        dom = snap.get("service_6006_domain") or ""
        if dom:
            base = "https://" + dom
            if logger:
                logger("从 snapshot 自动取到 ComfyUI 地址：" + base)
    if base and not base.startswith("http"):
        base = "https://" + base
    return base


# ============================================================
# ComfyUI 调用
# ============================================================
def comfy_ready(base, timeout=300, logger=None, interval=6):
    """轮询 /system_stats 直到就绪"""
    t0 = time.time()
    last = -30
    while time.time() - t0 < timeout:
        try:
            r = requests.get(base + "/system_stats", timeout=8)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        el = int(time.time() - t0)
        if logger and el - last >= 30:
            logger("等待 ComfyUI 就绪… %ds" % el)
            last = el
        time.sleep(interval)
    return False


def build_workflow(prompt, w, h, seed, cfg, prefix, neg):
    steps = int(cfg.get("comfy_steps") or 20)
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": cfg.get("comfy_unet"), "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": cfg.get("comfy_clip"), "type": "qwen_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": cfg.get("comfy_vae")}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode",
              "inputs": {"text": neg, "clip": ["2", 0]}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "7": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": SAMPLER_CFG,
                         "sampler_name": SAMPLER_NAME, "scheduler": SCHEDULER,
                         "denoise": DENOISE, "model": ["1", 0],
                         "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": prefix, "images": ["8", 0]}},
    }


def comfy_generate(base, prompt, w, h, prefix, cfg, timeout=600, seed=None):
    """提交一张图并等待完成，返回 (filename, subfolder, meta)

    meta = 这张图最终提交给 ComfyUI 的提示词与参数（供任务日志/落库排查）"""
    if seed is None:
        seed = random.randint(1, 2 ** 31 - 1)
    neg_text = load_negative_prompt()                # 文件里的「负面提示词」段 + 强制安全词
    wf = build_workflow(prompt, w, h, seed, cfg, prefix, neg_text)
    meta = {"prompt": prompt, "neg": neg_text, "seed": seed,
            "steps": int(cfg.get("comfy_steps") or 20), "cfg": SAMPLER_CFG,
            "sampler": SAMPLER_NAME, "scheduler": SCHEDULER,
            "width": w, "height": h}
    r = requests.post(base + "/prompt", json={"prompt": wf}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError("提交失败 HTTP %s: %s" % (r.status_code, r.text[:300]))
    pid = r.json()["prompt_id"]

    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(4)
        try:
            hh = requests.get(base + "/history/" + pid, timeout=30).json()
        except Exception:
            continue
        if pid in hh:
            entry = hh[pid]
            if (entry.get("status") or {}).get("status_str") == "error":
                raise RuntimeError("ComfyUI 执行出错: %s" % json.dumps(
                    (entry.get("status") or {}).get("messages", []), ensure_ascii=False)[:300])
            for node in (entry.get("outputs") or {}).values():
                for im in (node.get("images") or []):
                    return im.get("filename"), im.get("subfolder", ""), meta
    raise RuntimeError("生成超时（%ds）" % timeout)


def comfy_download(base, fname, subfolder=""):
    r = requests.get(base + "/view",
                     params={"filename": fname, "subfolder": subfolder or "", "type": "output"},
                     timeout=120)
    r.raise_for_status()
    return r.content


# ============================================================
# 数据库
# ============================================================
def _content_db():
    conn = sqlite3.connect(CONTENT_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _merge_images(article_id, new_urls, drop_prefix=None):
    """把新图并进 images_json；drop_prefix 用于替换同前缀的旧图"""
    conn = _content_db()
    row = conn.execute("SELECT images_json FROM articles WHERE id=?", (article_id,)).fetchone()
    try:
        cur = json.loads((row["images_json"] if row else None) or "[]")
    except Exception:
        cur = []
    if drop_prefix:
        pre = [drop_prefix] if isinstance(drop_prefix, str) else list(drop_prefix)
        cur = [u for u in cur if not any(p in u for p in pre)]
    for u in new_urls:
        if u not in cur:
            cur.append(u)
    conn.execute("UPDATE articles SET images_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                 (json.dumps(cur, ensure_ascii=False), article_id))
    conn.commit()
    conn.close()
    return cur


def _save_gen_log(article_id, name, kind, meta, style=""):
    """把每张图最终提交的提示词与参数落库（出图排查用）；返回 None 或错误文案"""
    try:
        conn = _content_db()
        conn.execute("DELETE FROM illustration_logs WHERE article_id=? AND name=?",
                     (article_id, name))
        conn.execute(
            "INSERT INTO illustration_logs (article_id, name, kind, prompt, neg, style, "
            "seed, steps, cfg, sampler, scheduler, width, height) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (article_id, name, kind, meta.get("prompt", ""), meta.get("neg", ""), style,
             meta.get("seed"), meta.get("steps"), meta.get("cfg"),
             meta.get("sampler"), meta.get("scheduler"),
             meta.get("width"), meta.get("height")))
        conn.commit()
        conn.close()
        return None
    except Exception as e:
        return "%s: %s" % (type(e).__name__, str(e)[:120])


def _log_gen_meta(log, article_id, name, kind, meta, style):
    """任务日志打印 + 落库：这张图最终提交的提示词与参数"""
    log("\u24d8 参数：seed=%s · steps=%s · cfg=%s · %s/%s · %dx%d · 风格 %s"
        % (meta.get("seed"), meta.get("steps"), meta.get("cfg"), meta.get("sampler"),
           meta.get("scheduler"), meta.get("width"), meta.get("height"),
           STYLE_LABEL.get(style, style) or "-"))
    log("\u24d8 正向：" + (meta.get("prompt") or ""))
    log("\u24d8 负面：" + (meta.get("neg") or ""))
    err = _save_gen_log(article_id, name, kind, meta, style)
    if err:
        log("⚠ 参数未落库（%s）" % err)


# ============================================================
# 任务
# ============================================================
def _set(job_id, **kw):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if j:
            j.update(kw)


def _log(job_id, msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if j:
            j["log"].append(line)          # 原地修改，不 rebind
            if len(j["log"]) > 200:
                del j["log"][:-200]


def get_job(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        return dict(j) if j else None


def _gc_jobs():
    now = time.time()
    with JOBS_LOCK:
        for k in [k for k, v in JOBS.items()
                  if v.get("finished_at") and now - v["finished_at"] > JOB_TTL]:
            JOBS.pop(k, None)


def shutdown_instance():
    """手动关机（前端兜底按钮）"""
    res = adl_power_off()
    return {"ok": res.get("code") == "Success",
            "msg": res.get("msg") or res.get("code") or "",
            "raw": res}

# ============================================================
# 金句卡任务（AI 背景 + PIL 叠字）
# ============================================================
def _read_article_full(article_id):
    conn = _content_db()
    row = conn.execute(
        "SELECT id, title, content_md, platform, content_type, promo_link, promo_uid, promo_src "
        "FROM articles WHERE id=?", (article_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _link_of(art, article_id):
    if art.get('promo_link'):
        return art['promo_link']
    if art.get('promo_uid'):
        return 'https://xianbao.love/?ref=%s&src=%s' % (
            art['promo_uid'], art.get('promo_src') or ('c%d' % article_id))
    return None


def _extract_quotes_fallback(content, title, want):
    """LLM 不可用时：从正文抽句（与 auto-plan 同思路）"""
    import re as _re
    body = _re.sub(r'[#*>`\-\[\]()!]', '', content or '')
    cands = [s.strip() for s in _re.split(r'[。！？\n]', body) if 10 <= len(s.strip()) <= 28]
    return cands[:want] or [(title or '仙宝心灵成长')[:28]]


# ============================================================
# 配图方案（一次 LLM 出两组：金句组 + 场景组）
# ============================================================
ASPECTS = ("3:4", "1:1", "9:16", "16:9", "2.35:1")


def _pick_json(raw):
    """从 LLM 输出里取出第一个可解析的 JSON 对象
    容错：```json 代码围栏 / 前后多余说明文字 / 输出里出现多个对象"""
    if not raw:
        return None
    t = raw.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    try:
        j = json.loads(t)
        if isinstance(j, dict):
            return j
    except Exception:
        pass
    depth, start, in_str, esc = 0, -1, False, False      # 花括号配平扫描
    for i, ch in enumerate(t):
        if in_str:
            if esc:
                esc = False
            elif ch == chr(92):
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        j = json.loads(t[start:i + 1])
                        if isinstance(j, dict):
                            return j
                    except Exception:
                        pass
                    start = -1
    return None


def _llm_json(url, key, model, messages, tries=2):
    """调 LLM 并取出 JSON 对象；解析不出来再试一次。返回 (json|None, 错误文案)"""
    err = "LLM 未返回 JSON"
    for _ in range(max(1, tries)):
        try:
            r = requests.post(url, headers={"Authorization": "Bearer " + key,
                                            "Content-Type": "application/json"},
                              json={"model": model, "messages": messages}, timeout=180)
            raw = r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            err = "LLM 调用失败（%s）" % str(e)[:60]
            continue
        j = _pick_json(raw)
        if j is not None:
            return j, None
        err = "LLM 返回的 JSON 解析失败"
    return None, err


def _norm_image(d, default_aspect="3:4"):
    """规范化清单里的一条；无法成图（既没 bg 也没文字）返回 None

    新契约（只有 title/cn/bg）→ type 记为 photo（直出，不叠字）；
    default_aspect：本条没写 aspect 时用的画幅（按「文案类型档位 + 平台」推导）
    """
    if not isinstance(d, dict):
        return None
    t = str(d.get("type") or "").strip().lower()
    if t not in IMAGE_TYPES:
        t = "photo" if not (d.get("texts") or d.get("text")) else "quote"
    texts = d.get("texts")
    if not isinstance(texts, list):
        texts = [d.get("text")] if d.get("text") else []
    texts = [str(x).strip() for x in texts if str(x or "").strip()]
    if t == "photo":
        texts = []
    bg = str(d.get("bg") or d.get("prompt") or "").strip()
    if not bg and not texts:
        return None
    asp = str(d.get("aspect") or default_aspect or "3:4").strip()
    if asp not in ASPECTS:
        asp = default_aspect if default_aspect in ASPECTS else "3:4"
    return {"type": t,
            "title": str(d.get("title") or "").strip(),
            "aspect": asp,
            "section": str(d.get("section") or "").strip(),
            "at": str(d.get("at") or "").strip(),
            "cn": str(d.get("cn") or d.get("scene") or "").strip(),
            "texts": texts,
            "bg": bg,
            "on": bool(d.get("on", True))}


def _legacy_views(images):
    """② 出图路径仍按 quotes / scenes 两组走：由 images[] 派生，出图逻辑不动"""
    quotes, scenes = [], []
    for it in images:
        if it.get("type") == "photo":
            scenes.append({"scene": it.get("cn") or "", "prompt": it.get("bg") or "",
                           "aspect": it.get("aspect") or "3:4",
                           "on": it.get("on", True)})
        else:
            quotes.append({"text": "｜".join(it.get("texts") or []),
                           "cn": it.get("cn") or "", "bg": it.get("bg") or "",
                           "on": it.get("on", True)})
    return quotes, scenes


def _type_brief(images):
    """图型配比简报，如 cover1 · quote3 · photo2"""
    return " · ".join("%s%d" % (t, sum(1 for x in images if x.get("type") == t))
                      for t in IMAGE_TYPES if any(x.get("type") == t for x in images)) or "-"


# 新契约没写画幅时，按「文案类型档位 + 平台」推导（title 里含「头图」→ 公众号 2.35:1）
_PLAT_ASPECT = {"xiaohongshu": "3:4", "moments": "1:1", "wechat": "16:9", "video_account": "9:16",
                "douyin": "9:16", "kuaishou": "9:16", "bilibili": "16:9", "zhihu": "16:9",
                "weibo": "1:1", "toutiao": "16:9", "baijiahao": "16:9", "linkedin": "1:1",
                "youtube": "16:9", "twitter": "16:9", "instagram": "1:1", "podcast": "1:1"}


def _default_aspect(art, title=""):
    if "头图" in (title or ""):
        return "2.35:1"
    ct = str((art or {}).get("content_type") or "").strip()
    if ct in ("short_video", "speech"):
        return "9:16"
    if ct == "long_video":
        return "16:9"
    return _PLAT_ASPECT.get(str((art or {}).get("platform") or "").strip(), "3:4")


def _plan_out(data, default_style="", art=None):
    """把库里的 image_script / LLM 返回的 JSON 统一成一个方案对象（images[] 为唯一来源）"""
    if isinstance(data, list):
        data = {"scenes": data}
    if not isinstance(data, dict):
        data = {}
    art = art or {}
    images = []
    for it in (data.get("images") or []):
        asp0 = _default_aspect(art, it.get("title") if isinstance(it, dict) else "")
        n = _norm_image(it, asp0)
        if n:
            images.append(n)
    if not images:                                     # 兼容旧格式：quotes / scenes
        for q in (data.get("quotes") or []):
            if isinstance(q, str):
                q = {"text": q}
            if isinstance(q, dict):
                n = _norm_image(dict(q, type=q.get("type") or "quote"),
                                _default_aspect(art))
                if n:
                    images.append(n)
        for s in (data.get("scenes") or []):
            if isinstance(s, dict):
                n = _norm_image({"type": "photo", "aspect": s.get("aspect"),
                                 "cn": s.get("scene"), "bg": s.get("prompt"),
                                 "on": s.get("on", True)}, _default_aspect(art))
                if n:
                    images.append(n)
    quotes, scenes = _legacy_views(images)
    style = str(data.get("style") or "").strip().lower()
    return {"style": style if style in STYLE_TYPES else default_style,
            "platform": str(data.get("platform") or "").strip(),
            "images": images, "quotes": quotes, "scenes": scenes}


def read_plan(article_id, default_style=None):
    """读配图方案：统一成 images[]（旧 quotes/scenes 自动映射），并派生 ② 出图用的两组视图

    style 只认 14 个风格之一；旧配色值（purple/dark/gold/maya）一律忽略，回落默认风格。
    """
    conn = _content_db()
    row = conn.execute("SELECT image_script, platform, content_type FROM articles WHERE id=?",
                       (article_id,)).fetchone()
    conn.close()
    raw = (row["image_script"] if row else None) or ""
    try:
        data = json.loads(raw or "{}")
    except Exception:
        data = {}
    art = {"platform": (row["platform"] if row else "") or "",
           "content_type": (row["content_type"] if row else "") or ""}
    return _plan_out(data, default_style=default_style or get_default_style(), art=art)


def save_plan(article_id, plan):
    """写库：只存 {style, platform, images}；quotes/scenes 是派生视图，不入库"""
    if isinstance(plan, dict) and plan.get("images") is not None:
        p = {k: plan.get(k) for k in ("style", "platform", "images")}
    else:                                              # 极旧调用方（纯 quotes/scenes）
        p = plan
    conn = _content_db()
    conn.execute("UPDATE articles SET image_script=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                 (json.dumps(p, ensure_ascii=False), article_id))
    conn.commit()
    conn.close()
    return plan


def gen_plan(art, llm_cfg, card_want=0, scene_count=0, style=""):
    """一次 LLM 调用产出整份方案（契约 images[]）；返回 (plan|None, err)

    card_want / scene_count 保留仅为兼容旧调用方，数量与配比由 LLM 按平台+类型决定。
    style：人工在 ① 区选定的配图风格（空 / 不认识 → 用设置页默认风格），LLM 输出不作数。
    """
    import re as _re
    llm_cfg = llm_cfg or {}
    key = llm_cfg.get("llm_api_key", "")
    url = llm_cfg.get("llm_base_url") or ""
    model = llm_cfg.get("llm_model") or "deepseek-chat"
    content = (art.get("content_md") or "")[:2000]
    title = art.get("title") or ""
    if not (key and url and content):
        return None, "未配置 LLM 或文案无正文"

    # 系统提示词 = prompts/image_agent.md 文件内容（设置页可编辑）
    style = (style or "").strip().lower()
    style = style if style in STYLE_TYPES else get_default_style()
    sys_prompt = load_plan_prompt()
    plat = (art.get("platform") or "").strip()
    ctype = (art.get("content_type") or "").strip()
    user_msg = (PLAN_USER_TEMPLATE
                .replace("{platform}", PLATFORM_LABEL.get(plat, plat or "未指定"))
                .replace("{platform_id}", plat or "未指定")
                .replace("{ctype_label}", CTYPE_LABEL.get(ctype, ctype or "图文文章"))
                .replace("{ctype_id}", ctype or "article")
                .replace("{style_label}", STYLE_LABEL.get(style, style))
                .replace("{style_id}", style)
                .replace("{title}", title)
                .replace("{content}", content))
    j, err = _llm_json(url, key, model,
                       [{"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_msg}])
    if j is None:
        return None, err

    if not j.get("platform"):
        j["platform"] = plat
    plan = _plan_out(j, default_style=style, art=art)
    plan["style"] = style                              # 风格由人工选定，LLM 输出不作数
    plan["images"] = plan["images"][:12]               # 数量由 LLM 定，这里只兜底防爆
    plan["quotes"], plan["scenes"] = _legacy_views(plan["images"])
    if not plan["images"]:
        return None, "LLM 返回的方案是空的"
    return plan, ""



# ============================================================
# 单条重写提示词（只重写一条，不整组重跑 · 纯 LLM 不开机）
REWRITE_PROMPT = (
    "你是自媒体配图策划专家。下面这条【{kind_label}】的配图描述不理想，请只重写这一条，"
    "其它条目一律不要改动。\n\n"
    "当前条目（type={itype} · 画幅 {aspect}）：\n{current}\n\n"
    "要求：换一个与当前明显不同的构思或意象（不要只改写措辞、不要沿用当前意象）；"
    "图上文字保持简短（主标题≤12字、副标题≤18字、金句≤28字、要点≤16字）；图型与画幅不变。\n"
    "{palette}"
    "{hint}"
    "文案标题：{title}\n"
    "文案正文（节选）：\n{content}\n\n"
    "严格输出 JSON（不要输出多余文字）：{shape}"
)

# 各图型的输出形状（texts 的条数即结构，程序按它解析）
# 新契约（title/cn/bg 三条）的重写输出形状
REWRITE_SHAPE_T3 = '{"title":"用途（如 封面卡 / 第2段 00:45 · 画面）","cn":"中文提词","bg":"english image prompt"}'
REWRITE_SHAPE = {
    "cover": '{"cn":"中文画面描述","texts":["主标题","副标题"],"bg":"english background prompt"}',
    "quote": '{"cn":"中文画面描述","texts":["金句"],"bg":"english background prompt"}',
    "points": '{"cn":"中文画面描述","texts":["标题","要点一","要点二","要点三"],'
              '"bg":"english background prompt"}',
    "photo": '{"cn":"中文画面描述","texts":[],"bg":"english image prompt"}',
}


def rewrite_plan_item(article_id, index, hint, llm_cfg):
    """只让 LLM 重写方案里第 index 条（cn / texts / bg；图型、画幅、段落位置不变）。

    返回 (新条目|None, 错误文案|None)；任何解析/校验失败都保持原值不动。"""
    import re as _re
    if RUN_LOCK.locked():
        return None, "有生成任务正在跑，稍后再试"
    llm_cfg = llm_cfg or {}
    key = llm_cfg.get("llm_api_key", "")
    url = llm_cfg.get("llm_base_url") or ""
    model = llm_cfg.get("llm_model") or "deepseek-chat"
    if not (key and url):
        return None, "未配置 LLM"
    plan = read_plan(article_id)
    items = plan["images"]
    if not (0 <= index < len(items)):
        return None, "条目不存在"
    item = items[index]
    itype = item.get("type") or "quote"
    art = _read_article_full(article_id) or {}
    title = art.get("title") or ""
    content = (art.get("content_md") or "")[:2000]
    others = "；".join([("|".join(x.get("texts") or []) or x.get("cn") or "")
                        for k, x in enumerate(items) if k != index][:8])
    new_shape = bool(str(item.get("title") or "").strip())
    if new_shape:
        cur = ("用途：%s\n中文提词：%s\n英文提词：%s"
               % (item.get("title") or "（无）", item.get("cn") or "（无）", item.get("bg") or "（空）"))
    else:
        cur = ("图上文字：%s\n中文画面描述：%s\n当前提示词（英文）：%s\n当前画幅：%s"
               % ("｜".join(item.get("texts") or []) or "（无）", item.get("cn") or "（无）",
                  item.get("bg") or "（空）", item.get("aspect") or "3:4"))
    _frag = STYLE_PROMPT.get(plan.get("style") or "")
    palette = ("风格：本次整套配图的风格是 %s（%s），画面请按这个风格来（画风英文词由系统追加，不必自己写）。\n"
               % (STYLE_LABEL.get(plan.get("style"), plan.get("style")), _frag)) if _frag else ""
    h = (hint or "").strip()
    prompt = (REWRITE_PROMPT
              .replace("{kind_label}", ITYPE_LABEL.get(itype, itype))
              .replace("{itype}", itype)
              .replace("{aspect}", item.get("aspect") or "3:4")
              .replace("{current}", cur)
              .replace("{palette}", palette)
              .replace("{hint}", ("额外要求（优先满足）：%s\n" % h) if h else "")
              .replace("{title}", title)
              .replace("{content}", content)
              .replace("{shape}", REWRITE_SHAPE_T3 if new_shape
                       else REWRITE_SHAPE.get(itype, REWRITE_SHAPE["quote"])))
    if others:
        prompt += "\n其它条目（不要与它们重复）：" + others
    j, err = _llm_json(url, key, model, [{"role": "user", "content": prompt}])
    if j is None:
        return None, err
    bg = str(j.get("bg") or "").strip()
    if not bg:
        return None, "LLM 没给出新的提示词"
    texts = j.get("texts")
    if not isinstance(texts, list):
        texts = item.get("texts") or []
    texts = [str(x).strip() for x in texts if str(x or "").strip()]
    if itype == "photo":
        texts = []
    elif not texts:
        texts = item.get("texts") or []
    new_item = dict(item)
    if new_shape and str(j.get("title") or "").strip():
        new_item["title"] = str(j.get("title")).strip()
    new_item["cn"] = str(j.get("cn") or item.get("cn") or "").strip()
    new_item["texts"] = texts
    new_item["bg"] = bg
    items[index] = new_item
    plan["quotes"], plan["scenes"] = _legacy_views(items)
    save_plan(article_id, plan)
    return new_item, None



# ============================================================
# 统一生成任务（金句卡 + 场景配图，一次开机一次关机）
# ============================================================
def start_generate(article_id, opts, llm_cfg, card_size, card_want, scene_count):
    """opts: {cards: bool, scenes: bool, replan: bool}"""
    art = _read_article_full(article_id)
    if not art:
        return {"error": "文案不存在"}
    want_cards = bool(opts.get("cards"))
    want_scenes = bool(opts.get("scenes"))
    plan_only = bool(opts.get("plan_only"))
    if not (want_cards or want_scenes) and not plan_only:
        return {"error": "请至少勾选一组（金句卡 / 场景配图）"}
    if not plan_only:
        cfg = get_comfy_config()
        if not cfg.get("comfy_instance_uuid") or not cfg.get("comfy_api_token"):
            return {"error": "未配置应用实例 UUID / Token，请去「设置」页填写"}
    if RUN_LOCK.locked():
        return {"error": "已有生成任务在跑，请等它结束"}

    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id, "article_id": article_id, "status": "queued",
            "total": (card_want if want_cards else 0) + (scene_count if want_scenes else 0),
            "done": 0, "images": [], "log": [], "error": "",
            "started_at": time.time(), "finished_at": None, "mode": "generate",
            "title": art.get("title") or "", "style": "",
            "quotes": [], "scenes": [],
            "want_cards": want_cards, "want_scenes": want_scenes,
        }
    threading.Thread(
        target=_generate_worker,
        args=(job_id, article_id, opts, llm_cfg, card_size, int(card_want), int(scene_count)),
        daemon=True).start()
    _gc_jobs()
    return {"ok": True, "job_id": job_id,
            "total": JOBS[job_id]["total"]}


def _generate_worker(job_id, article_id, opts, llm_cfg, card_size, card_want, scene_count):
    def log(m):
        _log(job_id, m)

    with RUN_LOCK:
        cfg = get_comfy_config()
        art = _read_article_full(article_id) or {}
        link = _link_of(art, article_id)
        want_cards = bool(opts.get("cards"))
        want_scenes = bool(opts.get("scenes"))
        plan = read_plan(article_id)
        quotes, scenes = [], []
        card_urls, scene_urls = [], []
        try:
            # ---- ① 方案：只有「生成方案」才调 LLM（不再一步到位）----
            need = bool(opts.get("replan"))
            if need:
                _set(job_id, status="planning")
                log("AI 正在分析文案、设计配图方案…")
                newp, reason = gen_plan(art, llm_cfg, card_want, scene_count,
                                        style=plan.get("style") or "")
                if newp:
                    plan = newp
                    save_plan(article_id, plan)
                    log("方案已生成：%d 张（%s）· 风格 %s"
                        % (len(plan["images"]), _type_brief(plan["images"]),
                           STYLE_LABEL.get(plan["style"], plan["style"])))
                else:
                    log("方案生成失败：%s" % reason)
                    if want_cards and not plan["quotes"]:
                        fb = _extract_quotes_fallback(art.get("content_md"),
                                                      art.get("title"), card_want)
                        plan["quotes"] = [{"text": q, "bg": ""} for q in fb]
                        if plan["style"] not in STYLE_TYPES:
                            plan["style"] = get_default_style()
                        save_plan(article_id, plan)
                        log("改用正文抽句作为金句（%d 句）" % len(plan["quotes"]))

            if opts.get("plan_only"):
                _set(job_id, status="done", finished_at=time.time(), total=0, done=0,
                     plan_images=plan["images"], quotes=plan["quotes"], scenes=plan["scenes"],
                     style=plan["style"], want_cards=False, want_scenes=False)
                log("方案已更新（仅分析，未出图、未开机）")
                return

            quotes = [q for q in plan["quotes"] if q.get("on", True)] if want_cards else []
            scenes = [s for s in plan["scenes"] if s.get("on", True)] if want_scenes else []
            if want_cards and not quotes:
                raise RuntimeError("方案里没有勾选的金句 —— 请先在「① 生成方案」里勾选要出的条目")
            if want_scenes and not scenes:
                raise RuntimeError("方案里没有勾选的场景 —— 请先在「① 生成方案」里勾选要出的条目")
            style = plan.get("style") or ""
            if style not in STYLE_TYPES:
                style = get_default_style()
            total = len(quotes) + len(scenes)
            _set(job_id, quotes=plan["quotes"], scenes=plan["scenes"],
                 style=style, total=total)

            # ---- ② 开机 ----
            _set(job_id, status="booting")
            st = adl_status().get("data") or ""
            if st == "running":
                log("实例已在运行，跳过开机")
            else:
                log("启动实例…（当前状态 %s）" % (st or "未知"))
                res = adl_power_on()
                if res.get("code") != "Success":
                    raise RuntimeError("开机失败：%s" % (res.get("msg") or res.get("code")))
                log("开机指令已下发")
                for _ in range(60):
                    time.sleep(3)
                    if (adl_status().get("data") or "") == "running":
                        log("实例已运行")
                        break
                else:
                    raise RuntimeError("等待实例 running 超时")

            # ---- ③ 等 ComfyUI ----
            _set(job_id, status="ready")
            base = resolve_base_url(logger=log)
            if not base:
                raise RuntimeError("拿不到 ComfyUI 地址")
            log("ComfyUI 地址：" + base)
            if not comfy_ready(base, timeout=300, logger=log):
                raise RuntimeError("ComfyUI 300s 内未就绪")
            log("ComfyUI 已就绪")

            _set(job_id, status="generating")
            out_dir = GEN_DIR / str(article_id)
            out_dir.mkdir(parents=True, exist_ok=True)
            done = 0

            # ---- ④ 金句卡：AI 满版背景 + PIL 叠字 ----
            if quotes:
                cw, ch = SIZES.get(card_size, SIZES["xiaohongshu"])
                for i, q in enumerate(quotes):
                    bgp = _with_style(q.get("bg") or FALLBACK_BG, style)
                    log("金句卡 %d/%d 出背景中（%dx%d）…" % (i + 1, len(quotes), cw, ch))
                    fn, sub, meta = comfy_generate(base, bgp, cw, ch,
                                                   "qcbg_%d_%d" % (article_id, i), cfg)
                    bg = comfy_download(base, fn, sub)
                    card = compose_card_over_bg(bg, q["text"],
                                                size=card_size, qr_link=None)
                    name = "qa_%02d_%s.png" % (i + 1,
                                               hashlib.md5(card.tobytes()).hexdigest()[:6])
                    card.save(str(out_dir / name), "PNG", optimize=True)
                    card_urls.append("/static/generated/%d/%s" % (article_id, name))
                    done += 1
                    with JOBS_LOCK:
                        if job_id in JOBS:
                            JOBS[job_id]["done"] = done
                            JOBS[job_id]["images"] = list(card_urls + scene_urls)
                    log("金句卡 %d 完成 → %s" % (i + 1, name))
                    _log_gen_meta(log, article_id, name, "card", meta, style)

            # ---- ⑤ 场景配图：纯画面 ----
            for i, s in enumerate(scenes):
                w, h = ASPECT_SIZE.get(s["aspect"], ASPECT_SIZE["3:4"])
                sp = _with_style(s["prompt"], style)
                log("场景图 %d/%d 生成中（%s → %dx%d）…"
                    % (i + 1, len(scenes), s["aspect"], w, h))
                fn, sub, meta = comfy_generate(base, sp, w, h,
                                               "zl_%d_%d" % (article_id, i), cfg)
                data = comfy_download(base, fn, sub)
                name = "ai_%02d_%s.png" % (i, hashlib.md5(data).hexdigest()[:6])
                (out_dir / name).write_bytes(data)
                scene_urls.append("/static/generated/%d/%s" % (article_id, name))
                done += 1
                with JOBS_LOCK:
                    if job_id in JOBS:
                        JOBS[job_id]["done"] = done
                        JOBS[job_id]["images"] = list(card_urls + scene_urls)
                log("场景图 %d 完成 → %s" % (i + 1, name))
                _log_gen_meta(log, article_id, name, "scene", meta, style)

            # ---- ⑥ 二维码图（跟金句卡同组）----
            if card_urls and link:
                qimg = make_qrcode(link, box=400)
                qname = "qr_%s.png" % hashlib.md5(link.encode("utf-8")).hexdigest()[:6]
                qimg.save(str(out_dir / qname), "PNG", optimize=True)
                card_urls.append("/static/generated/%d/%s" % (article_id, qname))

            # ---- ⑦ 入库：只替换本次生成的组 ----
            if card_urls:
                _merge_images(article_id, card_urls, drop_prefix=["qa_", "qr_", "auto_"])
            if scene_urls:
                _merge_images(article_id, scene_urls, drop_prefix=["ai_"])
            with JOBS_LOCK:
                if job_id in JOBS:
                    JOBS[job_id]["images"] = list(card_urls + scene_urls)
                    JOBS[job_id]["done"] = total
            _set(job_id, status="done", finished_at=time.time())
            log("全部完成：金句卡 %d 张 · 场景图 %d 张" % (len(quotes), len(scenes)))

        except Exception as e:
            log("❌ 生成失败：%s" % str(e)[:220])
            # 失败策略：不动任何已有配图，只清理本次已落盘但未入库的残留
            removed = 0
            for u in list(card_urls) + list(scene_urls):
                try:
                    p = GEN_DIR / str(article_id) / Path(u).name
                    if p.is_file():
                        p.unlink()
                        removed += 1
                except Exception:
                    pass
            if removed:
                log("已清理本次未入库的残留图 %d 张" % removed)
            log("已有配图未做任何改动。")
            _set(job_id, status="failed", finished_at=time.time(),
                 error="%s: %s" % (type(e).__name__, e))
        finally:
            try:
                if opts.get("plan_only"):
                    pass
                elif get_comfy_config().get("comfy_auto_shutdown", "1") == "1":
                    log("任务结束，关闭实例…")
                    r = adl_power_off()
                    log("关机结果：" + str(r.get("code") or r)[:80])
                else:
                    log("自动关机已关闭，实例保持运行")
            except Exception as e:
                log("关机异常：" + str(e)[:200])
