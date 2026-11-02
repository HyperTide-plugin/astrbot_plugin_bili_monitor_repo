"""二维码生成测试（不联网）。"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import setup  # noqa: E402

setup()

from astrbot_plugin_bili_monitor import qr  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


def png_size(data: bytes) -> tuple[int, int]:
    # PNG: 8 字节签名 + 4 字节长度 + 'IHDR' + 宽(4) 高(4)
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def main():
    print("[1] 依赖可用")
    check("qrcode 已安装", qr.available())

    print("[2] 生成 PNG")
    png = qr.make_png("https://example.com/scan?qrcode_key=abc123")
    check("有输出", bool(png))
    check("PNG 签名", png[:8] == b"\x89PNG\r\n\x1a\n", png[:8])

    width, height = png_size(png)
    check("正方形", width == height and width > 100, (width, height))
    check("尺寸是模块整数倍", width % 8 == 0, width)

    print("[3] data URI")
    uri = qr.data_uri("hello")
    check("前缀正确", uri.startswith("data:image/png;base64,"), uri[:30])
    check("长度合理", len(uri) > 500, len(uri))

    print("[4] 依赖缺失时降级")
    backup = qr.qrcode
    qr.qrcode = None
    try:
        check("make_png 返回 None", qr.make_png("x") is None)
        check("data_uri 返回空串", qr.data_uri("x") == "")
        check("available 为 False", qr.available() is False)
    finally:
        qr.qrcode = backup

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
