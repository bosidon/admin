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
import contextlib
import difflib
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import requests

from illustrate import SIZES, make_qrcode, compose_card_over_bg
import jobs as jobstore

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
    "comfy_cfg": "2.5",
    "comfy_sampler": "euler",
    "comfy_scheduler": "simple",
    "comfy_denoise": "1.0",
    "comfy_lora": "",
    "comfy_auto_shutdown": "1",
    "comfy_instances": "",          # 实例池：每行一台 `uuid|备注`，顺序=优先级；为空则回落 comfy_instance_uuid
}

# aspect → (宽, 高)，与 illustrate.py 的 SIZES 对齐
ASPECT_SIZE = {
    "3:4":    (1080, 1440),
    "1:1":    (1080, 1080),
    "9:16":   (1080, 1920),
    "16:9":   (1920, 1080),
    "2.35:1": (1536, 656),   # ≈1.01M px，16 的倍数（原 1080x460 只有 0.5M 且非 8 倍数）
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

# 采样参数默认（settings 留空 / 填错 → 回落这里；不填 = 与旧行为完全一致）
SAMPLER_DEF = {"steps": 20, "cfg": SAMPLER_CFG, "sampler": SAMPLER_NAME,
               "scheduler": SCHEDULER, "denoise": DENOISE}
DEF_TEXT = {"comfy_sampler": SAMPLER_NAME, "comfy_scheduler": SCHEDULER}


def get_sampler_cfg(cfg=None):
    """采样参数：settings（comfy_*）优先，空 / 非法回落默认"""
    cfg = cfg if cfg is not None else get_comfy_config()

    def _int(v, dv):
        try:
            n = int(float(str(v).strip()))
            return n if n > 0 else dv
        except Exception:
            return dv

    def _flt(v, dv):
        try:
            return float(str(v).strip())
        except Exception:
            return dv

    def _txt(v, dv):
        return str(v or "").strip() or dv

    out = {"steps": _int(cfg.get("comfy_steps"), SAMPLER_DEF["steps"]),
           "cfg": _flt(cfg.get("comfy_cfg"), SAMPLER_DEF["cfg"]),
           "sampler": _txt(cfg.get("comfy_sampler"), SAMPLER_DEF["sampler"]),
           "scheduler": _txt(cfg.get("comfy_scheduler"), SAMPLER_DEF["scheduler"]),
           "denoise": _flt(cfg.get("comfy_denoise"), SAMPLER_DEF["denoise"])}
    if out["cfg"] <= 0:
        out["cfg"] = SAMPLER_DEF["cfg"]
    if not (0 < out["denoise"] <= 1):
        out["denoise"] = SAMPLER_DEF["denoise"]
    return out


def parse_lora(spec):
    """comfy_lora 文本 → [(名称, model权重, clip权重)]

    逗号 / 分号 / 换行分隔；可写 `名称:0.8`（不写 = 1.0）"""
    out = []
    for part in re.split(r"[,;\n]+", str(spec or "")):
        p = part.strip()
        if not p:
            continue
        name, w = p, 1.0
        if ":" in p:
            a, b = p.rsplit(":", 1)
            try:
                w = float(b.strip())
                name = a.strip()
            except Exception:
                name, w = p, 1.0
        if name:
            out.append((name, w, w))
    return out

# 配图风格（14 选 1）：人工在 ① 区下拉选定；出图时把英文风格词追加到正向提示词
# 配图风格（单一事实来源 = configs/styles.json：可编辑、改完刷新页面即生效，不用重启）
# ⚠️ id 一旦上线不要改 —— 历史配图方案与相册日志都按 id 存；加风格只需在 json 里加一条
STYLES_FILE = BASE_DIR / "configs" / "styles.json"
# ⚠️ 这份兜底只在 configs/styles.json 缺失/为空时用于自愈，**不含 guide** → 正常别删那个文件
STYLES_DEFAULT = {
    "default": "artistic",
    "styles": [
        {"id": "realistic", "name": "写实风", "group": "摄影写实", "prompt": "photorealistic, natural light, shallow depth of field, 50mm lens"},
        {"id": "cinematic", "name": "电影感", "group": "摄影写实", "prompt": "cinematic still, anamorphic lens, moody rim light, film grain"},
        {"id": "commercial", "name": "商业质感", "group": "摄影写实", "prompt": "clean studio product photography, softbox lighting, seamless backdrop"},
        {"id": "illustration", "name": "插画风", "group": "绘画插画", "prompt": "flat editorial illustration, soft gradients, clean shapes"},
        {"id": "watercolor", "name": "水彩风", "group": "绘画插画", "prompt": "watercolor painting, wet-on-wet washes, soft paper texture"},
        {"id": "ink", "name": "水墨国风", "group": "绘画插画", "prompt": "Chinese ink wash painting, xuan paper texture, generous negative space"},
        {"id": "anime", "name": "动漫风", "group": "绘画插画", "prompt": "anime key visual, cel shading, clean line art"},
        {"id": "3d", "name": "3D风", "group": "数字渲染", "prompt": "3D render, soft studio light, clay material, subsurface scattering"},
        {"id": "concept", "name": "概念艺术", "group": "数字渲染", "prompt": "concept art, matte painting, dramatic scale, volumetric light"},
        {"id": "artistic", "name": "意境风", "group": "氛围与设计", "prompt": "ethereal dreamscape, mist, soft light rays, surreal calm"},
        {"id": "mystic", "name": "暗黑神秘", "group": "氛围与设计", "prompt": "dark mystic atmosphere, deep shadows, candlelight, quiet symbolism"},
        {"id": "minimal", "name": "极简留白", "group": "氛围与设计", "prompt": "minimalist composition, vast negative space, muted tones"},
        {"id": "editorial", "name": "杂志排版", "group": "氛围与设计", "prompt": "editorial magazine layout, strong grid, space for bold typography"},
        {"id": "collage", "name": "拼贴杂志", "group": "氛围与设计", "prompt": "cut-out paper collage, torn paper edges, layered textures"},
    ],
}
def _norm_style(s):
    """风格条目规范化：id/prompt 必须有；name 缺省用 id、group 缺省归「其它」"""
    if not (isinstance(s, dict) and s.get("id") and s.get("prompt")):
        return None
    sid = str(s["id"]).strip().lower()
    return {"id": sid, "name": str(s.get("name") or sid).strip(),
            "group": str(s.get("group") or "其它").strip(), "prompt": str(s["prompt"]).strip(),
            "guide": str(s.get("guide") or "").strip()}


def load_styles():
    """读 configs/styles.json → {"styles":[{id,name,group,prompt}], "groups":[{name,styles}], "default":id}

    文件不存在/为空 → 用内置默认并补写文件（自愈）；文件坏 → 用内置默认，**不覆盖用户文件**；
    id 重复保留第一次出现。**每次直读**（4KB 文件、微秒级）：改完 json 刷新页面即生效，不用重启。
    ⚠️ 别加 mtime 缓存 —— 实测同一秒内的两次写入 mtime 可能不变，会导致「改了不生效」。"""
    raw = ""
    try:
        raw = STYLES_FILE.read_text(encoding="utf-8")
    except Exception:
        raw = ""
    if not raw.strip():
        try:
            STYLES_FILE.parent.mkdir(parents=True, exist_ok=True)
            STYLES_FILE.write_text(json.dumps(STYLES_DEFAULT, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
        except Exception:
            pass
        styles, dflt = [dict(s) for s in STYLES_DEFAULT["styles"]], STYLES_DEFAULT["default"]
    else:
        styles, dflt = [], ""
        try:
            data = json.loads(raw)
            styles = [x for x in (_norm_style(s) for s in (data.get("styles") or [])) if x]
            dflt = str(data.get("default") or "").strip().lower()
        except Exception:
            styles, dflt = [], ""
        if not styles:
            styles, dflt = [dict(s) for s in STYLES_DEFAULT["styles"]], STYLES_DEFAULT["default"]
    seen, uniq = set(), []
    for s in styles:
        if s["id"] in seen:
            continue
        seen.add(s["id"])
        uniq.append(s)
    groups, names = [], []
    for s in uniq:
        if s["group"] not in names:
            names.append(s["group"])
            groups.append({"name": s["group"], "styles": []})
        groups[names.index(s["group"])]["styles"].append({"id": s["id"], "name": s["name"]})
    if dflt not in seen:
        dflt = uniq[0]["id"] if uniq else "artistic"
    return {"styles": uniq, "groups": groups, "default": dflt}


def style_types():
    """全部风格 id（顺序 = ①区下拉顺序）"""
    return tuple(s["id"] for s in load_styles()["styles"])


def style_prompt_map():
    """id → 追加到正向提示词末尾的英文画风词"""
    return {s["id"]: s["prompt"] for s in load_styles()["styles"]}


def style_label_map():
    """id → 中文名"""
    return {s["id"]: s["name"] for s in load_styles()["styles"]}


def style_guide_map():
    """id → 中文「画面要求」（写给 LLM 看：该风格画面该长什么样、bg 里该避免什么词）
    照片类风格会写明「照常写摄影语言」，换材质类风格写明「不要写摄影媒介词」—— 逐风格写，不能用一句通用提示。"""
    return {s["id"]: (s.get("guide") or "") for s in load_styles()["styles"]}


def style_groups():
    """①区下拉 / 设置页下拉的分组数据 [{name, styles:[{id,name}]}]"""
    return load_styles()["groups"]


def style_default():
    """未指定 / 不认识的风格回落到它"""
    return load_styles()["default"]

# 金句没写 bg 时的兜底背景词（不含画风，画风由 styles.json 的当前风格补）
FALLBACK_BG = ("Mystical serene minimal full-bleed background, soft light, "
               "no text, no letters, no people")


def _with_style(prompt, style):
    """给正向提示词补上风格英文词；没有对应风格就原样返回"""
    frag = style_prompt_map().get(style or "")
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
    "{style_guide_line}"
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

# 运行期状态：任务持久化在库里（data/database.db 的 jobs 表），重启不丢
jobstore.init()
RUN_LOCK = threading.Lock()          # 出图任务独占（一块 GPU）


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


# ---------------- 模型组 / 生成档位（configs/models.json，设置页两个下拉的数据源） ----------------
MODELS_FILE = BASE_DIR / "configs" / "models.json"
MODEL_PRESETS_DEFAULT = [
    {"id": "std20", "name": "标准（20 步）", "lora": "", "steps": "20", "cfg": "2.5"},
    {"id": "fast4", "name": "极速（4 步 · Lightning）",
     "lora": "Qwen-Image-Lightning-4steps-V1.0.safetensors", "steps": "4", "cfg": "1.0"},
    {"id": "fast8", "name": "快速（8 步 · Lightning）",
     "lora": "Qwen-Image-Lightning-8steps-V1.0.safetensors", "steps": "8", "cfg": "1.0"},
]
MODEL_GROUPS_DEFAULT = {
    "groups": [
        {"id": "qwen_image", "name": "Qwen-Image（标准）",
         "unet": "qwen_image_fp8_e4m3fn.safetensors",
         "clip": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
         "vae": "qwen_image_vae.safetensors",
         "presets": MODEL_PRESETS_DEFAULT},
        {"id": "qwen_image_2512", "name": "Qwen-Image 2512",
         "unet": "qwen_image_2512_fp8_e4m3fn.safetensors",
         "clip": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
         "vae": "qwen_image_vae.safetensors",
         "presets": [
             {"id": "std20", "name": "标准（20 步）", "lora": "", "steps": "20", "cfg": "2.5"},
             {"id": "turbo4", "name": "极速（4 步 · Turbo）",
              "lora": "Wuli-Qwen-Image-2512-Turbo-LoRA-4steps-V1.0-bf16_ComfyUi.safetensors",
              "steps": "4", "cfg": "1.0"},
         ]},
    ],
    "presets_default": MODEL_PRESETS_DEFAULT,
}


def _norm_preset(p):
    """档位规范化：id/name 必须有；lora 可为空（= 不加 LoRA）"""
    if not isinstance(p, dict) or not p.get("id") or not p.get("name"):
        return None
    return {"id": str(p["id"]), "name": str(p["name"]), "lora": str(p.get("lora") or ""),
            "steps": str(p.get("steps") or "20"), "cfg": str(p.get("cfg") or "2.5")}


def load_model_config():
    """读 configs/models.json → {"groups": [组(每组带 presets)], "presets_default": [档位]}

    文件不存在/为空 → 用内置默认并补写文件（自愈）；
    文件存在但格式坏 → 用内置默认，**不覆盖用户文件**（保住他的编辑）"""
    raw = ""
    try:
        raw = MODELS_FILE.read_text(encoding="utf-8")
    except Exception:
        raw = ""
    if not raw.strip():
        try:
            MODELS_FILE.parent.mkdir(parents=True, exist_ok=True)
            MODELS_FILE.write_text(
                json.dumps(MODEL_GROUPS_DEFAULT, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
        return json.loads(json.dumps(MODEL_GROUPS_DEFAULT))
    out, presets = [], []
    try:
        data = json.loads(raw)
        for g in (data.get("groups") or []):
            if not (isinstance(g, dict) and g.get("unet") and g.get("clip") and g.get("vae")):
                continue
            out.append({"id": str(g.get("id") or ""), "name": str(g.get("name") or g.get("id") or ""),
                        "unet": str(g["unet"]), "clip": str(g["clip"]), "vae": str(g["vae"]),
                        "presets": [x for x in (_norm_preset(p) for p in (g.get("presets") or [])) if x]})
        presets = [x for x in (_norm_preset(p) for p in (data.get("presets_default") or [])) if x]
    except Exception:
        out, presets = [], []
    if not out:
        return json.loads(json.dumps(MODEL_GROUPS_DEFAULT))
    return {"groups": out,
            "presets_default": presets or [dict(p) for p in MODEL_PRESETS_DEFAULT]}


def load_model_groups():
    """只要模型组（向后兼容壳）"""
    return load_model_config()["groups"]


def get_default_style():
    """配图默认风格：settings.default_style（设置页「默认视觉风格」）；空 / 不认识 → styles.json 的 default"""
    v = ""
    try:
        conn = _settings_db()
        r = conn.execute("SELECT value FROM settings WHERE key='default_style'").fetchone()
        conn.close()
        v = (r["value"] if r else "") or ""
    except Exception:
        pass
    v = str(v).strip().lower()
    return v if v in style_types() else style_default()


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


def adl_status(instance_uuid=""):
    return _adl("GET", "/status", {"instance_uuid": instance_uuid or primary_uuid()})


def adl_snapshot(instance_uuid=""):
    return _adl("GET", "/snapshot", {"instance_uuid": instance_uuid or primary_uuid()})


def adl_list_instances():
    """账号下全部应用实例（看板用，只读）"""
    res = _adl("POST", "/list", {"page_index": 1, "page_size": 50})
    d = res.get("data") or {}
    lst = d.get("list") if isinstance(d, dict) else d
    return lst if isinstance(lst, list) else []


def adl_hosts():
    """主机看板数据：每台实例的状态/规格/单价（只读，不开关机）+ 是否在实例池里（含顺序号）"""
    pool = instance_uuids()
    cur = pool[0] if pool else ""
    out = []
    for it in adl_list_instances():
        u = it.get("uuid") or ""
        h = {"uuid": u, "name": it.get("name") or "", "status": it.get("status") or "",
             "region": it.get("region_name") or "", "spec": it.get("gpu_spec_uuid") or "",
             "alias": "", "price": None, "current": u == cur,
             "in_pool": u in pool, "pool_seq": (pool.index(u) + 1) if u in pool else 0}
        try:
            snap = (adl_snapshot(u).get("data") or {})
            h["alias"] = snap.get("snapshot_gpu_alias_name") or ""
            p = snap.get("payg_price")
            h["price"] = round(float(p) / 1000.0, 2) if p else None
        except Exception:
            pass
        out.append(h)
    return out


def adl_power_on(instance_uuid=""):
    return _adl("POST", "/power_on",
                {"instance_uuid": instance_uuid or primary_uuid(), "payload": "gpu"})


def adl_power_off(instance_uuid=""):
    return _adl("POST", "/power_off", {"instance_uuid": instance_uuid or primary_uuid()})


# ------------------------------------------------------------
# ⭐ 实例池：多实例并行（每台一把锁 → 同台串行、跨台并行）
# ------------------------------------------------------------
_LOCK_POOL = {}                    # uuid -> Lock
_LOCK_GUARD = threading.Lock()
_SLOT = {"n": 0}                   # 正在占用「并行名额」的任务数
_SLOT_GUARD = threading.Lock()


def instance_pool():
    """实例池（有序）：settings.comfy_instances 每行 `uuid|备注`；为空则回落旧的单实例字段"""
    cfg = get_comfy_config()
    out = []
    for ln in str(cfg.get("comfy_instances") or "").replace(",", "\n").split("\n"):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split("|", 1)
        u = parts[0].strip()
        if u and u not in [x["uuid"] for x in out]:
            out.append({"uuid": u, "note": (parts[1].strip() if len(parts) > 1 else "")})
    if not out:
        u = str(cfg.get("comfy_instance_uuid") or "").strip()
        if u:
            out.append({"uuid": u, "note": "（兼容旧配置）"})
    return out


def instance_uuids():
    return [x["uuid"] for x in instance_pool()]


def primary_uuid():
    pool = instance_uuids()
    return pool[0] if pool else ""


def parallel_limit():
    """并行上限 = min(调度器的 gpu 域并发上限, 池内台数)；gpu_parallel 在设置页配（默认 2）"""
    n = 1
    try:
        n = int(jobstore.domain_limit("gpu"))
    except Exception:
        n = 1
    return max(1, min(n, len(instance_uuids()) or 1))


def instance_lock(uuid):
    with _LOCK_GUARD:
        if uuid not in _LOCK_POOL:
            _LOCK_POOL[uuid] = threading.Lock()
        return _LOCK_POOL[uuid]


def _slot_take():
    with _SLOT_GUARD:
        if _SLOT["n"] < parallel_limit():
            _SLOT["n"] += 1
            return True
    return False


def _slot_drop():
    with _SLOT_GUARD:
        if _SLOT["n"] > 0:
            _SLOT["n"] -= 1


def acquire_ready_instance(job_id, log, timeout=1800, boot_timeout=300):
    """挑一台「空闲且可用」的实例并锁住（返回 uuid, lock）
    顺序：池内正在 running 的优先（秒级可用）→ 其余按池序尝试开机（无库存重试）
    全忙/开不起来 → 等待（可被取消）；调用方负责 lock.release() + _slot_drop()"""
    t0, warned = time.time(), False
    while True:
        if jobstore.canceled(job_id):
            raise RuntimeError("已取消")
        pool = instance_pool()
        if not pool:
            raise RuntimeError("未配置应用实例（设置页「实例池」）")
        cands = pool[:max(1, parallel_limit())]
        if _slot_take():
            # ① 先挑已经在 running 且锁空闲的（不用等开机）
            for x in cands:
                lk = instance_lock(x["uuid"])
                if not lk.acquire(blocking=False):
                    continue
                try:
                    if (adl_status(x["uuid"]).get("data") or "") == "running":
                        log("使用实例 %s（已在运行）" % x["uuid"])
                        return x["uuid"], lk
                except Exception:
                    pass
                lk.release()
            # ② 再按池序尝试开机（先关掉 ① 探到的 shutdown 状态就是正常路径）
            for i, x in enumerate(cands):
                lk = instance_lock(x["uuid"])
                if not lk.acquire(blocking=False):
                    continue
                left = len(cands) - i - 1                        # 后面还有候选 → 别在一台上耗满 5 分钟
                try:
                    jobstore.update(job_id, stage="booting", host=x["uuid"])
                    _ensure_instance(x["uuid"], job_id, log,
                                     timeout=(boot_timeout if not left else min(120, boot_timeout)))
                    return x["uuid"], lk
                except Exception as e:
                    log("实例 %s 不可用（%s）%s" % (x["uuid"], str(e)[:70],
                                                "→ 换下一台" if left else ""))
                    lk.release()
            _slot_drop()
        if not warned:
            jobstore.update(job_id, stage="waiting")
            log("等待空闲实例…（%d 台：并行上限 %d）" % (len(cands), parallel_limit()))
            warned = True
        if time.time() - t0 > timeout:
            raise RuntimeError("等待空闲实例超时（%d 分钟）" % (timeout // 60))
        time.sleep(5)


class _InstanceCtx:
    """with instance_ctx(jid, log) as inst: —— 进场拿实例（含开机），退场释放锁与并行名额"""
    def __init__(self, job_id, log):
        self.job_id, self.log = job_id, log
        self.uuid = self.lock = None

    def __enter__(self):
        self.uuid, self.lock = acquire_ready_instance(self.job_id, self.log)
        return self.uuid

    def __exit__(self, *exc):
        try:
            if self.lock:
                self.lock.release()
        finally:
            _slot_drop()
        return False


def instance_ctx(job_id, log):
    return _InstanceCtx(job_id, log)


# ------------------------------------------------------------
# ⭐ 模型名适配：同一份配置跑多台实例（不同实例的同名模型可能在不同子目录）
# ------------------------------------------------------------
_ENUM_CACHE = {}                   # base -> (ts, {kind: [names]})


def instance_enums(base, ttl=600):
    """该实例的模型枚举（缓存 ttl 秒）：unet / clip / vae / lora / ckpt"""
    key = base or ""
    now = time.time()
    hit = _ENUM_CACHE.get(key)
    if hit and (now - hit[0]) < ttl:
        return hit[1]
    oi = requests.get(key.rstrip("/") + "/object_info", timeout=90).json()

    def enum(node, field):
        try:
            return list(oi[node]["input"]["required"][field][0])
        except Exception:
            return []
    d = {"unet_name": enum("UNETLoader", "unet_name"),
         "clip_name": enum("CLIPLoader", "clip_name"),
         "vae_name": enum("VAELoader", "vae_name"),
         "lora_name": enum("LoraLoader", "lora_name"),
         "ckpt_name": enum("CheckpointLoaderSimple", "ckpt_name")}
    _ENUM_CACHE[key] = (now, d)
    return d


def fit_name(kind, name, base):
    """把模型名适配成该实例上的实际路径：精确命中 → 同名文件（不同子目录）→ 子串；找不到原样返回"""
    name = str(name or "").strip()
    if not name:
        return name
    try:
        opts = instance_enums(base).get(kind) or []
    except Exception:
        return name
    if not opts or name in opts:
        return name
    tail = name.split("/")[-1].lower()
    for o in opts:
        if o.split("/")[-1].lower() == tail:
            return o
    for o in opts:
        if tail in o.lower():
            return o
    return name


def fit_workflow(wf, base, logger=None):
    """提交前把工作流里该实例没有的模型名换成实际路径；返回 [(kind, old, new), ...]"""
    changes = []
    try:
        for node in (wf or {}).values():
            ins = (node or {}).get("inputs") or {}
            for k in ("unet_name", "clip_name", "vae_name", "lora_name", "ckpt_name"):
                v = ins.get(k)
                if isinstance(v, str) and v:
                    nv = fit_name(k, v, base)
                    if nv != v:
                        ins[k] = nv
                        changes.append((k, v, nv))
                        if logger:
                            logger("ⓘ 模型名适配：%s → %s" % (v, nv))
    except Exception as e:
        if logger:
            logger("⚠ 模型名适配跳过（%s）" % str(e)[:80])
    return changes


def _comfy_ping(base, timeout=6):
    """快速探一下 /system_stats（配好的地址可能因实例换区/重建而失效）"""
    try:
        return requests.get(base.rstrip("/") + "/system_stats", timeout=timeout).status_code == 200
    except Exception:
        return False


def _snapshot_domain(instance_uuid=""):
    """指定实例的实时域名（AutoDL 换区/重建后域名会变）"""
    try:
        snap = adl_snapshot(instance_uuid).get("data") or {}
    except Exception:
        snap = {}
    dom = str(snap.get("service_6006_domain") or "").strip().rstrip("/")
    if not dom:
        return ""
    return dom if dom.startswith("http") else "https://" + dom


def base_for(instance_uuid="", logger=None):
    """指定实例的 ComfyUI 地址：池中第一台优先用 settings 里配的地址（探得通才用），其余用实时域名"""
    cfg = get_comfy_config()
    u = instance_uuid or primary_uuid()
    base = (cfg.get("comfy_base_url") or "").strip().rstrip("/")
    if base and not base.startswith("http"):
        base = "https://" + base
    if base and u == primary_uuid() and _comfy_ping(base):
        return base
    live = _snapshot_domain(u)
    if live:
        if logger and base and live.rstrip("/") != base.rstrip("/"):
            logger("实例 %s 实时地址：%s" % (u, live))
        return live
    return base


def resolve_base_url(logger=None):
    """（兼容旧调用）池中第一台的 ComfyUI 地址"""
    return base_for(primary_uuid(), logger)


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

# 「检测配置」用：字段 → (中文名, 是否必填)；清单 key 与 settings key 同名
CHECK_FIELDS = (
    ("comfy_unet", "底模 unet", True),
    ("comfy_clip", "文本编码器", True),
    ("comfy_vae", "VAE", True),
    ("comfy_sampler", "采样器", False),
    ("comfy_scheduler", "调度器", False),
)

# 清单来源：settings key → (ComfyUI 节点, 输入字段)
OBJ_NODES = {
    "comfy_unet": ("UNETLoader", "unet_name"),
    "comfy_clip": ("CLIPLoader", "clip_name"),
    "comfy_vae": ("VAELoader", "vae_name"),
    "comfy_lora": ("LoraLoader", "lora_name"),
    "comfy_sampler": ("KSampler", "sampler_name"),
    "comfy_scheduler": ("KSampler", "scheduler"),
}


def comfy_object_info(base, timeout=25):
    """拉 ComfyUI /object_info，抽出可核对的清单（文件名 / 采样器 / 调度器）"""
    r = requests.get(base.rstrip("/") + "/object_info", timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError("HTTP %s" % r.status_code)
    oi = r.json()
    out = {}
    for key, (node, field) in OBJ_NODES.items():
        vals = []
        try:
            spec = (oi.get(node) or {}).get("input") or {}
            for grp in ("required", "optional"):
                f = (spec.get(grp) or {}).get(field)
                if f and isinstance(f[0], list):
                    vals = [str(x) for x in f[0]]
                    break
        except Exception:
            vals = []
        out[key] = vals
    return out


def _near(v, avail):
    """拼错时给近似名建议"""
    try:
        m = difflib.get_close_matches(v, avail, n=1, cutoff=0.55)
        return ("像 %s ？" % m[0]) if m else ""
    except Exception:
        return ""


def check_comfy_config(cfg, lists):
    """逐项核对自由填写的值（只报告，不阻断）；lists = comfy_object_info() 的结果"""
    cfg = cfg or {}
    lists = lists or {}
    items = []
    for key, label, must in CHECK_FIELDS:
        v = str(cfg.get(key) or "").strip()
        av = lists.get(key) or []
        if not v:
            if must:
                ok, hint = False, "未填写"
            else:
                ok, hint = True, "留空 = 默认 %s" % DEF_TEXT.get(key, "")
        elif v in av:
            ok, hint = True, ""
        else:
            ok = False
            hint = _near(v, av) or ("清单里没有这个名字（可选项 %d 个）" % len(av))
        items.append({"key": key, "label": label, "value": v, "ok": ok, "hint": hint})

    loras = []
    for nm, w, _ in parse_lora(cfg.get("comfy_lora")):
        av = lists.get("comfy_lora") or []
        if nm in av:
            loras.append({"name": nm, "weight": w, "ok": True, "hint": ""})
        else:
            loras.append({"name": nm, "weight": w, "ok": False,
                          "hint": _near(nm, av) or "清单里没有这个 LoRA"})

    sp = get_sampler_cfg(cfg)
    notes = []
    low = " ".join([nm.lower() for nm, _, _ in parse_lora(cfg.get("comfy_lora"))])
    if "lightning" in low:
        notes.append("LoRA 含 Lightning 加速模型：建议 cfg=1.0、步数 4~8（当前 cfg=%s、步数=%s）"
                     % (sp["cfg"], sp["steps"]))
    if "edit" in str(cfg.get("comfy_unet") or "").lower():
        notes.append("底模是 Edit 系（需要输入图）：当前出图流程没有输入图，可能跑不通")
    if str(cfg.get("comfy_clip") or "").strip() and "qwen" not in str(cfg.get("comfy_clip")).lower():
        notes.append("出图走 Qwen-Image：文本编码器通常是 qwen 系（clip 类型固定 qwen_image）")

    return {"items": items, "loras": loras, "notes": notes, "sample": sp,
            "counts": {k: len(v) for k, v in lists.items()}}


def build_workflow(prompt, w, h, seed, cfg, prefix, neg):
    """组装出图工作流：采样参数取 settings（留空 = 旧默认）；comfy_lora 非空时串 LoraLoader 链"""
    sp = get_sampler_cfg(cfg)
    model_ref, clip_ref = ["1", 0], ["2", 0]
    loras = parse_lora(cfg.get("comfy_lora"))
    wf = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": cfg.get("comfy_unet"), "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": cfg.get("comfy_clip"), "type": "qwen_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": cfg.get("comfy_vae")}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": clip_ref}},
        "5": {"class_type": "CLIPTextEncode",
              "inputs": {"text": neg, "clip": clip_ref}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "7": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": sp["steps"], "cfg": sp["cfg"],
                         "sampler_name": sp["sampler"], "scheduler": sp["scheduler"],
                         "denoise": sp["denoise"], "model": model_ref,
                         "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": prefix, "images": ["8", 0]}},
    }
    for i, (nm, sw_m, sw_c) in enumerate(loras):
        nid = str(10 + i)
        wf[nid] = {"class_type": "LoraLoader",
                   "inputs": {"lora_name": nm, "strength_model": sw_m,
                              "strength_clip": sw_c, "model": model_ref, "clip": clip_ref}}
        model_ref, clip_ref = [nid, 0], [nid, 1]
    wf["7"]["inputs"]["model"] = model_ref
    wf["4"]["inputs"]["clip"] = clip_ref
    wf["5"]["inputs"]["clip"] = clip_ref
    return wf


def comfy_generate(base, prompt, w, h, prefix, cfg, timeout=600, seed=None):
    """提交一张图并等待完成，返回 (filename, subfolder, meta)

    meta = 这张图最终提交给 ComfyUI 的提示词与参数（供任务日志/落库排查）"""
    if seed is None:
        seed = random.randint(1, 2 ** 31 - 1)
    neg_text = load_negative_prompt()                # 文件里的「负面提示词」段 + 强制安全词
    wf = build_workflow(prompt, w, h, seed, cfg, prefix, neg_text)
    sp = get_sampler_cfg(cfg)
    meta = {"prompt": prompt, "neg": neg_text, "seed": seed,
            "steps": sp["steps"], "cfg": sp["cfg"],
            "sampler": sp["sampler"], "scheduler": sp["scheduler"],
            "denoise": sp["denoise"], "unet": cfg.get("comfy_unet") or "",
            "lora": cfg.get("comfy_lora") or "",
            "width": w, "height": h}
    meta["name_fit"] = fit_workflow(wf, base)      # 按实例适配模型名（多实例目录结构可能不同）
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
                raise RuntimeError("ComfyUI 执行出错: %s" % _wf_err_text(entry.get("status")))
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


def _wf_err_text(status):
    """从 ComfyUI history 的 status 里抽出真正有用的报错（节点类型 + 异常信息）"""
    msgs = (status or {}).get("messages") or []
    for ev, data in reversed(msgs):
        if ev in ("execution_error", "execution_interrupted") and isinstance(data, dict):
            return "%s (node %s): %s" % (data.get("node_type"), data.get("node_id"),
                                         str(data.get("exception_message") or
                                             data.get("exception_type") or "")[:200])
    try:
        return json.dumps(msgs, ensure_ascii=False)[:240]
    except Exception:
        return str(msgs)[:240]


def comfy_upload(base, path, name=None, timeout=120):
    """把本地图上传到 ComfyUI 的 input 目录，返回可直接喂 LoadImage 的文件名"""
    p = Path(path)
    nm = name or p.name
    with open(str(p), "rb") as f:
        r = requests.post(base + "/upload/image",
                          files={"image": (nm, f, "application/octet-stream")},
                          data={"overwrite": "true", "type": "input"}, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError("上传到 ComfyUI 失败 HTTP %s: %s" % (r.status_code, r.text[:200]))
    try:
        d = r.json() or {}
    except Exception:
        d = {}
    nm = d.get("name") or nm
    sub = d.get("subfolder") or ""
    return (sub + "/" + nm) if sub else nm


def comfy_run_wf(base, wf, timeout=900, poll=3):
    """提交**任意工作流**并等待完成，返回 [(filename, subfolder), ...]"""
    r = requests.post(base + "/prompt", json={"prompt": wf}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError("提交失败 HTTP %s: %s" % (r.status_code, r.text[:300]))
    pid = r.json()["prompt_id"]
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(poll)
        try:
            hh = requests.get(base + "/history/" + pid, timeout=30).json()
        except Exception:
            continue
        if pid in hh:
            entry = hh[pid]
            st = entry.get("status") or {}
            if st.get("status_str") == "error":
                raise RuntimeError("ComfyUI 执行出错: %s" % _wf_err_text(st))
            out = []
            for node in (entry.get("outputs") or {}).values():
                for im in (node.get("images") or []):
                    out.append((im.get("filename"), im.get("subfolder", "")))
            if out:
                return out
            if st.get("completed"):
                raise RuntimeError("工作流执行完但没有图片输出")
    raise RuntimeError("加工超时（%ds）" % timeout)


def _run_wf_one(base, wf, timeout=900, logger=None):
    """跑一个工作流并取回第一张图的字节"""
    fit_workflow(wf, base, logger)                 # 按实例适配模型名
    imgs = comfy_run_wf(base, wf, timeout=timeout)
    return comfy_download(base, imgs[0][0], imgs[0][1])


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
            "seed, steps, cfg, sampler, scheduler, width, height, unet, lora, denoise) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (article_id, name, kind, meta.get("prompt", ""), meta.get("neg", ""), style,
             meta.get("seed"), meta.get("steps"), meta.get("cfg"),
             meta.get("sampler"), meta.get("scheduler"),
             meta.get("width"), meta.get("height"),
             meta.get("unet", ""), meta.get("lora", ""), meta.get("denoise")))
        conn.commit()
        conn.close()
        return None
    except Exception as e:
        return "%s: %s" % (type(e).__name__, str(e)[:120])


def _log_gen_meta(log, article_id, name, kind, meta, style):
    """任务日志打印 + 落库：这张图最终提交的提示词与参数"""
    extra = (" · LoRA " + meta["lora"]) if meta.get("lora") else ""
    log("\u24d8 参数：seed=%s · steps=%s · cfg=%s · %s/%s · %dx%d · 风格 %s · 底模 %s%s"
        % (meta.get("seed"), meta.get("steps"), meta.get("cfg"), meta.get("sampler"),
           meta.get("scheduler"), meta.get("width"), meta.get("height"),
           style_label_map().get(style, style) or "-", (meta.get("unet") or "-"), extra))
    for _k, _old, _new in (meta.get("name_fit") or []):
        log("\u24d8 模型名适配：%s → %s" % (_old, _new))
    log("\u24d8 正向：" + (meta.get("prompt") or ""))
    log("\u24d8 负面：" + (meta.get("neg") or ""))
    err = _save_gen_log(article_id, name, kind, meta, style)
    if err:
        log("⚠ 参数未落库（%s）" % err)


# ============================================================
# 配置（LLM / 任务并发）
# ============================================================
LLM_DEFAULTS = {"llm_base_url": "https://api.deepseek.com/v1/chat/completions",
                "llm_model": "deepseek-chat", "llm_api_key": ""}


def _env_all():
    """读 /var/www/.env（或项目 .env）里的键值"""
    out = {}
    for p in (BASE_DIR / ".env", Path("/var/www/.env")):
        try:
            if p.exists():
                for ln in p.read_text(encoding="utf-8").splitlines():
                    ln = ln.strip()
                    if ln and not ln.startswith("#") and "=" in ln:
                        k, v = ln.split("=", 1)
                        out[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            pass
    return out


def get_llm_config():
    """LLM 参数：settings 表优先 → DEEPSEEK_API_KEY 环境变量 → 默认 DeepSeek 官网"""
    cfg = dict(LLM_DEFAULTS)
    try:
        conn = _settings_db()
        for r in conn.execute("SELECT key, value FROM settings WHERE key IN "
                              "('llm_base_url','llm_model','llm_api_key')"):
            if r["value"]:
                cfg[r["key"]] = r["value"]
        conn.close()
    except Exception:
        pass
    if not cfg.get("llm_api_key"):
        cfg["llm_api_key"] = _env_all().get("DEEPSEEK_API_KEY", "")
    return cfg


def _set(job_id, **kw):
    jobstore.update(job_id, **kw)


def _log(job_id, msg):
    jobstore.log(job_id, msg)


def get_job(job_id):
    return jobstore.get(job_id)


def _gc_jobs():
    """任务已持久化，无需清内存（历史任务按需在查询侧限条数）"""
    return None


def shutdown_instance():
    """手动关机（前端兜底按钮）"""
    res = adl_power_off()
    return {"ok": res.get("code") == "Success",
            "msg": res.get("msg") or res.get("code") or "",
            "raw": res}

# ============================================================
# 叠字卡任务（AI 背景 + PIL 叠字）
# ============================================================
def _read_article_full(article_id):
    conn = _content_db()
    row = conn.execute(
        "SELECT id, title, content_md, platform, content_type, promo_link, owner_id, promo_src "
        "FROM articles WHERE id=?", (article_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _link_of(art, article_id):
    if art.get('promo_link'):
        return art['promo_link']
    if art.get('owner_id'):
        return 'https://xianbao.love/?ref=%s&src=%s' % (
            art['owner_id'], art.get('promo_src') or str(article_id))
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


def uid_ok(v):
    """DeepSeek user_id 规范：只允许 [a-zA-Z0-9-_]，≤512 字符，其余替换为 _"""
    return re.sub(r"[^A-Za-z0-9\-_]", "_", str(v or ""))[:512]




def gen_storyboard(article_content, llm_cfg):
    """根据文案用 LLM 生成分镜脚本"""
    import os
    prompts_dir = os.path.dirname(os.path.abspath(__file__)) + "/prompts"
    prompt_md = open(prompts_dir + "/video_storyboard.md", encoding="utf-8").read()
    user_msg = prompt_md.replace("{{CONTENT}}", article_content[:8000])
    url = (llm_cfg.get("llm_base_url") or "https://api.deepseek.com/v1").rstrip("/")
    if "/chat/completions" not in url:
        url += "/chat/completions"
    data, err = _llm_json(url, llm_cfg.get("llm_api_key", ""),
                   llm_cfg.get("llm_model") or "deepseek-chat",
                   [{"role": "system", "content": "你是一位专业短视频分镜导演。只输出 JSON，不要其他文字。"},
                    {"role": "user", "content": user_msg}])
    if err:
        raise RuntimeError("LLM 调用失败: " + err[:200])
    if not isinstance(data, dict) or "shots" not in data:
        raise RuntimeError("LLM 返回格式错误: " + str(data)[:200])
    for i, sh in enumerate(data["shots"]):
        sh.setdefault("idx", i); sh.setdefault("duration_s", 5)
        sh.setdefault("shot_type", "中景"); sh.setdefault("visual_prompt", "")
        sh.setdefault("subtitle", ""); sh.setdefault("music_hint", "none")
        sh.setdefault("template_hint", "U02")
    return data

def _llm_json(url, key, model, messages, tries=3, user_id=""):
    """调 LLM 并取出 JSON 对象 → (json|None, 错误文案)

    - 退避重试：429（并发限流）/ 5xx / 超时 / 空响应 → 等 1s、2s、4s 再试（默认 3 次）
    - user_id：DeepSeek 用它做内容安全、KVCache、调度隔离；格式不合法会先规范化
    - 注意：DeepSeek 排队期间非流式请求会持续返回空行，requests+json() 能正常处理
    """
    err = "LLM 未返回 JSON"
    body = {"model": model, "messages": messages}
    _uid = uid_ok(user_id)
    if _uid:
        body["user_id"] = _uid
    delay = 1
    for i in range(max(1, tries)):
        try:
            r = requests.post(url, headers={"Authorization": "Bearer " + key,
                                            "Content-Type": "application/json"},
                              json=body, timeout=180)
            if r.status_code == 429:
                err = "LLM 限流 429（第 %d 次）" % (i + 1)
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code >= 500:
                err = "LLM 服务端 %s（第 %d 次）" % (r.status_code, i + 1)
                time.sleep(delay)
                delay *= 2
                continue
            raw = r.json()["choices"][0]["message"]["content"]
            if not (raw or "").strip():
                err = "LLM 返回空内容（第 %d 次）" % (i + 1)
                time.sleep(delay)
                delay *= 2
                continue
        except Exception as e:
            err = "LLM 调用失败（%s）" % str(e)[:60]
            time.sleep(delay)
            delay *= 2
            continue
        j = _pick_json(raw)
        if j is not None:
            return j, None
        err = "LLM 返回的 JSON 解析失败"
        time.sleep(0.5)
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


# 画幅两级：条目文本里写的比例优先；没有就按文案类型档位兜底（平台不参与）


_ASPECT_PAT = [
    ("2.35:1", ("2.35:1", "2.35", "21:9", "2.39:1")),
    ("16:9", ("16:9", "1.78:1")),
    ("9:16", ("9:16",)),
    ("1:1", ("1:1",)),
    ("3:4", ("3:4",)),
]
def _norm_txt(s):
    """统一全角冒号/斜杠、压掉冒号两侧空格，便于匹配「2.35 : 1」这类写法"""
    t = str(s or "").replace("：", ":").replace("／", "/")
    return re.sub(r"\s*:\s*", ":", t)


def _aspect_from_text(txt):
    """从文本里认出画幅（2.35:1 / 16:9 / 9:16 / 1:1 / 3:4），没有返回空串"""
    t = _norm_txt(txt)
    for asp, keys in _ASPECT_PAT:
        for k in keys:
            if k in t:
                return asp
    return ""


def _clean_aspect_words(txt):
    """去掉提示词里的比例字样（画幅由程序设定，留在提示词里会让模型画成宽银幕黑边）"""
    t = _norm_txt(txt)
    for _, keys in _ASPECT_PAT:
        for k in sorted(keys, key=len, reverse=True):
            t = t.replace(k, "")
    t = re.sub(r"(?i)\b(aspect\s+ratio|aspect|ratio)\b", "", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.replace(" ,", ",").replace(" .", ".").replace(",,", ",").strip(" ,.、-()（）")


def _aspect_of(art, item=None):
    """画幅两级：① 条目文本（title → cn → bg）里明确的画幅 ② 文案类型档位

    平台不参与画幅决策（平台的画幅偏好写在提示词里，由 AI 把比例写进文本，程序只认文本）
    """
    item = item or {}
    for k in ("title", "cn", "bg"):
        asp = _aspect_from_text(item.get(k))
        if asp:
            return asp, k
    return _default_aspect(art), "档位"


def _default_aspect(art, title=""):
    """画幅兜底：只看文案类型（不看平台）

    article → 3:4 · short_video / speech → 9:16 · long_video → 16:9
    要别的比例：在提示词/条目文本里写明（如「9:16」），由 _aspect_from_text 识别
    """
    ct = str((art or {}).get("content_type") or "").strip()
    if ct in ("short_video", "speech"):
        return "9:16"
    if ct == "long_video":
        return "16:9"
    return "3:4"


def _plan_out(data, default_style="", art=None):
    """把库里的 image_script / LLM 返回的 JSON 统一成一个方案对象（images[] 为唯一来源）"""
    if isinstance(data, list):
        data = {"scenes": data}
    if not isinstance(data, dict):
        data = {}
    art = art or {}
    images = []
    for it in (data.get("images") or []):
        asp0 = _aspect_of(art, it)[0] if isinstance(it, dict) else _default_aspect(art)
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
    return {"style": style if style in style_types() else default_style,
            "platform": str(data.get("platform") or "").strip(),
            "images": images, "quotes": quotes, "scenes": scenes}


def read_plan(article_id, default_style=None):
    """读配图方案：统一成 images[]（旧 quotes/scenes 自动映射），并派生 ② 出图用的两组视图

    style 只认 configs/styles.json 里的风格之一；旧配色值（purple/dark/gold/maya）一律忽略，回落默认风格。
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
    style = style if style in style_types() else get_default_style()
    sys_prompt = load_plan_prompt()
    plat = (art.get("platform") or "").strip()
    ctype = (art.get("content_type") or "").strip()
    user_msg = (PLAN_USER_TEMPLATE
                .replace("{platform}", PLATFORM_LABEL.get(plat, plat or "未指定"))
                .replace("{platform_id}", plat or "未指定")
                .replace("{ctype_label}", CTYPE_LABEL.get(ctype, ctype or "图文文章"))
                .replace("{ctype_id}", ctype or "article")
                .replace("{style_label}", style_label_map().get(style, style))
                .replace("{style_id}", style)
                .replace("{style_guide_line}",
                         ("- 该风格的画面要求（按它写画面内容与提示词，不要写与它冲突的媒介/风格词）：%s\n"
                          % style_guide_map().get(style, "")) if style_guide_map().get(style, "") else "")
                .replace("{title}", title)
                .replace("{content}", content))
    with jobstore.LLM_GATE:                 # LLM 域闸门：与队列共用同一并发上限
        j, err = _llm_json(url, key, model,
                           [{"role": "system", "content": sys_prompt},
                            {"role": "user", "content": user_msg}],
                           user_id=("p%s" % (art.get("owner_id") or "") if art.get("owner_id") else ""))
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
    _frag = style_prompt_map().get(plan.get("style") or "")
    palette = ("风格：本次整套配图的风格是 %s（%s），画面请按这个风格来（画风英文词由系统追加，不必自己写）。\n"
               % (style_label_map().get(plan.get("style"), plan.get("style")), _frag)) if _frag else ""
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
    _art = _read_article_full(article_id) or {}
    _uid = ("p%s" % _art.get("owner_id")) if _art.get("owner_id") else ""
    with jobstore.LLM_GATE:                 # 同上：重写是同步接口，也要占 LLM 域名额
        j, err = _llm_json(url, key, model, [{"role": "user", "content": prompt}], user_id=_uid)
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
# 统一生成任务（叠字卡 + 画面，一次开机一次关机）
# ============================================================
def start_generate(article_id, opts, llm_cfg=None, card_size="xiaohongshu",
                   card_want=0, scene_count=0, owner="", owner_id=""):
    """任务入队（幂等）：① 生成方案 → kind=plan；② 出图 → kind=images
    返回 {ok, job_id, kind, queue_pos, reused}；llm_cfg 仅为兼容旧调用保留
    （后台任务自己从 settings / .env 取 key，不再经前端传）
    """
    if not _read_article_full(article_id):
        return {"error": "文章不存在"}
    plan_only = bool(opts.get("plan_only"))
    want_cards = bool(opts.get("cards"))
    want_scenes = bool(opts.get("scenes"))
    if not (want_cards or want_scenes) and not plan_only:
        return {"error": "请至少勾选一组要出图的条目"}
    if not plan_only:
        cfg = get_comfy_config()
        if not instance_uuids() or not cfg.get("comfy_api_token"):
            return {"error": "未配置实例池 / Token，请去「设置」页填写"}
    kind = "plan" if plan_only else "images"
    payload = {"cards": want_cards, "scenes": want_scenes, "replan": bool(opts.get("replan")),
               "plan_only": plan_only, "card_size": card_size,
               "card_want": int(card_want or 0), "scene_count": int(scene_count or 0)}
    total = 0 if plan_only else ((int(card_want or 0) if want_cards else 0)
                                 + (int(scene_count or 0) if want_scenes else 0))
    jid, reused = jobstore.DISPATCHER.enqueue(kind, article_id, payload, total, priority=10,
                                              owner=owner, owner_id=owner_id)
    return {"ok": True, "job_id": jid, "kind": kind, "reused": reused,
            "queue_pos": jobstore.queue_pos(jid)}


# ============================================================
# 任务执行体（由 jobs.DISPATCHER 调度；状态/日志全部落 jobs 表）
# ============================================================
def _ensure_instance(instance_uuid, job_id, log, timeout=300):
    """确保指定实例 running（已在运行则跳过），返回是否本次开机"""
    st = adl_status(instance_uuid).get("data") or ""
    if st == "running":
        log("实例已在运行，跳过开机")
        return False
    log("启动实例…（当前状态 %s）" % (st or "未知"))
    # ⚠️ 开机常被「当前算力规格暂无库存，请修改配置或稍等再试」拒绝 → 每 30s 重试，最长 5 分钟
    # （实测重试即可抢到；不是配置错误，所以只有这类文案才重试，其它错误立刻失败）
    res, t0 = {}, time.time()
    while time.time() - t0 < timeout:
        if jobstore.canceled(job_id):
            raise RuntimeError("已取消")
        res = adl_power_on(instance_uuid)
        if res.get("code") == "Success":
            break
        msg = str(res.get("msg") or res.get("code") or "")
        if ("库存" in msg) or ("稍等" in msg):
            log("开机被拒：%s —— 30s 后重试" % msg)
            time.sleep(30)
            continue
        raise RuntimeError("开机失败：%s" % msg)
    else:
        raise RuntimeError("开机失败：%s" % (res.get("msg") or res.get("code") or "未知"))
    log("开机指令已下发")
    for _ in range(60):
        if jobstore.canceled(job_id):
            raise RuntimeError("已取消")
        time.sleep(3)
        if (adl_status(instance_uuid).get("data") or "") == "running":
            log("实例已运行")
            return True
    raise RuntimeError("等待实例 running 超时")


def _shutdown_if_idle(job_id, instance_uuid, log):
    """收尾：还有排队任务（可能要用这台）就保持运行，否则关掉本次用的这台实例"""
    try:
        if get_comfy_config().get("comfy_auto_shutdown", "1") != "1":
            log("自动关机已关闭，实例保持运行")
            return
        gpu_kinds = ("images", "cutout", "edit", "stitch", "txt2img")   # 漏一个就会把还有任务的实例关掉
        pending = sum(jobstore.queued_count(k) for k in gpu_kinds)
        if pending > 0:
            log("还有 %d 个排队任务，实例 %s 保持运行" % (pending, instance_uuid))
            return
        log("队列已空，关闭实例 %s…" % instance_uuid)
        r = adl_power_off(instance_uuid)
        code = str(r.get("code") or r)
        msg = str(r.get("msg") or "")
        if code == "Success" or "BadRequest" in code:      # 本来就没开机 → 也算完成（幂等）
            log("关机完成")
        else:
            log("关机结果：" + (code + " " + msg)[:100])
    except Exception as e:
        log("关机异常：" + str(e)[:200])


def run_plan_job(job):
    """① 生成方案：纯 LLM，不开机、不占显存（可与出图任务并行）"""
    jid, article_id = job["id"], job.get("article_id")
    opts = job.get("payload") or {}
    _set(jid, stage="planning")
    _log(jid, "AI 正在分析文案、设计配图方案…")
    plan = read_plan(article_id)
    newp, reason = gen_plan(_read_article_full(article_id) or {}, get_llm_config(),
                            int(opts.get("card_want") or 0), int(opts.get("scene_count") or 0),
                            style=plan.get("style") or "")
    if not newp:
        raise RuntimeError("方案生成失败：%s" % (reason or "未知原因"))
    save_plan(article_id, newp)
    _log(jid, "方案已生成：%d 张（%s）· 风格 %s"
         % (len(newp["images"]), _type_brief(newp["images"]),
            style_label_map().get(newp["style"], newp["style"])))
    _set(jid, total=0, done=0, plan_images=newp["images"], quotes=newp["quotes"],
         scenes=newp["scenes"], style=newp["style"], want_cards=False, want_scenes=False)


def _image_numbers(plan, want_cards, want_scenes):
    """出图文件名用的编号：= 该条目在 ① 区清单（plan["images"]）里的位置，1 起。

    ⚠️ 绝不能用「本次生成的第几张」—— 只勾一条重出时，那会算出 1，
    直接覆盖掉第 1 条的文件（数据丢失）。编号必须与清单位置绑定。
    返回 (card_nos, scene_nos)，与 plan["quotes"]/["scenes"] 顺序一一对应。"""
    plan = plan or {}
    items = plan.get("images") or []
    n_card = len([q for q in (plan.get("quotes") or []) if q.get("on", True)])
    n_scene = len([s for s in (plan.get("scenes") or []) if s.get("on", True)])
    card_nos = [n for n, it in enumerate(items, 1)
                if it.get("type") != "photo" and it.get("on", True)]
    scene_nos = [n for n, it in enumerate(items, 1)
                 if it.get("type") == "photo" and it.get("on", True)]
    if len(card_nos) != n_card:          # 兜底：方案没有 images[] 时退化为顺序号
        card_nos = list(range(1, n_card + 1))
    if len(scene_nos) != n_scene:
        scene_nos = list(range(1, n_scene + 1))
    return (card_nos if want_cards else []), (scene_nos if want_scenes else [])


def _sync_materials(article_id, job=None):
    """配图产物入库（素材库）：只增不删；失败只记日志，绝不影响出图"""
    try:
        import materials as mat
        n = mat.sync_article_images(article_id,
                                    owner_id=(job or {}).get("owner_id"),
                                    owner=(job or {}).get("owner") or "")
        if n and job:
            _log(job["id"], "素材库已入库 %d 张" % n)
    except Exception as e:
        try:
            if job:
                _log(job["id"], "素材入库跳过：%s" % str(e)[:120])
        except Exception:
            pass


def run_images_job(job):
    """② 出图：一次开机 → 出完本任务 → 队列空了才关机"""
    jid, article_id = job["id"], job.get("article_id")
    opts = job.get("payload") or {}
    want_cards = bool(opts.get("cards"))
    want_scenes = bool(opts.get("scenes"))
    card_size = opts.get("card_size") or "xiaohongshu"
    cfg = get_comfy_config()
    art = _read_article_full(article_id) or {}
    link = _link_of(art, article_id)
    plan = read_plan(article_id)
    quotes = [q for q in plan["quotes"] if q.get("on", True)] if want_cards else []
    scenes = [s for s in plan["scenes"] if s.get("on", True)] if want_scenes else []
    if want_cards and not quotes:
        raise RuntimeError("方案里没有勾选的条目 —— 请先在「① 生成方案」里勾选要出的条目")
    if want_scenes and not scenes:
        raise RuntimeError("方案里没有勾选的场景 —— 请先在「① 生成方案」里勾选要出的条目")
    # 文件名 = 条目在 ① 区清单里的固定编号 → 同名覆盖原图（URL 恒定、清单不用改）
    card_nos, scene_nos = _image_numbers(plan, want_cards, want_scenes)
    style = plan.get("style") or ""
    if style not in style_types():
        style = get_default_style()
    total = len(quotes) + len(scenes)
    _set(jid, quotes=plan["quotes"], scenes=plan["scenes"], style=style, total=total,
         want_cards=want_cards, want_scenes=want_scenes)
    card_urls, scene_urls, done = [], [], 0

    with instance_ctx(jid, lambda m: _log(jid, m)) as inst:   # 多实例：按池顺序挑一台空闲可用的（同台串行、跨台并行）
        try:
            _set(jid, stage="ready", host=inst)
            base = base_for(inst, logger=lambda m: _log(jid, m))
            if not base:
                raise RuntimeError("拿不到 ComfyUI 地址")
            _log(jid, "ComfyUI 地址：" + base)
            if not comfy_ready(base, timeout=300, logger=lambda m: _log(jid, m)):
                raise RuntimeError("ComfyUI 300s 内未就绪")
            _log(jid, "ComfyUI 已就绪")
            _set(jid, stage="generating")

            out_dir = GEN_DIR / str(article_id)
            out_dir.mkdir(parents=True, exist_ok=True)

            # ④ 叠字卡路径（旧契约方案）：AI 满版背景 + PIL 叠字
            if quotes:
                cw, ch = SIZES.get(card_size, SIZES["xiaohongshu"])
                for i, q in enumerate(quotes):
                    if jobstore.canceled(jid):
                        raise RuntimeError("已取消")
                    bgp = _with_style(_clean_aspect_words(q.get("bg") or FALLBACK_BG), style)
                    _log(jid, "配图 %d/%d 出背景中（%dx%d）…" % (i + 1, len(quotes), cw, ch))
                    fn, sub, meta = comfy_generate(base, bgp, cw, ch,
                                                   "qcbg_%d_%d" % (article_id, i), cfg)
                    bg = comfy_download(base, fn, sub)
                    card = compose_card_over_bg(bg, q["text"], size=card_size, qr_link=None)
                    name = "ai_%02d.png" % card_nos[i]
                    card.save(str(out_dir / name), "PNG", optimize=True)
                    card_urls.append("/static/generated/%d/%s" % (article_id, name))
                    done += 1
                    _set(jid, done=done, images=list(card_urls + scene_urls))
                    _log(jid, "配图 %d 完成 → %s" % (i + 1, name))
                    _log_gen_meta(lambda m: _log(jid, m), article_id, name, "card", meta, style)

            # ⑤ 场景/画面：纯出图
            for i, s in enumerate(scenes):
                if jobstore.canceled(jid):
                    raise RuntimeError("已取消")
                w, h = ASPECT_SIZE.get(s["aspect"], ASPECT_SIZE["3:4"])
                sp = _with_style(_clean_aspect_words(s["prompt"]), style)
                _log(jid, "画面 %d/%d 生成中（%s → %dx%d）…"
                     % (i + 1, len(scenes), s["aspect"], w, h))
                fn, sub, meta = comfy_generate(base, sp, w, h, "zl_%d_%d" % (article_id, i), cfg)
                data = comfy_download(base, fn, sub)
                name = "ai_%02d.png" % scene_nos[i]
                (out_dir / name).write_bytes(data)
                scene_urls.append("/static/generated/%d/%s" % (article_id, name))
                done += 1
                _set(jid, done=done, images=list(card_urls + scene_urls))
                _log(jid, "画面 %d 完成 → %s" % (i + 1, name))
                _log_gen_meta(lambda m: _log(jid, m), article_id, name, "scene", meta, style)

            # ⑥ 二维码图
            if card_urls and link:
                qimg = make_qrcode(link, box=400)
                qname = "qr.png"
                qimg.save(str(out_dir / qname), "PNG", optimize=True)
                card_urls.append("/static/generated/%d/%s" % (article_id, qname))

            # ⑦ 入库：只增不删 —— 同名覆盖下同条目的 URL 恒定；旧引用/孤儿一律保留，绝不清组
            if card_urls or scene_urls:
                _merge_images(article_id, list(card_urls) + list(scene_urls))
            _set(jid, images=list(card_urls + scene_urls), done=total)
            _log(jid, "全部完成：配图 %d 张" % (len(quotes) + len(scenes)))
        except Exception as e:
            _log(jid, "❌ 生成失败：%s" % str(e)[:220])
            # ⚠️ 固定文件名（同名覆盖）下**不许清理**：本次写出的文件就是该条目的正式文件，
            # 删掉等于把用户唯一的图删了。失败只报错：文件保留、清单不动，重跑即覆盖。
            _log(jid, "本次已出图 %d 张（同名覆盖，保留不动）；配图清单未做任何改动。"
                 % (len(card_urls) + len(scene_urls)))
            raise RuntimeError(str(e)[:300])
        finally:
            _sync_materials(article_id, job)
            _shutdown_if_idle(jid, inst, lambda m: _log(jid, m))


def run_material_job(job):
    """素材加工：抠图(cutout) / 图生图(edit) / 拼版(stitch) —— 一次开机 → 做完 → 队列空才关机"""
    import materials as mat
    jid = job["id"]
    kind = job.get("kind") or ""
    opts = job.get("payload") or {}
    ids = opts.get("ids") or []
    params = opts.get("params") or {}
    title = {"cutout": "抠图", "edit": "图生图", "stitch": "拼版"}.get(kind, kind)
    rows = [r for r in (mat.get_material(i) for i in ids) if r]
    err = mat.validate(kind, rows, params)
    if err:
        raise RuntimeError(err)
    cfg = get_comfy_config()
    if not instance_uuids() or not cfg.get("comfy_api_token"):
        raise RuntimeError("未配置实例池 / Token，请去「设置」页填写")
    total = 1 if kind == "stitch" else len(rows)
    _set(jid, total=total, done=0)
    _log(jid, "%s：%d 张素材" % (title, len(rows)))
    results, done = [], 0
    with instance_ctx(jid, lambda m: _log(jid, m)) as inst:   # 多实例：挑一台空闲可用的
        try:
            _set(jid, stage="ready", host=inst)
            base = base_for(inst, logger=lambda m: _log(jid, m))
            if not base:
                raise RuntimeError("拿不到 ComfyUI 地址")
            _log(jid, "ComfyUI 地址：" + base)
            if not comfy_ready(base, timeout=300, logger=lambda m: _log(jid, m)):
                raise RuntimeError("ComfyUI 300s 内未就绪")
            _set(jid, stage="generating")
            aid = (rows[0].get("article_id") if rows else None) or 0
            outdir = mat.out_dir(aid)
            srcs = [mat.url_to_path(r.get("file_path")) for r in rows]

            if kind == "stitch":
                _log(jid, "拼版：%d 张 → %s（%s px）"
                     % (len(rows), params.get("mode") or "grid3", params.get("res") or 1080))
                imgs = [comfy_upload(base, str(p)) for p in srcs]
                name = mat.out_name(kind, rows[0].get("file_path"), params,
                                    "|".join(sorted(r.get("file_path") or "" for r in rows)))
                wf = mat.build_wf(kind, imgs, params, cfg=cfg)
                (outdir / name).write_bytes(_run_wf_one(base, wf, logger=lambda m: _log(jid, m)))
                url = "%s/materials/%s/%s" % (mat.URL_PREFIX, aid, name)
                results.append((rows[0], url))
                done = 1
                _set(jid, done=done, images=[u for _, u in results])
                _log(jid, "拼版完成 → " + name)
            else:
                for i, (r, p) in enumerate(zip(rows, srcs)):
                    if jobstore.canceled(jid):
                        raise RuntimeError("已取消")
                    _log(jid, "%s %d/%d：%s" % (title, i + 1, len(rows), r.get("name") or ""))
                    main = comfy_upload(base, str(p))
                    # 图生图：本轮这一张当主体，其余张（最多 2 张）当参考 —— 参考图排除主体自己
                    imgs = [main]
                    if kind == "edit":
                        refs = [(r2, p2) for j, (r2, p2) in enumerate(zip(rows, srcs)) if j != i][:2]   # 总输入上限 3 张（1 主体 + 2 参考）
                        for r2, p2 in refs:
                            imgs.append(comfy_upload(base, str(p2)))
                    name = mat.out_name(kind, r.get("file_path"), params)
                    wf = mat.build_wf(kind, imgs, params, cfg=cfg,
                                      prompt=mat.make_prompt(kind, params),
                                      seed=random.randint(1, 2 ** 31 - 1))
                    (outdir / name).write_bytes(_run_wf_one(base, wf, logger=lambda m: _log(jid, m)))
                    url = "%s/materials/%s/%s" % (mat.URL_PREFIX, aid, name)
                    results.append((r, url))
                    done += 1
                    _set(jid, done=done, images=[u for _, u in results])
                    _log(jid, "%s %d 完成 → %s" % (title, i + 1, name))

            # 入库：同一素材 + 同一参数 = 同名覆盖，不产生重复卡
            for r, url in results:
                n = (r.get("name") or "").strip() or url.rsplit("/", 1)[-1]
                mat.upsert(url, "image", name=("%s · %s" % (n[:40], title)),
                           category=r.get("category") or "", source=kind,
                           article_id=r.get("article_id"), owner_id=job.get("owner_id"),
                           owner=job.get("owner") or "", tags=title)
            _set(jid, images=[u for _, u in results], done=len(results))
            _log(jid, "%s全部完成：%d 张" % (title, len(results)))
        except Exception as e:
            _log(jid, "❌ %s失败：%s" % (title, str(e)[:220]))
            raise RuntimeError(str(e)[:300])
        finally:
            _shutdown_if_idle(jid, inst, lambda m: _log(jid, m))


def run_txt2img_job(job):
    """文生图：一句提词 → 1 张
    出图链路复用「文章配图」的 comfy_generate（同实例 / 同底模 / 同 LoRA / 同采样参数 / 同负面词）"""
    import hashlib
    import materials as mat
    jid = job["id"]
    opts = job.get("payload") or {}
    text = (opts.get("prompt") or "").strip()
    style = (opts.get("style") or "").strip() or get_default_style()
    aspect = opts.get("aspect") or "3:4"
    w, h = ASPECT_SIZE.get(aspect, ASPECT_SIZE["3:4"])
    if not text:
        raise RuntimeError("提词不能为空")
    cfg = get_comfy_config()
    if not instance_uuids() or not cfg.get("comfy_api_token"):
        raise RuntimeError("未配置实例池 / Token，请去「设置」页填写")
    short = text if len(text) <= 24 else text[:24] + "…"
    _set(jid, total=1, done=0, style=style)
    _log(jid, "文生图：%s（风格 %s · %s）" % (short, style_label_map().get(style, style) or "-", aspect))
    with instance_ctx(jid, lambda m: _log(jid, m)) as inst:   # 多实例：挑一台空闲可用的
        try:
            _set(jid, stage="ready", host=inst)
            base = base_for(inst, logger=lambda m: _log(jid, m))
            if not base:
                raise RuntimeError("拿不到 ComfyUI 地址")
            _log(jid, "ComfyUI 地址：" + base)
            if not comfy_ready(base, timeout=300, logger=lambda m: _log(jid, m)):
                raise RuntimeError("ComfyUI 300s 内未就绪")
            _set(jid, stage="generating")
            p = _with_style(_clean_aspect_words(text), style)      # 补画风英文词 + 去掉提词里的比例字样
            _log(jid, "画面生成中（%s → %dx%d）…" % (aspect, w, h))
            fn, sub, meta = comfy_generate(base, p, w, h, "t2i_%s" % jid[:6], cfg)
            data = comfy_download(base, fn, sub)
            # 文件名 = 提词+风格+比例的哈希 + 任务号 → 同一提词重跑不会覆盖旧图
            hh = hashlib.md5(("%s|%s|%s" % (text, style, aspect)).encode("utf-8")).hexdigest()[:6]
            name = "t2i_%s_%s.png" % (hh, jid[:4])
            (mat.out_dir(0) / name).write_bytes(data)
            url = "%s/materials/0/%s" % (mat.URL_PREFIX, name)
            mat.upsert(url, "image", name=short, source="txt2img", article_id=None,
                       owner_id=job.get("owner_id"), owner=job.get("owner") or "", tags="文生图")
            _set(jid, done=1, images=[url])
            _log_gen_meta(lambda m: _log(jid, m), 0, name, "t2i", meta, style)
            _log(jid, "文生图完成 → " + name)
        except Exception as e:
            _log(jid, "❌ 文生图失败：%s" % str(e)[:220])
            raise RuntimeError(str(e)[:300])
        finally:
            _shutdown_if_idle(jid, inst, lambda m: _log(jid, m))


def register_jobs():
    """注册任务执行体 + 启动调度器（app.py 启动时调用）"""
    jobstore.DISPATCHER.register("images", run_images_job, domain="gpu")   # 显存域：一块 GPU 串行
    jobstore.DISPATCHER.register("plan", run_plan_job, domain="llm")       # LLM 域：受 llm_parallel 限制
    # 素材加工：都吃显存 → gpu 域（与出图同域，一块卡串行）
    for _k in ("cutout", "edit", "stitch"):
        jobstore.DISPATCHER.register(_k, run_material_job, domain="gpu")
    jobstore.DISPATCHER.register("txt2img", run_txt2img_job, domain="gpu")   # 文生图（同 GPU 域串行）
    # 将来加场景只需两行：写一个 runner + 注册域（分镜/素材 → llm；出视频/配音 → gpu）
    jobstore.DISPATCHER.start()
