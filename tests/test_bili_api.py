"""B 站客户端的风控/代理逻辑测试（不联网）。"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import setup  # noqa: E402

setup()

from astrbot_plugin_bili_monitor.bili_api import BiliClient, BiliRiskError  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


async def main():
    print("[1] 浏览器指纹 Cookie")
    c = BiliClient("SESSDATA=abc; bili_jct=xyz")
    c._buvid = {"buvid3": "B3", "buvid4": "B4"}
    cookie = c._cookie_header()
    check("带 buvid3", "buvid3=B3" in cookie, cookie)
    check("带 b_nut", "b_nut=" in cookie, cookie)
    check("带 _uuid", "_uuid=" in cookie, cookie)
    check("带 b_lsid", "b_lsid=" in cookie, cookie)
    check("保留用户 cookie", "SESSDATA=abc" in cookie and "bili_jct=xyz" in cookie, cookie)
    import re as _re

    u1 = _re.search(r"_uuid=([^;]+)", c._cookie_header()).group(1)
    u2 = _re.search(r"_uuid=([^;]+)", c._cookie_header()).group(1)
    check("_uuid 稳定", u1 == u2, (u1, u2))

    print("[2] 代理解析")
    p1 = BiliClient("", "http://127.0.0.1:7890")._proxy_args()
    check("普通代理", p1 == {"proxy": "http://127.0.0.1:7890"}, p1)

    p2 = BiliClient("", "http://user:p%40ss@1.2.3.4:8080")._proxy_args()
    check("带账号代理 host", p2.get("proxy") == "http://1.2.3.4:8080", p2)
    check("带账号代理 auth", p2.get("proxy_auth") is not None
          and p2["proxy_auth"].login == "user"
          and p2["proxy_auth"].password == "p@ss", p2)

    check("无代理", BiliClient("")._proxy_args() == {})

    print("[3] 风控冷却")
    c2 = BiliClient("", "", 120)
    check("初始不冷却", c2.blocked_for() == 0)
    c2._block("HTTP 412 request was banned")
    check("拦截后进入冷却", c2.blocked_for() > 100, c2.blocked_for())

    try:
        await c2._api("https://api.bilibili.com/x/relation/stat", {"vmid": "2"})
        check("冷却期直接短路", False, "居然发出了请求")
    except BiliRiskError as e:
        check("冷却期直接短路", "冷却中" in str(e), str(e))

    print("[4] 冷却到期")
    c3 = BiliClient("", "", 60)
    c3._blocked_until = 0
    check("到期后放行", c3.blocked_for() == 0)

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
