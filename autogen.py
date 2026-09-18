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
import random
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import requests

from illustrate import SIZES, STYLES, make_qrcode, compose_card_over_bg

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
    "comfy_neg": "",
}

# aspect → (宽, 高)，与 illustrate.py 的 SIZES 对齐
ASPECT_SIZE = {
    "3:4":    (1080, 1440),
    "1:1":    (1080, 1080),
    "9:16":   (1080, 1920),
    "2.35:1": (1080, 460),
}

NEG = ("模糊, 低质量, 文字错误, 错别字, 多余的文字, 水印, 重复文字, 变形, 杂乱, "
       "人物正脸, 引号, 双引号, 书名号, 立体书本, 相框, 边框, "
       "裸露, 裸体, 绳索, 束缚, 捆绑, 蒙眼, 武器, 刀, 剑, 血腥, 恐怖, "
       # 英文安全词：正向提示词是英文，中文负面词跨语言压制弱，双语更稳
       "nude, nudity, naked, rope, bonding, bondage, tied up, blindfold, "
       "weapon, knife, sword, blood, gore, horror, mutilation")

# 画面安全底线：无论模板怎么改，都由 gen_plan 固定前置到提示词里（不可关闭）
SAFETY_RULE = (
    "【硬性安全要求 · 最高优先级 · 不得省略】本次所有配图："
    "不得出现裸露或性暗示；不得出现人物被束缚、捆绑、蒙眼、镣铐控制等画面；"
    "不得出现武器、刀剑、血腥、伤害、恐怖自伤；不得出现真实人物正脸（背影/侧影/剪影可以）；"
    "不要使用绳索、链条、镣铐等束缚意象。若文案本身涉及此类内容，请改用隐喻表达（如光影、门、路、水、天空）。\n\n"
)

# 采样参数（build_workflow 出图与「参数透明化」日志共用，避免两处漂移）
SAMPLER_CFG = 2.5
SAMPLER_NAME = "euler"
SCHEDULER = "simple"
DENOISE = 1.0

# 配色的英文色卡：出图时追加到正向提示词，让 AI 背景与卡片配色成套
STYLE_PROMPT = {
    "purple": "deep indigo and violet color palette, muted gold accents",
    "dark": "near-black charcoal color palette, low-key quiet lighting",
    "gold": "warm cream and soft beige color palette, gentle golden light",
    "maya": "deep teal and jade green color palette, turquoise tones",
}

# 金句没写 bg 时的兜底背景词（不含颜色，颜色由 STYLE_PROMPT 按当前配色补）
FALLBACK_BG = ("Mystical serene minimal full-bleed background, soft light, "
               "no text, no letters, no people")


def _with_style(prompt, style):
    """给正向提示词补上配色色卡；没有对应配色就原样返回"""
    frag = STYLE_PROMPT.get(style or "")
    p2 = (prompt or "").strip()
    if not frag:
        return p2
    return (p2.rstrip(" ,") + ", " + frag) if p2 else frag


# 配图 Agent 的 System Prompt：出厂默认在下面，实际使用 prompts/image_agent.md（设置页可编辑）
PROMPT_DIR = BASE_DIR / "prompts"
PLAN_PROMPT_FILE = PROMPT_DIR / "image_agent.md"
PLAN_PROMPT_DEFAULT = """# 角色

你是「自媒体文案配图 Agent」——资深自媒体视觉策划 + AI 绘画提示词工程师。任务：读用户给的文案，判断平台、文案类型、受众、情绪、配图目标，直接输出一份可执行的配图方案与生图提示词。

# 运行口径（硬约束 · 必须遵守）

出图后端是 AutoDL ComfyUI（Qwen-Image），据此：

- **提示词只用英文**：`bg` / `prompt` 必须是英文提示词；中文只写在 `text`（图上文字）、`cn`（给人看的中文画面描述）、`scene`（场景的中文画面描述）里
- **画幅只能 4 选 1**：`3:4`（小红书/封面）· `1:1`（朋友圈/知乎/微博）· `9:16`（抖音/快手/视频号/直播）· `2.35:1`（公众号头图/头条/B站）
- **配色只能 4 选 1**（写进 `style`）：`purple` 深紫·灵性塔罗 · `dark` 玄黑·心理哲思 · `gold` 米金·疗愈温柔 · `maya` 青绿·玛雅图腾
- **只有两种图**：
  - `quote` 金句卡 / 大字卡：AI 出满版背景，图上中文由程序精确叠加。凡是要在图上出现的中文（金句、标题、要点），全部放这里、放 `text` —— **绝不让 AI 去画文字**
  - `scene` 场景图：纯画面，画面里不能有任何文字（提示词里带上 `no text, no letters, no watermark`）
- **一张卡最多 3 行、每行 ≤ 14 字**；要点用「｜」分隔
- **数量由你定**：按平台与文案类型给合理张数，可以是 0 张，不要凑数
- 画面安全底线由系统自动附加，不必在此重复
- 同一篇文案的所有图保持同一视觉主线和色调（配色色卡由系统按 `style` 自动追加，不必重复写颜色）

# 工作流程

1. 读文案 → 平台、类型、主题、核心观点/卖点、受众、情绪、关键词、行动号召
2. 判断配图目标：点击率 / 信息传达 / 情绪共鸣 / 转化 / 品牌记忆
3. 定视觉主线：主体、场景、构图、镜头、光线、色彩、风格、材质、情绪
4. 出清单：几张金句卡 + 几张场景图，各用什么画幅
5. 逐条给：中文画面描述 + 英文提示词
6. 合规自检：广告法（不用「最 / 第一 / 100% / 包治」等绝对化）、版权、隐私、敏感内容、平台规范
7. 信息不足就**自行合理假设**并写进 `note.assumptions`，不要反问用户

# 平台适配（画幅 / 风格 / 张数）

| 平台 | 画幅 | 风格基调 | 建议张数 |
|---|---|---|---|
| 小红书 | 3:4 为主，1:1 备用 | 生活感、奶油风、杂志风、高饱和 | 6-9 |
| 公众号 | 2.35:1 封面 + 1:1 内页 | 简洁、品牌感 | 2-4 |
| 抖音/快手/视频号 | 9:16 | 强冲击、大字、动态感 | 1-3 |
| B站 | 2.35:1 | 科技、极简、梗图 | 1-3 |
| 知乎 | 1:1 | 理性、低饱和、数据感 | 1-3 |
| 微博 | 1:1 或 9:16 | 热点海报、话题感 | 1-2 |
| 头条/百家号 | 2.35:1 | 新闻感、真实感 | 1-3 |
| LinkedIn | 1:1 | 商务、专业、干净 | 1-3 |

# 文案类型 → 视觉策略（含配比）

- 种草：真实生活、暖光、特写、使用场景 → 场景图为主 + 1 张金句卡
- 测评：对比、干净背景、细节微距 → 场景图 + 1 张要点卡
- 教程 / 干货：步骤、编号、网格、极简 → 场景图 1-2 + 2-4 张要点卡（`text` 写「标题｜要点1｜要点2」）
- 知识科普：干净、图形化、低饱和 → 场景图 + 要点卡
- 观点：杂志排版、强标题、对比色 → 1 张强标题金句卡 + 场景图 1-2
- 故事 / 情感 / 灵性 / 心理：电影感、叙事、留白、情绪光 → 金句卡为主 4-8 张 + 场景图 1-2
- 职场：办公场景、商务、低饱和 → 场景图 1-3
- 产品推广 / 品牌宣传：商业摄影、棚拍、质感、品牌色 → 场景图为主 + 1 张要点卡
- 活动 / 直播预告：海报、倒计时、福利、大字 → 1 张金句卡（大字）+ 场景图 1-2
- 招聘 / 节日 / 新闻资讯：真实感、主题色 → 场景图 1-3（节日可加 1 张金句卡）

# 生图提示词公式

英文：主体 + 动作/状态 + 场景 + 构图 + 镜头 + 光线 + 色彩 + 风格 + 材质 + 情绪 + 画质 + `no text, no letters, no watermark`（画幅由系统按 `aspect` 设定，不用写 `--ar` 之类的工具参数）

中文：一句话说清画面（10-40 字），要具体、可画、能一眼判断画面对不对

# 输出格式（唯一格式）

只输出**一个严格 JSON 对象**：不要 Markdown、不要解释、不要代码块围栏。

{
  "style": "dark",
  "quotes": [
    {"text": "图上中文，10-28 字，可含一个「｜」换行", "cn": "中文画面描述（背景画什么）", "bg": "english background prompt"}
  ],
  "scenes": [
    {"scene": "中文画面描述（20 字内）", "prompt": "english image prompt", "aspect": "3:4"}
  ],
  "note": {
    "diagnosis": "平台 / 类型 / 受众 / 情绪 / 配图目标",
    "strategy": "视觉主线 / 风格 / 色彩 / 构图 / 文字策略 / 比例 / 数量",
    "compliance": "合规风险提示（广告法 / 版权 / 隐私 / 平台规范）",
    "platform_variants": "同篇发多平台时的比例与改版建议",
    "assumptions": "信息不足时你做的假设"
  },
  "reason": "一句话说明为什么这么配图"
}

字段规则：

- `style`：只能 4 选 1
- `text`：图上中文，10-28 字，不要 emoji、不要话题标签
- `bg` / `prompt`：英文，画面中不得出现任何文字
- `aspect`：只能 3:4 / 1:1 / 9:16 / 2.35:1
- `note`：全部中文、简洁；`note` 不参与出图，只给人看

# 硬性约束

- 不生成侵权名人、血腥暴力、色情、违法、虚假医疗/金融承诺、绝对化广告词
- 不编造事实（不虚构数据、案例、资质、用户评价）
- 品牌色、Logo、真人肖像未经用户确认不要画
- 图中文字要短、可读、不堆砌
"""

# 用户消息模板：本次任务参数（System Prompt 里不出现占位符）
PLAN_USER_TEMPLATE = (
    "【本次任务】\n"
    "平台：{platform}\n"
    "文案类型：{ctype}\n"
    "金句卡（quotes）参考 {card_want} 张（可 ±1~2，也可以是 0 张）\n"
    "场景图（scenes）参考 {scene_count} 张（可 ±1~2）\n"
    "配色 4 选 1、画幅按上面的平台适配规则自己定。\n"
    "严格按「输出格式」只回一个 JSON 对象。\n\n"
    "文案标题：{title}\n"
    "文案正文：\n{content}"
)

# 平台标识 → 中文名（写进用户消息，让 LLM 能对上平台适配表）
PLATFORM_LABEL = {
    "xiaohongshu": "小红书", "moments": "朋友圈", "wechat": "公众号",
    "video_account": "视频号", "douyin": "抖音", "kuaishou": "快手",
    "bilibili": "B站", "zhihu": "知乎", "weibo": "微博", "toutiao": "头条",
    "baijiahao": "百家号", "linkedin": "LinkedIn", "youtube": "YouTube",
    "twitter": "Twitter/X", "instagram": "Instagram", "podcast": "播客",
}


def load_plan_prompt():
    """读 System Prompt 文件；缺失/读失败 → 用出厂默认（并自动补回文件）"""
    try:
        t = PLAN_PROMPT_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    except Exception:
        pass
    save_plan_prompt(PLAN_PROMPT_DEFAULT)
    return PLAN_PROMPT_DEFAULT.strip()


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


save_plan_prompt(load_plan_prompt())        # 确保 prompts/image_agent.md 存在（缺失即补回）

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


def build_workflow(prompt, w, h, seed, cfg, prefix, neg=None):
    steps = int(cfg.get("comfy_steps") or 20)
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": cfg.get("comfy_unet"), "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": cfg.get("comfy_clip"), "type": "qwen_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": cfg.get("comfy_vae")}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode",
              "inputs": {"text": (neg or cfg.get("comfy_neg") or "").strip() or NEG,
                         "clip": ["2", 0]}},
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


def comfy_generate(base, prompt, w, h, prefix, cfg, timeout=600, neg=None, seed=None):
    """提交一张图并等待完成，返回 (filename, subfolder, meta)

    meta = 这张图最终提交给 ComfyUI 的提示词与参数（供任务日志/落库排查）"""
    if seed is None:
        seed = random.randint(1, 2 ** 31 - 1)
    neg_text = (neg or cfg.get("comfy_neg") or "").strip() or NEG
    wf = build_workflow(prompt, w, h, seed, cfg, prefix, neg=neg_text)
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
    log("\u24d8 参数：seed=%s · steps=%s · cfg=%s · %s/%s · %dx%d · 配色 %s"
        % (meta.get("seed"), meta.get("steps"), meta.get("cfg"), meta.get("sampler"),
           meta.get("scheduler"), meta.get("width"), meta.get("height"), style or "-"))
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
STYLE_ALIAS = {
    '深紫': 'purple', '紫': 'purple', '灵性': 'purple', '塔罗': 'purple',
    '玄黑': 'dark', '黑': 'dark', '心理': 'dark', '哲思': 'dark',
    '米金': 'gold', '金': 'gold', '疗愈': 'gold', '温柔': 'gold',
    '玛雅': 'maya', '青绿': 'maya', '图腾': 'maya',
}
STYLE_HINT = {
    'tarot': 'purple', '塔罗': 'purple', '灵性': 'purple', '能量': 'purple',
    '心理': 'dark', '哲思': 'dark', '情绪': 'dark',
    '玛雅': 'maya', '图腾': 'maya',
    '疗愈': 'gold', '温柔': 'gold', '阅读': 'gold',
}


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
ASPECTS = ("3:4", "1:1", "9:16", "2.35:1")


def read_plan(article_id):
    """读配图方案；兼容旧格式（纯数组 = 只有场景组）"""
    conn = _content_db()
    row = conn.execute("SELECT image_script FROM articles WHERE id=?", (article_id,)).fetchone()
    conn.close()
    raw = (row["image_script"] if row else None) or ""
    try:
        data = json.loads(raw or "{}")
    except Exception:
        data = {}
    if isinstance(data, list):                     # 旧格式：场景数组
        data = {"scenes": data}
    if not isinstance(data, dict):
        data = {}
    quotes = []
    for q in (data.get("quotes") or []):
        if isinstance(q, dict) and (q.get("text") or "").strip():
            quotes.append({"text": str(q.get("text")).strip(),
                           "cn": str(q.get("cn") or "").strip(),
                           "bg": str(q.get("bg") or "").strip(),
                           "on": bool(q.get("on", True))})
        elif isinstance(q, str) and q.strip():
            quotes.append({"text": q.strip(), "cn": "", "bg": "", "on": True})
    scenes = []
    for s in (data.get("scenes") or []):
        if isinstance(s, dict) and (s.get("prompt") or "").strip():
            asp = str(s.get("aspect") or "3:4").strip()
            scenes.append({"scene": str(s.get("scene") or "").strip(),
                           "prompt": str(s.get("prompt")).strip(),
                           "aspect": asp if asp in ASPECTS else "3:4",
                           "on": bool(s.get("on", True))})
    style = str(data.get("style") or "").strip().lower()
    style = STYLE_ALIAS.get(style, style)
    note = data.get("note")
    return {"style": style if style in STYLES else "",
            "quotes": quotes, "scenes": scenes,
            "note": note if isinstance(note, dict) else {}}


def save_plan(article_id, plan):
    conn = _content_db()
    conn.execute("UPDATE articles SET image_script=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                 (json.dumps(plan, ensure_ascii=False), article_id))
    conn.commit()
    conn.close()
    return plan


def gen_plan(art, llm_cfg, card_want, scene_count):
    """一次 LLM 调用产出整份方案；返回 (plan|None, reason)"""
    import re as _re
    llm_cfg = llm_cfg or {}
    key = llm_cfg.get("llm_api_key", "")
    url = llm_cfg.get("llm_base_url") or ""
    model = llm_cfg.get("llm_model") or "deepseek-chat"
    content = (art.get("content_md") or "")[:2000]
    title = art.get("title") or ""
    if not (key and url and content):
        return None, "未配置 LLM 或文案无正文"

    # 系统提示词 = 安全底线（代码固定，改不掉）+ prompts/image_agent.md 文件内容
    sys_prompt = SAFETY_RULE + load_plan_prompt()
    plat = (art.get("platform") or "").strip()
    user_msg = (PLAN_USER_TEMPLATE
                .replace("{platform}", PLATFORM_LABEL.get(plat, plat or "未指定"))
                .replace("{ctype}", (art.get("content_type") or "文章").strip())
                .replace("{card_want}", str(card_want))
                .replace("{scene_count}", str(scene_count))
                .replace("{title}", title)
                .replace("{content}", content))
    try:
        r = requests.post(url, headers={"Authorization": "Bearer " + key,
                                        "Content-Type": "application/json"},
                          json={"model": model,
                                "messages": [{"role": "system", "content": sys_prompt},
                                             {"role": "user", "content": user_msg}]},
                          timeout=180)
        raw = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return None, "LLM 调用失败（%s）" % str(e)[:60]

    m = _re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None, "LLM 未返回 JSON"
    try:
        j = json.loads(m.group())
    except Exception:
        return None, "LLM 返回的 JSON 解析失败"

    style = str(j.get("style") or "").strip().lower()
    style = STYLE_ALIAS.get(style, style)
    quotes = []
    for q in (j.get("quotes") or [])[:8]:          # 数量由 LLM 定，这里只兜底防爆
        if isinstance(q, dict) and str(q.get("text") or "").strip():
            quotes.append({"text": str(q["text"]).strip(),
                           "cn": str(q.get("cn") or "").strip(),
                           "bg": str(q.get("bg") or "").strip()})
        elif isinstance(q, str) and q.strip():
            quotes.append({"text": q.strip(), "cn": "", "bg": ""})
    scenes = []
    for s in (j.get("scenes") or [])[:6]:          # 数量由 LLM 定，这里只兜底防爆
        if not isinstance(s, dict) or not str(s.get("prompt") or "").strip():
            continue
        asp = str(s.get("aspect") or "3:4").strip()
        scenes.append({"scene": str(s.get("scene") or "").strip(),
                       "prompt": str(s["prompt"]).strip(),
                       "aspect": asp if asp in ASPECTS else "3:4"})
    if not quotes and not scenes:
        return None, "LLM 返回的方案是空的"
    note = j.get("note")
    return ({"style": style if style in STYLES else "purple",
             "quotes": quotes, "scenes": scenes,
             "note": note if isinstance(note, dict) else {}}, j.get("reason", ""))


# ============================================================
# 单条重写提示词（只重写一条，不整组重跑 · 纯 LLM 不开机）
# ============================================================
REWRITE_PROMPT = (
    "你是自媒体配图策划专家。下面这条【{kind_label}】的配图描述不理想，请只重写这一条，"
    "其它条目一律不要改动。\n\n"
    "当前条目：\n{current}\n\n"
    "要求：{requirement}\n"
    "{palette}"
    "{hint}"
    "文案标题：{title}\n"
    "文案正文（节选）：\n{content}\n\n"
    "严格输出 JSON（不要输出多余文字）：{shape}"
)


def rewrite_plan_item(article_id, kind, index, hint, llm_cfg):
    """只让 LLM 重写某一条的 bg（kind='quote'）/ prompt（kind='scene'）。

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
    items = plan["quotes"] if kind == "quote" else plan["scenes"]
    if not (0 <= index < len(items)):
        return None, "条目不存在"
    item = items[index]
    art = _read_article_full(article_id) or {}
    title = art.get("title") or ""
    content = (art.get("content_md") or "")[:2000]
    if kind == "quote":
        others = "；".join([(q.get("text") or "") for j, q in enumerate(items) if j != index][:8])
        cur = "金句：%s\n当前背景提示词（英文）：%s" % (
            item.get("text") or "", item.get("bg") or "（空）")
        requirement = ("换一个完全不同的氛围/意象背景（不要沿用当前的意象）。"
                       "bg 为英文绘图提示词：只画氛围与意象背景，画面中不要出现任何文字，"
                       "风格统一 mystical / serene / minimal，包含主体、光线、色调、构图，"
                       "不要真实人物正脸。同时给出新的中文画面描述 cn（10-40 字，给人看）。")
        shape = '{"cn":"中文画面描述","bg":"english background prompt"}'
    else:
        others = "；".join([(x.get("scene") or "") for j, x in enumerate(items) if j != index][:8])
        cur = "画面描述：%s\n当前绘图提示词（英文）：%s\n当前比例：%s" % (
            item.get("scene") or "", item.get("prompt") or "（空）", item.get("aspect") or "3:4")
        requirement = ("换一个完全不同的画面构思（不要沿用当前的构思）。"
                       "scene 为中文画面描述（20 字内）；prompt 为英文绘图提示词，"
                       "含主体/风格/光线/色调/构图，画面中不要出现文字；"
                       "aspect 从 3:4 / 1:1 / 9:16 / 2.35:1 中选一个。")
        shape = '{"scene":"画面描述","prompt":"english image prompt","aspect":"3:4"}'
    h = (hint or "").strip()
    _frag = STYLE_PROMPT.get(plan.get("style") or "")
    palette = ("配色：本次整套配图的配色是 %s，画面色调请围绕「%s」。\n"
               % (plan.get("style"), _frag)) if _frag else ""
    prompt = (SAFETY_RULE + REWRITE_PROMPT
              .replace("{kind_label}", "金句卡" if kind == "quote" else "场景图")
              .replace("{current}", cur)
              .replace("{requirement}", requirement)
              .replace("{palette}", palette)
              .replace("{hint}", ("额外要求（优先满足）：%s\n" % h) if h else "")
              .replace("{title}", title)
              .replace("{content}", content)
              .replace("{shape}", shape))
    if others:
        prompt += "\n其它条目（不要与它们重复）：" + others
    try:
        r = requests.post(url, headers={"Authorization": "Bearer " + key,
                                        "Content-Type": "application/json"},
                          json={"model": model,
                                "messages": [{"role": "user", "content": prompt}]},
                          timeout=180)
        raw = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return None, "LLM 调用失败（%s）" % str(e)[:60]
    m = _re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None, "LLM 未返回 JSON"
    try:
        j = json.loads(m.group())
    except Exception:
        return None, "LLM 返回的 JSON 解析失败"
    if kind == "quote":
        bg = str(j.get("bg") or "").strip()
        if not bg:
            return None, "LLM 没给出新的背景提示词"
        new_item = {"text": item.get("text") or "",
                    "cn": str(j.get("cn") or item.get("cn") or "").strip(),
                    "bg": bg, "on": item.get("on", True)}
        plan["quotes"][index] = new_item
    else:
        pr = str(j.get("prompt") or "").strip()
        if not pr:
            return None, "LLM 没给出新的绘图提示词"
        asp = str(j.get("aspect") or "").strip()
        new_item = {"scene": str(j.get("scene") or item.get("scene") or "").strip(),
                    "prompt": pr,
                    "aspect": asp if asp in ASPECTS else (item.get("aspect") or "3:4"),
                    "on": item.get("on", True)}
        plan["scenes"][index] = new_item
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
                newp, reason = gen_plan(art, llm_cfg, card_want, scene_count)
                if newp:
                    plan = newp
                    save_plan(article_id, plan)
                    log("方案已生成：金句 %d 句 · 场景 %d 个 · 配色 %s"
                        % (len(plan["quotes"]), len(plan["scenes"]), plan["style"]))
                else:
                    log("方案生成失败：%s" % reason)
                    if want_cards and not plan["quotes"]:
                        fb = _extract_quotes_fallback(art.get("content_md"),
                                                      art.get("title"), card_want)
                        plan["quotes"] = [{"text": q, "bg": ""} for q in fb]
                        if not plan["style"]:
                            plan["style"] = STYLE_HINT.get(art.get("platform") or "", "purple")
                        save_plan(article_id, plan)
                        log("改用正文抽句作为金句（%d 句）" % len(plan["quotes"]))

            if opts.get("plan_only"):
                _set(job_id, status="done", finished_at=time.time(), total=0, done=0,
                     quotes=plan["quotes"], scenes=plan["scenes"], style=plan["style"],
                     want_cards=False, want_scenes=False)
                log("方案已更新（仅分析，未出图、未开机）")
                return

            quotes = [q for q in plan["quotes"] if q.get("on", True)] if want_cards else []
            scenes = [s for s in plan["scenes"] if s.get("on", True)] if want_scenes else []
            if want_cards and not quotes:
                raise RuntimeError("方案里没有勾选的金句 —— 请先在「① 生成方案」里勾选要出的条目")
            if want_scenes and not scenes:
                raise RuntimeError("方案里没有勾选的场景 —— 请先在「① 生成方案」里勾选要出的条目")
            style = plan.get("style") or "purple"
            if style not in STYLES:
                style = "purple"
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
                    card = compose_card_over_bg(bg, q["text"], style=style,
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
