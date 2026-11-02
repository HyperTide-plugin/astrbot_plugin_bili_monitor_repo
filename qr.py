"""二维码生成。

只依赖 `qrcode`（纯 Python，拿矩阵），PNG 自己用 zlib 拼出来——
这样不用为了出图再拉一个 Pillow。qrcode 没装时返回 None，
调用方降级成"把链接发给用户"。
"""

from __future__ import annotations

import struct
import zlib
from typing import Optional

try:
    import qrcode
except Exception:  # pragma: no cover - 没装就降级
    qrcode = None


def available() -> bool:
    return qrcode is not None


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _matrix_to_png(matrix: list[list[bool]], box: int = 8, border: int = 2) -> bytes:
    """把 0/1 矩阵画成 8 位灰度 PNG。"""
    n = len(matrix)
    width = (n + border * 2) * box
    rows: list[bytes] = []
    white = b"\xff" * box
    black = b"\x00" * box

    for y in range(-border, n + border):
        line = bytearray()
        for x in range(-border, n + border):
            dark = 0 <= y < n and 0 <= x < n and bool(matrix[y][x])
            line.extend(black if dark else white)
        row = b"\x00" + bytes(line)  # 每行前缀是 PNG 的 filter type
        rows.extend([row] * box)

    raw = b"".join(rows)
    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, width, 8, 0, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(raw, 9))
    png += _chunk(b"IEND", b"")
    return png


def make_png(data: str, box: int = 8) -> Optional[bytes]:
    """把文本编成二维码 PNG；qrcode 不可用时返回 None。"""
    if qrcode is None or not data:
        return None
    qr = qrcode.QRCode(border=0)
    qr.add_data(data)
    qr.make(fit=True)
    return _matrix_to_png(qr.get_matrix(), box=box)


def data_uri(data: str, box: int = 8) -> str:
    """给仪表盘用的 data: URI；不可用时返回空串。"""
    png = make_png(data, box=box)
    if not png:
        return ""
    import base64

    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
