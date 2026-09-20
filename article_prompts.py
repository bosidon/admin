#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""4 条业务线各自的「文案 Agent 提词」——文件可配、可重置

· 文件：prompts/article_<line>.md（lingxiu / maya / tarot / psych），设置页可编辑，改完立即生效
· 占位符：{{平台}} 这类中文双花括号；运行时按 vars 替换，缺值渲染成「（无）」
· 缺文件 → 写回内置默认（与 image_agent.md 的机制一致）
"""
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROMPT_DIR = BASE_DIR / "prompts"

LINES = ("lingxiu", "maya", "tarot", "psych")
LINE_NAME = {"lingxiu": "灵性书籍", "maya": "玛雅天赋", "tarot": "塔罗", "psych": "心理咨询"}

# 设置页要展示的「本条可用占位符」
PLACEHOLDERS_COMMON = ["业务线", "平台", "内容类型", "字数", "选题", "语气", "输出格式", "素材", "品牌指引"]
PLACEHOLDERS = {
    "lingxiu": PLACEHOLDERS_COMMON + ["书目", "话题", "角度", "结构", "钩子"],
    "maya": PLACEHOLDERS_COMMON + ["选题类型", "选题对象"],
    "tarot": PLACEHOLDERS_COMMON + ["选题类型", "选题对象"],
    "psych": PLACEHOLDERS_COMMON + ["选题类型", "选题对象"],
}

# A 方案：4 条线先用同一套骨架，各自独立可改
SKELETON = """你是「仙宝心灵成长」{{业务线}}业务线的专职文案 Agent，产出可直接发布的{{内容类型}}。

## 本次参数
- 平台：{{平台}}
- 内容类型：{{内容类型}}
- 字数要求：{{字数}}
- 选题：{{选题}}
- 语气：{{语气}}
- 角度：{{角度}}
- 结构：{{结构}}
- 钩子：{{钩子}}
- 输出格式：{{输出格式}}

## 选题素材（**唯一事实来源**）
{{素材}}

## 品牌指引（摘要）
{{品牌指引}}

## 写作要求
1. 只用素材里的术语与说法（如印记 / 图腾 / 音阶 / 波符这类原体系词汇），**不要换成星座、占星等其它体系的说法**；不要编造素材里没有的数据、年份、比例或案例
2. 从读者真实困惑切入（"我为什么总是…"这类），不要写成百科词条
3. 结尾给一个可执行的小练习或自我提问
4. 不要输出任何网址或链接（系统会自动在文末追加推广链接）
5. 标签 5-8 个

## 输出
只输出 JSON，不要解释、不要代码块标记：
{"title":"标题","content":"正文","summary":"120字摘要","tags":"标签1,标签2,..."}
"""

DEFAULT = {ln: SKELETON for ln in LINES}

_PH_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def file_of(line):
    return PROMPT_DIR / ("article_%s.md" % line)


def load_raw(line):
    """该线提词原文；缺失/为空 → 写回默认并返回默认"""
    if line not in LINES:
        raise KeyError("未知业务线：%s" % line)
    p = file_of(line)
    try:
        t = p.read_text(encoding="utf-8").strip()
        if t:
            return t
    except Exception:
        pass
    save(line, DEFAULT[line])
    return DEFAULT[line].strip()


def save(line, text):
    """写文件；返回 (ok, 错误文案)"""
    if line not in LINES:
        return False, "未知业务线：%s" % line
    t = (text or "").strip()
    if not t:
        return False, "内容不能为空"
    try:
        PROMPT_DIR.mkdir(parents=True, exist_ok=True)
        file_of(line).write_text(t + "\n", encoding="utf-8")
        return True, None
    except Exception as e:
        return False, "写入失败：%s" % e


def reset(line):
    return save(line, DEFAULT[line])


def render(line, values):
    """把提词里的 {{占位符}} 换成实际值；未提供的渲染成「（无）」"""
    raw = load_raw(line)

    def sub(m):
        k = m.group(1).strip()
        v = values.get(k)
        v = "" if v is None else str(v).strip()
        return v if v else "（无）"

    return _PH_RE.sub(sub, raw)


def meta(line):
    """设置页需要的信息"""
    return {"line": line, "name": LINE_NAME.get(line, line),
            "path": "prompts/article_%s.md" % line,
            "placeholders": PLACEHOLDERS.get(line, PLACEHOLDERS_COMMON)}


if __name__ == "__main__":
    # 自检：把 4 条线的提词都渲染一遍，打印未替换的占位符
    for ln in LINES:
        vals = {"业务线": LINE_NAME[ln], "平台": "抖音", "内容类型": "短视频脚本", "字数": "对应15-60秒视频",
                "选题": "红龙 · 源动力", "语气": "温暖、真诚", "角度": "自我接纳", "结构": "故事+干货",
                "钩子": "提问开场", "输出格式": "台词脚本", "素材": "（示例素材）", "品牌指引": "（示例指引）"}
        out = render(ln, vals)
        left = _PH_RE.findall(out)
        print("%-8s 渲染 %d 字 ｜ 未替换占位符: %s" % (ln, len(out), left if left else "无 ✅"))
