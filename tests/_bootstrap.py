"""测试引导。

仓库根目录本身就是插件目录（AstrBot 要求 metadata.yaml 在根），
所以这里手动把根目录注册成包 `astrbot_plugin_bili_monitor`，
这样 main.py 里的相对导入（from .bili_api import ...）能正常工作，
也不用往仓库根塞一个 __init__.py。
"""

import os
import sys
import types

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
PACKAGE = "astrbot_plugin_bili_monitor"


def setup() -> None:
    stub = os.path.join(TESTS_DIR, "stub")
    for path in (stub, ROOT):
        if path not in sys.path:
            sys.path.insert(0, path)

    if PACKAGE not in sys.modules:
        pkg = types.ModuleType(PACKAGE)
        pkg.__path__ = [ROOT]
        sys.modules[PACKAGE] = pkg
