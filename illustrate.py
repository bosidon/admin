#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配图生成模块：金句卡（PIL）+ 推广二维码"""
import io, os, math, re, textwrap
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import qrcode

# 字体：Noto Sans CJK（.ttc 是字体集合，必须用 index 选字面）
FONT_PATH = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
FONT_INDEX = 2                                     # 2 = Noto Sans CJK SC（简体）
FALLBACK_FONT_PATH = '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf'

# 卡片叠字前要剔除的字符：emoji / 变体选择符 / 零宽字符
# 原因：Droid 与 Noto CJK 都没有 emoji 字形，画出来是"空心方框 / 带叉方框"
_STRIP_RE = re.compile(
    '['
    '\U0001F000-\U0001FAFF'      # emoji 主区（表情/手势/交通/符号…）
    '\U00002600-\U000027BF'      # 杂项符号 + 装饰符号（☀★☆⚡✨✅❌…）
    '\U00002B00-\U00002BFF'      # 杂项符号与箭头（⬜⭐…）
    '\U0000FE00-\U0000FE0F'      # 变体选择符
    '\U000E0100-\U000E01EF'      # 变体选择符补充
    '\U0000200B-\U0000200F'      # 零宽字符
    '\U00002060-\U00002064'
    '\U0000FEFF'                  # BOM
    ']+')


def _clean_text(t):
    """剔除卡片渲染不出的 emoji/变体选择符/零宽字符，并压缩多余空格"""
    t = _STRIP_RE.sub('', t or '')
    return re.sub(r'[ \t]{2,}', ' ', t).strip()

# 尺寸规格（宽 x 高）
SIZES = {
    'xiaohongshu': (1080, 1440),   # 3:4  小红书
    'moments':     (1080, 1080),   # 1:1  朋友圈
    'wechat':      (1080, 460),    # 2.35:1 公众号头图
    'story':       (1080, 1920),   # 9:16 短视频封面
}

# 配色方案：背景渐变（起/止）+ 主文字 + 强调色 + 品牌色
STYLES = {
    'purple': {'bg': ((38, 22, 66), (18, 12, 34)), 'text': (255, 255, 255), 'accent': (200, 166, 92), 'brand': (167, 139, 250)},
    'dark':   {'bg': ((18, 18, 22), (8, 8, 10)),   'text': (240, 238, 235), 'accent': (200, 166, 92), 'brand': (148, 163, 184)},
    'gold':   {'bg': ((250, 246, 238), (238, 228, 210)), 'text': (62, 48, 36), 'accent': (168, 118, 42), 'brand': (140, 110, 70)},
    'maya':   {'bg': ((16, 52, 58), (10, 30, 40)),  'text': (235, 245, 240), 'accent': (212, 175, 55), 'brand': (94, 200, 180)},
}


def _font(size):
    try:
        return ImageFont.truetype(FONT_PATH, size, index=FONT_INDEX)
    except Exception:
        return ImageFont.truetype(FALLBACK_FONT_PATH, size)


def _gradient_bg(w, h, c1, c2):
    """垂直渐变背景"""
    base = Image.new('RGB', (w, h), c1)
    top = Image.new('RGB', (w, h), c2)
    mask = Image.new('L', (w, h))
    md = mask.load()
    for y in range(h):
        v = int(255 * (y / max(1, h - 1)))
        for x in range(w):
            md[x, y] = v
    base.paste(top, (0, 0), mask)
    return base


def _wrap(text, font, max_w, draw):
    """按像素宽度自动换行（支持中英文）"""
    lines, cur = [], ''
    for ch in text:
        if ch == '\n':
            lines.append(cur); cur = ''; continue
        test = cur + ch
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def make_qrcode(link, box=260, label='扫码了解详情', brand_color=(200, 166, 92)):
    """生成带标签的推广二维码（白底圆角）"""
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(link)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color='black', back_color='white').convert('RGB')
    qr_img = qr_img.resize((box, box), Image.LANCZOS)

    pad = 14
    lab_h = 44 if label else 0
    canvas = Image.new('RGB', (box + pad * 2, box + pad * 2 + lab_h), (255, 255, 255))
    canvas.paste(qr_img, (pad, pad))
    if label:
        d = ImageDraw.Draw(canvas)
        f = _font(24)
        tw = d.textlength(label, font=f)
        d.text(((canvas.width - tw) / 2, box + pad + 6), label, font=f, fill=(70, 70, 70))
    # 圆角
    radius = 18
    mask = Image.new('L', canvas.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, canvas.width - 1, canvas.height - 1], radius=radius, fill=255)
    out = Image.new('RGB', canvas.size, (255, 255, 255))
    out.paste(canvas, (0, 0), mask)
    return out


def make_quote_card(text, style='purple', size='xiaohongshu', qr_link=None, brand='仙宝心灵成长'):
    """生成金句卡（渐变背景 + 金句 + 品牌 + 可选二维码）"""
    w, h = SIZES.get(size, SIZES['xiaohongshu'])
    st = STYLES.get(style, STYLES['purple'])
    img = _gradient_bg(w, h, st['bg'][0], st['bg'][1])

    # 微光晕（提升质感）
    glow = Image.new('RGB', (w, h), st['bg'][0])
    gd = ImageDraw.Draw(glow)
    gd.ellipse([w * 0.1, -h * 0.15, w * 0.9, h * 0.45], fill=tuple(min(255, c + 26) for c in st['bg'][0]))
    img = Image.blend(img, glow.filter(ImageFilter.GaussianBlur(120)), 0.55)

    _draw_quote_text(img, text, st, size, qr_link, brand)
    return img


def _draw_quote_text(img, text, st, size, qr_link=None, brand='仙宝心灵成长',
                     text_fill=None, brand_fill=None):
    """在 img 上叠加金句 + 装饰线 + 品牌 + 二维码（模板卡 / AI 卡共用）"""
    text = _clean_text(text)
    brand = _clean_text(brand)
    w, h = img.size
    draw = ImageDraw.Draw(img)
    tx = text_fill if text_fill else st['text']
    bfill = brand_fill if brand_fill else st['brand']

    # ---- 金句文字 ----
    is_wide = size == 'wechat'
    fsize = int(min(w, h) * (0.072 if not is_wide else 0.095))
    font = _font(fsize)
    max_w = w - int(w * 0.16)
    lines = _wrap(text, font, max_w, draw)

    line_h = int(fsize * 1.62)
    total_h = line_h * len(lines)
    y = (h - total_h) / 2 - (h * 0.03)
    for ln in lines:
        tw = draw.textlength(ln, font=font)
        draw.text(((w - tw) / 2, y), ln, font=font, fill=tx)
        y += line_h

    # ---- 装饰线 ----
    dy = (h - total_h) / 2 - h * 0.075
    lw = int(w * 0.09)
    draw.line([(w / 2 - lw, dy), (w / 2 + lw, dy)], fill=st['accent'], width=4)

    # ---- 品牌署名 ----
    bf = _font(int(fsize * 0.42))
    bw = draw.textlength(brand, font=bf)
    by = (h + total_h) / 2 + h * 0.055
    draw.text(((w - bw) / 2, by), brand, font=bf, fill=bfill)

    # ---- 推广二维码（右下角）----
    if qr_link:
        box = int(min(w, h) * 0.20)
        qr_img = make_qrcode(qr_link, box=box)
        margin = int(min(w, h) * 0.045)
        img.paste(qr_img, (w - qr_img.width - margin, h - qr_img.height - margin))
    return img


# AI 卡（背景由 ComfyUI 生成）用的文字色：统一近白，配渐变暗化保证可读
AI_CARD_TEXT = (246, 246, 250)
AI_CARD_BRAND = (214, 214, 222)


def _cover(img, w, h):
    """等比缩放 + 居中裁切到 w×h（cover 语义）"""
    iw, ih = img.size
    s = max(w / float(iw), h / float(ih))
    nw = max(w, int(iw * s + 0.5))
    nh = max(h, int(ih * s + 0.5))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _scrim(img, st, base_alpha=0.60):
    """垂直渐变暗化（上下更暗）—— 让近白文字在任意 AI 背景上都可读"""
    w, h = img.size
    dark = tuple(int(c * 0.20) for c in st['bg'][0])
    layer = Image.new('RGB', (w, h), dark)
    alpha = Image.new('L', (1, h))
    ad = alpha.load()
    for y in range(h):
        t = abs(y / float(max(1, h - 1)) - 0.5) * 2.0     # 0=中间, 1=上下边缘
        ad[0, y] = int(255 * base_alpha * (0.62 + 0.38 * t))
    alpha = alpha.resize((w, h), Image.BILINEAR)
    return Image.composite(layer, img, alpha)


def compose_card_over_bg(bg_bytes, text, style='purple', size='xiaohongshu',
                         qr_link=None, brand='仙宝心灵成长'):
    """AI 背景 + PIL 叠字：金句卡（背景由 ComfyUI 出，文字/二维码由 PIL 保证清晰可扫）"""
    w, h = SIZES.get(size, SIZES['xiaohongshu'])
    st = STYLES.get(style, STYLES['purple'])
    img = Image.open(io.BytesIO(bg_bytes)).convert('RGB')
    img = _cover(img, w, h)
    img = _scrim(img, st)
    _draw_quote_text(img, text, st, size, qr_link, brand,
                     text_fill=AI_CARD_TEXT, brand_fill=AI_CARD_BRAND)
    return img


def stamp_qr(src_path, link, box_ratio=0.20, out_path=None):
    """在已生成的配图上叠加推广二维码（右下角）；
    默认另存为 <原名>_q.png（原图不动）。返回输出文件路径。"""
    img = Image.open(src_path).convert('RGB')
    w, h = img.size
    box = int(min(w, h) * box_ratio)
    qr_img = make_qrcode(link, box=box)
    margin = int(min(w, h) * 0.045)
    img.paste(qr_img, (w - qr_img.width - margin, h - qr_img.height - margin))
    if not out_path:
        root, ext = os.path.splitext(src_path)
        out_path = root + '_q' + (ext or '.png')
    img.save(out_path, 'PNG', optimize=True)
    return out_path
