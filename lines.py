#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业务线（内容线）：选题来源 + 引流落点 + 文末引导语

选题**全部读各子站现成资产**，不另建内容库：
  · 灵性书籍 → lingxiu /var/www/lingxiu/data/xianbao.db（books/categories/themes，由 app.py 原有逻辑提供）
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
KIN_CYCLE = 260

# 业务线定义：home = 引流落点（子网站首页），guide = 文末引导语
LINES = [
    {"id": "lingxiu", "name": "灵性书籍", "icon": "📚",
     "home": "https://xianbao.love",
     "guide": "🌙 想了解更多心灵成长内容？",
     "kinds": [{"id": "book", "name": "书目选题"}]},
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
    if line_id == "maya":
        return _cached("maya:%s" % kind, lambda: _maya_items(kind))
    if line_id == "tarot":
        return _cached("tarot:%s" % kind, lambda: _tarot_items(kind))
    if line_id == "psych":
        return _cached("psych:%s" % kind, lambda: _psych_items(kind))
    return []


def material(line_id, kind, obj):
    """该选题的原始素材全文（LLM 的唯一事实来源）"""
    try:
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
