"""本地渲染推送卡片（Pillow）。

原来的图片模式靠第三方截图站，机房环境经常连不上；
这里改成在插件进程里直接画一张图：头像 + 昵称 + 时间 + 正文 + 配图 + 链接。

没装 Pillow 或者系统里找不到中文字体时，available() 返回 False，
调用方自动退回文本推送，不会报错。
"""

from __future__ import annotations

import glob
import io
import os
from typing import Optional

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except Exception:  # pragma: no cover - 没装 Pillow 就降级
    Image = ImageDraw = ImageFont = ImageOps = None


# 常见中文字体位置（Linux 服务器 / Windows / macOS）
FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/google-droid/DroidSansFallback.ttf",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
]

BOLD_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
]

WIDTH = 900
PADDING = 44
CARD_BG = (255, 255, 255)
TEXT_MAIN = (32, 36, 44)
TEXT_SUB = (138, 145, 158)
ACCENT = (251, 114, 153)

_font_cache: dict[tuple[int, bool], object] = {}
_regular_path: Optional[str] = None
_bold_path: Optional[str] = None
_fonts_scanned = False


def _find_fonts() -> None:
    global _regular_path, _bold_path, _fonts_scanned
    if _fonts_scanned:
        return
    _fonts_scanned = True

    for path in FONT_CANDIDATES:
        if os.path.isfile(path):
            _regular_path = path
            break

    for path in BOLD_CANDIDATES:
        if os.path.isfile(path):
            _bold_path = path
            break

    # 候选都没有就扫一遍 /usr/share/fonts，挑名字里带 CJK / 中文的
    if not _regular_path:
        patterns = ["**/*CJK*", "**/*cjk*", "**/*Han*", "**/*wqy*", "**/*Hei*", "**/*Song*"]
        for pattern in patterns:
            for path in glob.glob(os.path.join("/usr/share/fonts", pattern), recursive=True):
                if path.lower().endswith((".ttc", ".ttf", ".otf")):
                    _regular_path = path
                    break
            if _regular_path:
                break

    if not _bold_path:
        _bold_path = _regular_path


def available() -> bool:
    if Image is None:
        return False
    _find_fonts()
    return _regular_path is not None


def font(size: int, bold: bool = False):
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    path = _bold_path if bold and _bold_path else _regular_path
    try:
        face = ImageFont.truetype(path, size)
    except Exception:
        face = ImageFont.load_default()
    _font_cache[key] = face
    return face


def _strip_emoji(text: str) -> str:
    """图里没有 emoji 字体，画出来会是方块，干脆去掉。"""
    out = []
    for ch in text:
        code = ord(ch)
        if (
            0x1F000 <= code <= 0x1FAFF  # emoji
            or 0x2600 <= code <= 0x27BF  # 杂项符号
            or 0xFE00 <= code <= 0xFE0F  # 变体选择符
            or code == 0x200D  # 零宽连接
            or 0x1F1E6 <= code <= 0x1F1FF  # 区域旗帜
        ):
            continue
        out.append(ch)
    return "".join(out)


def wrap(draw, text: str, face, max_width: int) -> list[str]:
    """按像素宽度换行；中文逐字、英文尽量按词。"""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.rstrip()
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for ch in paragraph:
            trial = current + ch
            if draw.textlength(trial, font=face) <= max_width:
                current = trial
                continue
            if current:
                lines.append(current)
            current = ch
        if current:
            lines.append(current)
    return lines


def _round_avatar(data: bytes, size: int):
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img = ImageOps.fit(img, (size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _fit_image(data: bytes, max_width: int, max_height: int):
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
    if img.height > max_height:
        ratio = max_height / img.height
        img = img.resize((int(img.width * ratio), max_height), Image.LANCZOS)
    return img


def render_card(
    *,
    nick: str,
    text: str = "",
    title: str = "",
    time_text: str = "",
    link: str = "",
    kind: str = "动态",
    avatar: Optional[bytes] = None,
    images: Optional[list[bytes]] = None,
) -> Optional[bytes]:
    """画一张动态卡片，返回 PNG 字节；不可用时返回 None。"""
    if not available():
        return None

    images = images or []
    inner_width = WIDTH - PADDING * 2
    avatar_size = 76
    measure = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    # 先把文字排好，才能算画布高度
    body_lines: list[str] = []
    if title:
        body_lines.extend(wrap(measure, _strip_emoji(title), font(32, True), inner_width))
        body_lines.append("")
    if text:
        body_lines.extend(wrap(measure, _strip_emoji(text), font(28), inner_width))
    body_lines = body_lines[:26]

    body_font = font(28)
    body_line_h = 42
    body_height = len(body_lines) * body_line_h

    prepared_images = []
    for data in images[:3]:
        try:
            prepared_images.append(_fit_image(data, inner_width, 900))
        except Exception:
            continue

    images_height = sum(img.height + 16 for img in prepared_images)

    footer_h = 54
    height = PADDING + 76 + 28 + body_height + (images_height + 16 if prepared_images else 0) + footer_h + PADDING
    height = max(360, min(height, 4000))

    canvas = Image.new("RGB", (WIDTH, height), CARD_BG)
    draw = ImageDraw.Draw(canvas)

    # 顶部品牌色细条
    draw.rectangle((0, 0, WIDTH, 6), fill=ACCENT)

    x = PADDING
    y = PADDING

    # 头像
    if avatar:
        try:
            av = _round_avatar(avatar, avatar_size)
            canvas.paste(av, (x, y), av)
        except Exception:
            draw.ellipse((x, y, x + avatar_size, y + avatar_size), fill=(238, 240, 244))
    else:
        draw.ellipse((x, y, x + avatar_size, y + avatar_size), fill=(238, 240, 244))

    # 昵称 + 时间
    name_face = font(34, True)
    draw.text((x + avatar_size + 22, y + 6), _strip_emoji(nick)[:22], font=name_face, fill=TEXT_MAIN)
    sub = f"{kind} · {time_text}" if time_text else kind
    draw.text((x + avatar_size + 22, y + 50), sub, font=font(22), fill=TEXT_SUB)

    y += avatar_size + 28

    # 正文
    for line in body_lines:
        if y + body_line_h > height:
            break
        draw.text((x, y), line, font=body_font, fill=TEXT_MAIN)
        y += body_line_h

    # 配图
    if prepared_images:
        y += 10
        for img in prepared_images:
            if y + img.height > height - footer_h:
                break
            canvas.paste(img, (x, y))
            y += img.height + 16

    # 底部：类型标签 + 链接
    footer_y = height - PADDING - 26
    tag = f"B站{kind}"
    tag_w = draw.textlength(tag, font=font(20))
    draw.rounded_rectangle(
        (x, footer_y - 6, x + tag_w + 24, footer_y + 30), radius=16, fill=(253, 231, 238)
    )
    draw.text((x + 12, footer_y), tag, font=font(20), fill=ACCENT)

    link_text = link.replace("https://", "").replace("http://", "")
    if len(link_text) > 46:
        link_text = link_text[:46] + "…"
    draw.text((x + tag_w + 44, footer_y), link_text, font=font(20), fill=TEXT_SUB)

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
