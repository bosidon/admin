#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业务线（内容线）：选题来源 + 引流落点 + 文末引导语

选题**全部读各子站现成资产**，不另建内容库：
  · 灵性书籍 → 阅读站 /var/www/lingxiu/data/xianbao.db（books 书目 / chapters 章节 / chapter_summaries 摘要）
  · 玛雅天赋 → /var/www/kin-guide/data/{challenges,seals_refined,daily_energy,kin-<n>}.json
  · 塔罗     → /var/www/tarot/backend/cards.json + /var/www/tarot/shared/spreads.json
  · 心理咨询 → /var/www/psych-test/data/psychological_assessment.db（assessments/questions）
"""
import ast
import datetime
import json
import re
import sqlite3
import time
from pathlib import Path

KIN_DIR = Path("/var/www/kin-guide/data")
TAROT_CARDS = Path("/var/www/tarot/backend/cards.json")
TAROT_SPREADS = Path("/var/www/tarot/shared/spreads.json")
PSYCH_DB = Path("/var/www/psych-test/data/psychological_assessment.db")
LINGXIU_DB = Path("/var/www/lingxiu/data/xianbao.db")     # 阅读站（灵修）内容库
_SENSITIVE_FILE = Path(__file__).resolve().parent / "configs" / "sensitive_words.json"
_SENSITIVE_DEFAULT = {
    "体系术语": ["密度", "极性", "收割", "八度", "心身灵", "社会记忆复合体", "理则"],
    "灵性体系": ["通灵", "转世", "前世", "投胎", "灵界", "高维", "扬升", "星际", "外星", "金字塔",
                 "预言", "玄学", "超自然", "算命", "改运", "开光", "加持", "附体", "业力"],
    "医疗健康": ["治疗", "治愈", "疗效", "药用", "诊断", "抑郁症", "焦虑症", "心理疾病", "包治", "根治"],
    "绝对化用语": ["最灵", "最准", "一定", "保证", "必然", "必定", "绝对", "100%"],
    "平台违禁": ["加微信", "私信我", "扫码", "免费领取", "独家首发"],
}
KIN_CYCLE = 260

# 业务线定义：home = 引流落点（子网站首页），guide = 文末引导语
LINES = [
    {"id": "lingxiu", "name": "灵性书籍", "icon": "📚",
     "home": "https://xianbao.love",
     "guide": "🌙 想了解更多心灵成长内容？",
     "kinds": [{"id": "topic", "name": "话题选题"}]},
    {"id": "maya", "name": "玛雅天赋", "icon": "🔮",
     "home": "https://maya.xianbao.love",
     "guide": "✨ 想知道你的星系印记？免费测一下 →",
     "kinds": [{"id": "challenge", "name": "挑战功课"}, {"id": "seal", "name": "太阳图腾"},
               {"id": "tone", "name": "银河音阶"}, {"id": "wave", "name": "波符"},
               {"id": "daily", "name": "每日能量"}, {"id": "kin", "name": "单印记"}]},
    {"id": "tarot", "name": "塔罗", "icon": "🃏",
     "home": "https://tarot.xianbao.love",
     "guide": "🃏 想抽一张牌看看当下？来抽牌 →",
     "kinds": [{"id": "card", "name": "牌义解读"}, {"id": "spread", "name": "牌阵解读"}]},
    {"id": "psych", "name": "心理咨询", "icon": "💗",
     "home": "https://ceping.xianbao.love",
     "guide": "💗 想知道自己的状态？做一次专业测评 →",
     "kinds": [{"id": "scale", "name": "心理量表"}]},
]
LINE_MAP = {l["id"]: l for l in LINES}

_CACHE = {}


def _cached(key, fn, ttl=600):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        data = fn()
    except Exception:
        data = []
    _CACHE[key] = (now, data)
    return data


def _pv(text, n=70):
    t = " ".join(str(text or "").split())
    return t[:n] + ("…" if len(t) > n else "")


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def line_of(line_id):
    return LINE_MAP.get(line_id or "")


def kinds(line_id):
    l = line_of(line_id)
    return (l or {}).get("kinds") or []


# ---------------------------------------------------------------- 玛雅
def kin_of_date(y=None, m=None, d=None):
    """日期 → 卓尔金历 kin（算法照抄站上 maya_calculator.js 的 kinNum，勿自创）"""
    today = datetime.date.today()
    y, m, d = y or today.year, m or today.month, d or today.day
    off = (datetime.date(y, m, d) - datetime.date(2001, 1, 1)).days
    return ((365 * (y - 1900) + 52 + off) % KIN_CYCLE) + 1


def _lingxiu_conn():
    """阅读站内容库（只读连接，绝不写）"""
    c = sqlite3.connect("file:%s?mode=ro" % LINGXIU_DB, uri=True)
    c.row_factory = sqlite3.Row
    return c


def _lingxiu_books():
    """已发布书目（57 本）→ 书目下拉条目"""
    if not LINGXIU_DB.exists():
        return []
    try:
        c = _lingxiu_conn()
        cats = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM categories")}
        rows = c.execute("SELECT id, title, subtitle, author, category_id, description FROM books "
                         "WHERE status='published' ORDER BY sort_order, id").fetchall()
        c.close()
    except Exception:
        return []
    out = []
    for b_ in rows:
        name = "《%s》" % b_["title"]
        extra = " · ".join([x for x in (b_["author"], cats.get(b_["category_id"])) if x])
        out.append({"id": name, "name": (name + " · " + extra) if extra else name,
                    "preview": _pv(b_["description"], 80)})
    return out


def _sensitive_words():
    """平台敏感词分组（configs/sensitive_words.json，缺失则用内置兜底）"""
    try:
        d = json.loads(_SENSITIVE_FILE.read_text(encoding="utf-8"))
        return {k: v for k, v in d.items() if not k.startswith("_") and isinstance(v, list)}
    except Exception:
        return _SENSITIVE_DEFAULT


_EMO_WORDS = ("自我", "关系", "情绪", "孤独", "恐惧", "选择", "成长", "疗愈", "练习", "当下",
              "困惑", "爱", "痛", "生活", "内耗", "焦虑", "价值", "边界", "原生", "自责")
_SYS_WORDS = ("历史", "星际", "文明", "起源", "演化", "八度", "密度", "联邦", "金字塔", "战争",
              "宇宙", "哲学", "法则", "机制", "维度", "体系", "编年", "理论")


def _lingxiu_topics():
    """全部话题（1,574 条，含分类已删的孤儿）→ 选题条目 + 自媒体打标

    关联口径与阅读站一致：按 t.book_id（而不是绕分类表），这样分类被删的
    36 条话题也能被选中（内容仍在，只是阅读站上点不到）。
    """
    if not LINGXIU_DB.exists():
        return []
    try:
        c = _lingxiu_conn()
        rows = c.execute(
            "SELECT t.title, t.summary, t.overview, t.key_concepts, t.core_content, "
            "t.related_passages, t.practical_application, b.title AS book_title, "
            "c.name AS cat FROM ai_deep_themes t "
            "JOIN books b ON b.id = t.book_id "
            "LEFT JOIN ai_deep_categories c ON c.id = t.category_id "
            "WHERE b.status = 'published' AND t.status = 'completed' "
            "ORDER BY b.sort_order, b.id, c.name, t.title").fetchall()
        c.close()
    except Exception:
        return []
    out = []
    for r in rows:
        blob = " ".join([str(r["title"] or ""), str(r["summary"] or ""), str(r["overview"] or "")])
        mat_len = sum(len(str(r[k] or "")) for k in ("summary", "overview", "key_concepts",
                                                    "core_content", "related_passages", "practical_application"))
        risk = _lingxiu_risk(blob)
        out.append({
            "id": r["title"], "name": r["title"], "preview": _pv(_clean_summary(r["summary"]), 60),
            "book_name": r["book_title"] or "", "category": r["cat"] or "",
            "mat_len": mat_len, "seg": _lingxiu_seg(blob), "risk": risk,
            "plat": "公众号优先" if risk else "全平台",
        })
    return out


def _lingxiu_seg(blob):
    """选题画像：情绪向 / 体系向 / 混合（按关键词粗分，仅供选题参考）"""
    e = sum(1 for k in _EMO_WORDS if k in blob)
    s = sum(1 for k in _SYS_WORDS if k in blob)
    if e and not s:
        return "情绪向"
    if s and not e:
        return "体系向"
    if e and s:
        return "混合"
    return ""


def _lingxiu_risk(blob):
    """命中的平台敏感词（灵性体系 / 医疗健康 / 绝对化用语 三组，最多 4 个）"""
    w = _sensitive_words()
    hit = []
    for g in ("灵性体系", "医疗健康", "绝对化用语"):
        for x in w.get(g, []):
            if x in blob and x not in hit:
                hit.append(x)
    return hit[:4]


def lingxiu_book_id(book):
    """书名 → 阅读站 book_id（只读，用于引流深链）"""
    if not LINGXIU_DB.exists():
        return None
    name = str(book or "").strip().strip("《》").split(" · ")[0].strip()
    if not name:
        return None
    try:
        c = _lingxiu_conn()
        r = c.execute("SELECT id FROM books WHERE title=? AND status='published'", (name,)).fetchone()
        c.close()
        return r["id"] if r else None
    except Exception:
        return None


def _chapter_hint(book_title, topic_title, budget):
    """话题素材偏薄时，从同书章节摘要里挑最相关的补充（关键词重叠打分）"""
    if budget <= 300 or not LINGXIU_DB.exists():
        return ""
    try:
        c = _lingxiu_conn()
        rows = c.execute(
            "SELECT ch.title AS ctitle, s.summary AS csum FROM chapters ch "
            "JOIN books b ON b.id = ch.book_id "
            "JOIN chapter_summaries s ON s.chapter_id = ch.id "
            "WHERE b.title = ? AND length(coalesce(s.summary,'')) > 200 "
            "ORDER BY ch.sort_order, ch.id LIMIT 80", (book_title,)).fetchall()
        c.close()
    except Exception:
        return ""
    if not rows:
        return ""

    def grams(t, n=2):
        t = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", str(t or ""))
        return {t[i:i + n] for i in range(max(len(t) - n + 1, 0))}

    key = grams(topic_title)
    scored = []
    for r in rows:
        s_ = grams(r["ctitle"] + _clean_summary(r["csum"])[:600])
        if not s_:
            continue
        overlap = len(key & s_) / max(len(key), 1)
        scored.append((overlap, r["ctitle"], _clean_summary(r["csum"])))
    scored.sort(key=lambda x: -x[0])
    out, used = [], 0
    for ov, ctitle, csum in scored[:2]:
        if ov <= 0.05:
            break
        seg = "《%s》：%s" % (ctitle, csum[:max(budget - used - 40, 0)])
        if len(seg) < 120:
            break
        out.append("- " + seg)
        used += len(seg)
        if used >= budget:
            break
    if not out:
        return ""
    return "相关章节摘要（补充背景，可与话题内容互相印证）：\n" + "\n".join(out)


def sensitive_words():
    """对外暴露敏感词分组（app.py 用）"""
    return _sensitive_words()


def _clean_summary(t):
    """摘要去掉 markdown 符号与 emoji"""
    t = str(t or "").strip()
    t = re.sub("^#+[ ]*", "", t)
    t = re.sub("[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]", "", t)
    t = re.sub("[*_`>]+", " ", t)
    t = re.sub("-", " ", t)
    return " ".join(t.split())


def _html_text(h):
    """HTML 转纯文本（兜底用）"""
    t = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", str(h or ""), flags=re.I)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"')
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _lingxiu_material(kind, obj, book=None):
    """灵性线素材（唯一事实来源）

    话题粒度：概述 + 摘要 + 关键概念 + **原文引用(related_passages)** + 核心内容 + **实践应用(practical_application)**
    书粒度：书简介 + 该书话题的摘要清单
    总量控制在 ~4,400 字以内（调用方另有 4,500 上限）。
    """
    if not LINGXIU_DB.exists():
        return ""
    obj = str(obj or "").strip()
    book_title = str(book or "").strip().strip("《》").split(" · ")[0].strip()
    try:
        c = _lingxiu_conn()
        if obj:                                     # —— 话题粒度 ——
            row = c.execute(
                "SELECT t.title, t.summary, t.overview, t.key_concepts, t.core_content, "
                "t.related_passages, t.practical_application, t.content_html, "
                "c.name AS cat, b.title AS bt FROM ai_deep_themes t "
                "JOIN books b ON b.id = t.book_id "
                "LEFT JOIN ai_deep_categories c ON c.id = t.category_id "
                "WHERE t.title = ? AND (? = '' OR b.title = ?) LIMIT 1",
                (obj, book_title, book_title)).fetchone()
            if not row:
                c.close()
                return ""
            c.close()
            parts = ["书目：《%s》　分类：%s" % (row["bt"], row["cat"] or "（未归类）"),
                     "话题：%s" % row["title"]]
            if row["overview"]:
                parts.append("概述：\n" + str(row["overview"]).strip())
            if row["summary"]:
                parts.append("摘要：\n" + str(row["summary"]).strip())
            kc = str(row["key_concepts"] or "").strip()
            if kc:
                parts.append("关键概念：\n" + kc.replace("|", "\n- ").replace(",", "、"))
            rp = str(row["related_passages"] or "").strip()
            if rp:
                parts.append("原文引用（可直接引用，注明出处）：\n" + rp[:1300])
            core = str(row["core_content"] or "").strip() or _html_text(row["content_html"])
            if core:
                parts.append("核心内容：\n" + core[:1400])
            pa = str(row["practical_application"] or "").strip()
            if pa:
                parts.append("实践应用（可写成练习/行动建议）：\n" + pa[:1100])
            txt = "\n\n".join(parts)
            if len(txt) < 1500:                      # 素材偏薄 → 用同书章节摘要补厚
                extra = _chapter_hint(row["bt"], row["title"], 4200 - len(txt))
                if extra:
                    txt += "\n\n" + extra
            return txt

        # —— 只选了书目（没选话题）→ 书级素材 ——
        b_ = c.execute("SELECT id, title, subtitle, author, category_id, description FROM books "
                       "WHERE title = ? AND status='published'", (book_title,)).fetchone()
        if not b_:
            c.close()
            return ""
        cat = c.execute("SELECT name FROM categories WHERE id=?", (b_["category_id"],)).fetchone()
        tps = c.execute(
            "SELECT t.title, t.summary FROM ai_deep_themes t "
            "WHERE t.book_id = ? AND t.status='completed' ORDER BY t.category_id, t.title LIMIT 16",
            (b_["id"],)).fetchall()
        c.close()
        parts = ["书目：《%s》%s" % (b_["title"], ("｜" + b_["subtitle"]) if b_["subtitle"] else ""),
                 "作者：%s　分类：%s" % (b_["author"] or "—", cat["name"] if cat else "—"),
                 "简介：\n" + str(b_["description"] or "")]
        if tps:
            parts.append("本书话题（供选择切入角度）：\n" + "\n".join(
                "- %s：%s" % (t["title"], _clean_summary(t["summary"])[:120]) for t in tps))
        return "\n\n".join(parts)
    except Exception:
        return ""



def _maya_items(kind):
    if kind in ("challenge", "seal"):
        f, pk = (("challenges.json", "core_challenge") if kind == "challenge"
                 else ("seals_refined.json", "core_essence"))
        d = _load_json(KIN_DIR / f) or {}
        out = []
        for _, v in sorted(d.items(), key=lambda kv: int(kv[0])):
            label = "%s · %s" % (v.get("name"), v.get("archetype") or "")
            out.append({"id": label, "name": label,
                        "preview": _pv(v.get(pk) or v.get("intro") or v.get("keywords"))})
        return out
    if kind == "tone":
        d = _load_json(KIN_DIR / "tones.json") or {}
        out = []
        for _, v in sorted(d.items(), key=lambda kv: int(kv[0])):
            label = "%s（%s）" % (v.get("name"), v.get("question") or "")
            out.append({"id": label, "name": label,
                        "preview": _pv(v.get("personality") or v.get("traits"))})
        return out
    if kind == "wave":
        d = _load_json(KIN_DIR / "waves.json") or {}
        return [{"id": k, "name": k, "preview": _pv(v.get("summary"))} for k, v in d.items()]
    if kind in ("daily", "kin"):
        de = {}
        try:
            for x in (_load_json(KIN_DIR / "daily_energy.json").get("每日能量") or []):
                de[int(x.get("kin") or 0)] = x
        except Exception:
            de = {}
        items = []
        for n in range(1, KIN_CYCLE + 1):
            e = de.get(n) or {}
            nm = e.get("name") or ""
            label = ("kin %d · %s" % (n, nm)).strip(" ·")
            items.append({"id": label, "name": label, "kin": n,
                          "preview": _pv(e.get("frequency") or e.get("guidance"))})
        if kind == "daily":
            today = kin_of_date()
            for it in items:
                it["today"] = (it["kin"] == today)
            items.sort(key=lambda x: (not x["today"], x["kin"]))   # 今天排最前
        return items
    return []


def _maya_material(kind, obj):
    """喂给 LLM 的原始素材（唯一事实来源）"""
    if kind == "challenge":
        v = (_load_json(KIN_DIR / "challenges.json") or {}).get(_seal_id(obj)) or {}
        return "\n".join(x for x in [
            "图腾：%s（%s）" % (v.get("name"), v.get("archetype") or ""),
            "核心挑战：\n" + str(v.get("core_challenge") or "")[:2600],
            "突破方向：\n" + str(v.get("expansion") or "")[:1200],
            "概述：\n" + str(v.get("intro") or "")[:600]] if x.strip())
    if kind == "seal":
        v = (_load_json(KIN_DIR / "seals_refined.json") or {}).get(_seal_id(obj)) or {}
        return "\n".join(x for x in [
            "图腾：%s（%s）  关键词：%s" % (v.get("name"), v.get("archetype") or "", v.get("keywords") or ""),
            "本质：\n" + str(v.get("core_essence") or "")[:1500],
            "性格：\n" + str(v.get("personality") or "")[:1500],
            "阴影：\n" + str(v.get("shadow") or "")[:1000]] if x.strip())
    if kind == "tone":
        nm = str(obj or "").split("（")[0].split(" ·")[0].strip()
        for _, x in (_load_json(KIN_DIR / "tones.json") or {}).items():
            if (x.get("name") or "").strip() == nm:
                return "\n".join(p for p in [
                    "音阶：%s（第 %s 个音阶）" % (x.get("name"), x.get("id")),
                    "核心问句：" + str(x.get("question") or ""),
                    "特质：" + str(x.get("traits") or ""),
                    "人格：\n" + str(x.get("personality") or "")[:1500],
                    "驱动力：\n" + str(x.get("drive") or "")[:1200],
                    "挑战：\n" + str(x.get("challenge") or "")[:1000]] if p.strip())
        return ""
    if kind == "wave":
        nm = str(obj or "").split("（")[0].strip()
        w = (_load_json(KIN_DIR / "waves.json") or {}).get(nm) or {}
        if not w:
            return ""
        parts = ["波符：%s" % w.get("name"),
                 "概述：\n" + str(w.get("summary") or "")[:1600],
                 "诗：\n" + str(w.get("poem") or "")[:800]]
        days = w.get("days")
        if isinstance(days, str):
            try:
                days = ast.literal_eval(days)
            except Exception:
                days = None
        if isinstance(days, dict):
            rows = []
            for k in sorted(days, key=lambda x: int(x)):
                dd = days.get(k) or {}
                rows.append("第%s天 %s：%s" % (k, dd.get("tone_seal") or "", dd.get("question") or ""))
            if rows:
                parts.append("13 天能量：\n" + "\n".join(rows))
        return "\n\n".join(p for p in parts if p.strip())
    if kind in ("daily", "kin"):
        m = re.search(r"(\d+)", str(obj or ""))          # 标签形如「kin 55 · 电力的蓝鹰」
        n = int(m.group(1)) if m else kin_of_date()
        if not 1 <= n <= KIN_CYCLE:
            n = kin_of_date()
        if kind == "daily":
            for x in (_load_json(KIN_DIR / "daily_energy.json").get("每日能量") or []):
                if int(x.get("kin") or 0) == n:
                    return "\n".join([
                        "今日印记：%s（%s · %s）" % (x.get("name"), x.get("tone"), x.get("seal")),
                        "频率：" + str(x.get("frequency") or ""),
                        "指引：\n" + str(x.get("guidance") or ""),
                        "行动建议：" + str(x.get("action") or ""),
                        "肯定语：" + str(x.get("affirmation") or "")])
            return ""
        d = _load_json(KIN_DIR / ("kin-%d.json" % n)) or {}
        wd = d.get("waveDay") or {}
        parts = ["主印记：%s（kin %s）" % (d.get("name"), d.get("kin")),
                 "波符：%s（第 %s 天）" % (d.get("waveName"), d.get("wavePos")),
                 "宇宙问句：" + str(wd.get("question") or ""),
                 "能量种子：" + str(wd.get("energy_seed") or ""),
                 "核心解读：\n" + str(wd.get("interpretation") or "")[:1800],
                 "行动提示：" + str(wd.get("action_prompt") or "")]
        for key, label in (("support", "支持力"), ("challenge", "挑战力"),
                           ("hidden", "隐藏力"), ("guide", "引导力")):
            fc = (d.get("forces") or {}).get(key) or {}
            if fc.get("name"):
                parts.append("%s：%s（%s）\n%s" % (label, fc.get("name"),
                                                   fc.get("toneName") or "",
                                                   str(fc.get("ai") or "")[:1200]))
        return "\n\n".join(p for p in parts if p.strip())
    return ""


def _seal_id(name):
    """'红龙 · 源动力' / '红龙' → seals/challenges 里的 key（'1'..'20'）"""
    nm = str(name or "").split("·")[0].strip()
    for path in ("seals_refined.json", "challenges.json"):
        d = _load_json(KIN_DIR / path) or {}
        for k, v in d.items():
            if (v.get("name") or "").strip() == nm:
                return k
    return ""


# ---------------------------------------------------------------- 塔罗
def _tarot_items(kind):
    if kind == "card":
        cards = _load_json(TAROT_CARDS) or []
        out = []
        for c in cards:
            label = "%s（%s）" % (c.get("name"), c.get("arcana") or "")
            out.append({"id": label, "name": label,
                        "preview": _pv("关键词：%s｜正位：%s" % (c.get("keywords") or "",
                                                              c.get("meaning_upright") or ""))})
        return out
    if kind == "spread":
        d = (_load_json(TAROT_SPREADS) or {}).get("presets") or {}
        out = []
        for _, v in d.items():
            label = "%s（%s 张）" % (v.get("label"), v.get("count"))
            out.append({"id": label, "name": label, "preview": _pv(v.get("description"))})
        return out
    return []


def _tarot_material(kind, obj):
    if kind == "card":
        for c in (_load_json(TAROT_CARDS) or []):
            if (c.get("name") or "") == str(obj or "").split("（")[0].strip():
                return "\n".join([
                    "牌名：%s（%s，编号 %s）" % (c.get("name"), c.get("arcana"), c.get("number")),
                    "关键词：" + str(c.get("keywords") or ""),
                    "正位含义：\n" + str(c.get("meaning_upright") or ""),
                    "逆位含义：\n" + str(c.get("meaning_reversed") or "")])
        return ""
    if kind == "spread":
        for _, v in ((_load_json(TAROT_SPREADS) or {}).get("presets") or {}).items():
            if (v.get("label") or "") == str(obj or "").split("（")[0].strip():
                return "\n".join([
                    "牌阵：%s（%s 张）" % (v.get("label"), v.get("count")),
                    "说明：\n" + str(v.get("description") or ""),
                    "牌位：%s → %s" % ("、".join(v.get("positions") or []),
                                        "；".join(v.get("positionDetails") or []))])
        return ""
    return ""


# ---------------------------------------------------------------- 心理咨询
def _psych_items(kind):
    if kind != "scale":
        return []
    out = []
    try:
        c = sqlite3.connect(str(PSYCH_DB))
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT id, name, description, category, questions_count "
                         "FROM assessments ORDER BY category, id").fetchall()
        for r in rows:
            label = "%s（%s）" % (r["name"], r["category"] or "")
            out.append({"id": label, "name": label, "preview": _pv(r["description"]),
                        "count": r["questions_count"]})
        c.close()
    except Exception:
        return []
    return out


def _psych_material(kind, obj):
    if kind != "scale":
        return ""
    name = str(obj or "").split("（")[0].strip()
    try:
        c = sqlite3.connect(str(PSYCH_DB))
        c.row_factory = sqlite3.Row
        a = c.execute("SELECT id, name, description, category, estimated_time, questions_count "
                      "FROM assessments WHERE name=?", (name,)).fetchone()
        if not a:
            c.close()
            return ""
        qs = c.execute("SELECT question_text FROM questions WHERE assessment_id=? "
                       "ORDER BY order_index LIMIT 6", (a["id"],)).fetchall()
        c.close()
        parts = ["量表：%s（类别：%s，%s 题，约 %s 分钟）" % (a["name"], a["category"],
                                                          a["questions_count"], a["estimated_time"]),
                 "简介：\n" + str(a["description"] or "")]
        if qs:
            parts.append("样题（节选）：\n" + "\n".join("- " + str(q["question_text"]) for q in qs))
        return "\n\n".join(parts)
    except Exception:
        return ""


# ---------------------------------------------------------------- 统一入口
def topics(line_id, kind):
    if line_id == "lingxiu":
        if kind == "book":
            return _cached("lingxiu:book", _lingxiu_books)
        if kind == "topic":
            return _cached("lingxiu:topic", _lingxiu_topics, ttl=1800)
        return []
    if line_id == "maya":
        return _cached("maya:%s" % kind, lambda: _maya_items(kind))
    if line_id == "tarot":
        return _cached("tarot:%s" % kind, lambda: _tarot_items(kind))
    if line_id == "psych":
        return _cached("psych:%s" % kind, lambda: _psych_items(kind))
    return []


def material(line_id, kind, obj, book=None):
    """该选题的原始素材全文（LLM 的唯一事实来源）"""
    try:
        if line_id == "lingxiu":
            return _lingxiu_material(kind, obj, book)
        if line_id == "maya":
            return _maya_material(kind, obj)
        if line_id == "tarot":
            return _tarot_material(kind, obj)
        if line_id == "psych":
            return _psych_material(kind, obj)
    except Exception:
        return ""
    return ""


def promo_link(line_id, uid, src):
    """引流落点：子站首页 + 归因参数（uid 为空则返回空串）"""
    if not uid:
        return ""
    l = line_of(line_id) or line_of("lingxiu")
    return "%s/?ref=%s&src=%s" % (l["home"].rstrip("/"), uid, src or "")


def guide(line_id):
    l = line_of(line_id) or line_of("lingxiu")
    return l.get("guide") or ""


def kind_name(line_id, kind_id):
    for k in kinds(line_id):
        if k["id"] == kind_id:
            return k["name"]
    return kind_id or ""
