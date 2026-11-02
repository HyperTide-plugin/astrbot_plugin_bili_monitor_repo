"""联网冒烟测试：打真实的 B 站接口，验证 WBI 签名与卡片解析。"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import setup  # noqa: E402

setup()

from astrbot_plugin_bili_monitor.bili_api import BiliClient, BiliError, BiliRiskError  # noqa: E402
from astrbot_plugin_bili_monitor.parser import parse_dynamic  # noqa: E402


async def main() -> int:
    uid = sys.argv[1] if len(sys.argv) > 1 else "208259"
    client = BiliClient()
    failed = 0
    try:
        try:
            fans = await client.follower_count(uid)
            print(f"[fan]  {uid} 粉丝数 = {fans}")
        except BiliError as e:
            failed += 1
            print(f"[fan]  失败: {e}")

        try:
            info = await client.user_info(uid)
            print(f"[user] {info.get('name')!r} level={info.get('level')} sign={(info.get('sign') or '')[:30]!r}")
        except (BiliError, BiliRiskError) as e:
            failed += 1
            print(f"[user] 失败: {e}")

        try:
            data = await client.dynamics(uid)
            items = data.get("items") or []
            print(f"[dyn]  拿到 {len(items)} 条动态")
            for raw in items[:6]:
                p = parse_dynamic(raw)
                if not p:
                    print("   - 解析失败", str(raw)[:60])
                    continue
                head = (p["text"] or p["title"] or "").replace("\n", " ")[:40]
                print(f"   - {p['kind']:8s} {p['ts']} {head!r} imgs={len(p['images'])} {p['link']}")
        except (BiliError, BiliRiskError) as e:
            failed += 1
            print(f"[dyn]  失败: {e}")

        try:
            room_id = await client.live_room_id(uid)
            print(f"[room] {uid} room_id={room_id}")
            if room_id:
                room = await client.live_room(room_id)
                print(f"[live] status={room.get('live_status')} online={room.get('online')} title={(room.get('title') or '')[:30]!r}")
        except (BiliError, BiliRiskError) as e:
            print(f"[live] 失败（不一定是问题）: {e}")

        try:
            qr = await client.qr_login_generate()
            print(f"[qr]   key={'有' if qr.get('key') else '无'} url={str(qr.get('url'))[:60]}")
            if qr.get("key"):
                poll = await client.qr_login_poll(qr["key"])
                print(f"[qr]   poll status={poll.get('status')} message={poll.get('message')!r}")
        except (BiliError, BiliRiskError) as e:
            failed += 1
            print(f"[qr]   失败: {e}")
    finally:
        await client.close()

    print("失败项:", failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
