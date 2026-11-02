"""仪表盘后端接口测试（不走真实网络）。"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import setup  # noqa: E402

setup()

from astrbot.api import web as stub_web  # noqa: E402
from astrbot.api.star import Context  # noqa: E402

from astrbot_plugin_bili_monitor.main import BiliMonitor, PLUGIN_DIR_NAME  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


class FakeClient:
    def __init__(self):
        self.items = []
        self.live_status = 0
        self.online = 0
        self.last_room = None
        self.poll_status = 86101

    async def dynamics(self, uid, offset=""):
        return {"items": list(self.items), "offset": "", "has_more": False}

    async def qr_login_generate(self):
        return {"url": "https://example.com/scan?qrcode_key=KEY123", "key": "KEY123"}

    async def nav_info(self):
        return {"is_login": True, "name": "测试君", "mid": "42", "face": ""}

    async def qr_login_poll(self, key):
        cookies = (
            {"SESSDATA": "abc", "bili_jct": "def", "DedeUserID": "42"}
            if self.poll_status == 0
            else {}
        )
        return {"status": self.poll_status, "message": "", "cookies": cookies, "url": ""}

    async def live_master(self, uid):
        return {"room_id": 5551, "uname": "测试UP", "face": ""}

    async def live_room_id(self, uid):
        return 5551

    async def live_room(self, room_id):
        self.last_room = room_id
        return {"live_status": self.live_status, "online": self.online, "title": "测试直播", "cover": ""}

    async def close(self):
        pass


def build(conf):
    ctx = Context()
    mon = BiliMonitor(ctx, conf)
    mon._client = FakeClient()
    # 每个测试用独立状态文件，避免上次运行留下的昵称/房间号干扰断言
    mon._state = {}
    mon._state_path = os.path.join(tempfile.mkdtemp(), "state.json")
    return mon, ctx


CONF = {
    "uids": ["946974"],
    "push_targets": ["aiocqhttp:GroupMessage:1"],
    "room_overrides": [],
    "check_interval": 90,
    "image_mode": False,
    "enable": False,
}


async def main():
    print("[1] 路由注册")
    mon, ctx = build(dict(CONF))
    routes = [r[0] for r in ctx.web_routes]
    check("注册 6 个接口", len(routes) == 6, routes)
    check("带插件名前缀", all(r.startswith(f"/{PLUGIN_DIR_NAME}/") for r in routes), routes)

    print("[2] 概览")
    payload = mon._overview_payload()
    check("运行状态字段", "running" in payload)
    check("配置回显", payload["config"]["uids"] == ["946974"], payload["config"])
    check("条目字段齐全", set(payload["items"][0]) >= {"uid", "room_id", "live", "last_check", "seen"}, payload["items"])
    check("条目带推送信息", {"sessions", "own_targets"} <= set(payload["items"][0]), payload["items"][0])
    check("严格隔离默认开", payload["config"]["strict_isolation"] is True, payload["config"])

    print("[3] 保存配置")
    stub_web.request.payload = {
        "uids": ["946974", "208259", "946974"],
        "push_targets": ["aiocqhttp:GroupMessage:9", "aiocqhttp:GroupMessage:9"],
        "room_overrides": ["946974:173551", "bad", "208259:0"],
        "check_interval": 5,
        "quiet_hours": "23:30-07:30",
        "image_mode": True,
    }
    res = await mon.api_save_config()
    conf = mon.conf
    check("UID 去重", conf["uids"] == ["946974", "208259"], conf["uids"])
    check("目标去重", conf["push_targets"] == ["aiocqhttp:GroupMessage:9"], conf["push_targets"])
    check("房间号过滤", conf["room_overrides"] == ["946974:173551"], conf["room_overrides"])
    check("间隔下限 30", conf["check_interval"] == 30, conf["check_interval"])
    check("返回新概览", res["status"] == "ok" and res["data"]["overview"]["config"]["image_mode"] is True)

    print("[3b] 保存每个 UP 的推送群")
    stub_web.request.payload = {
        "routes": [
            "946974|aiocqhttp:GroupMessage:AAA",
            "bad-line",
            "208259:aiocqhttp:GroupMessage:BBB",
            "946974|aiocqhttp:GroupMessage:AAA",
        ],
        "strict_isolation": True,
    }
    await mon.api_save_config()
    check("routes 规范化去重",
          mon.conf["routes"] == ["946974|aiocqhttp:GroupMessage:AAA", "208259|aiocqhttp:GroupMessage:BBB"],
          mon.conf.get("routes"))
    subs = {s["uid"]: s for s in mon._subscriptions()}
    check("946974 只推 AAA", subs["946974"]["sessions"] == ["aiocqhttp:GroupMessage:AAA"], subs["946974"]["sessions"])
    check("208259 只推 BBB", subs["208259"]["sessions"] == ["aiocqhttp:GroupMessage:BBB"], subs["208259"]["sessions"])

    print("[4] 参数校验")
    stub_web.request.payload = {"quiet_hours": "晚上"}
    bad = await mon.api_save_config()
    check("勿扰格式报错", bad[1] == 400 and "23:30" in bad[0]["message"], bad)

    stub_web.request.payload = {"check_interval": "abc"}
    bad2 = await mon.api_save_config()
    check("间隔类型报错", bad2[1] == 400, bad2)

    print("[5] 启停开关")
    called = {"init": 0}

    async def fake_init():
        called["init"] += 1

    mon.initialize = fake_init  # type: ignore[assignment]
    mon._task = None
    stub_web.request.payload = {"enable": True}
    await mon.api_save_config()
    check("开监控会重启循环", called["init"] == 1, called)

    print("[6] 测试推送")
    mon.conf["routes"] = []
    ctx.sent.clear()
    res = await mon.api_test_push()
    check("按目标数发送", res["data"]["sent"] == 1 and len(ctx.sent) == 1, (res, len(ctx.sent)))
    check("内容含测试", "测试" in str(ctx.sent[0][1]), str(ctx.sent[0][1]))

    # 只配了专属群（没有默认推送）时，测试推送也应该能发出去
    mon.conf["push_targets"] = []
    mon.conf["routes"] = ["111|aiocqhttp:GroupMessage:ROUTE"]
    ctx.sent.clear()
    res = await mon.api_test_push()
    check("测试推送认专属群",
          res["data"]["sent"] == 1 and ctx.sent and ctx.sent[0][0] == "aiocqhttp:GroupMessage:ROUTE",
          (res, [s[0] for s in ctx.sent]))

    mon.conf["routes"] = []
    mon.conf["push_targets"] = []
    res = await mon.api_test_push()
    check("没有目标时报错", res[1] == 400, res)

    print("[7] 立即检查")
    ctx.sent.clear()
    mon.conf["push_targets"] = ["aiocqhttp:GroupMessage:1"]
    mon._client.items = [{
        "id_str": "9001",
        "type": "DYNAMIC_TYPE_WORD",
        "modules": {
            "module_author": {"mid": 946974, "name": "测试UP", "face": "", "pub_ts": int(__import__("time").time())},
            "module_dynamic": {"desc": {"text": "接口测试动态"}, "major": None},
        },
    }]
    res = await mon.api_check_now()
    results = res["data"]["results"]
    check("返回结果", results and results[0]["uid"] == "946974", results)
    check("记录到新动态", results[0]["new"] >= 1, results[0])
    check("概览已刷新", res["data"]["overview"]["items"][0]["name"] == "测试UP", res["data"]["overview"]["items"])

    mon.conf["uids"] = []
    res = await mon.api_check_now()
    check("没订阅时报错", res[1] == 400, res)

    print("[8] 扫码登录")
    mon.conf["uids"] = ["946974"]
    res = await mon.api_login_qr()
    check("生成二维码", res["status"] == "ok" and res["data"]["key"] == "KEY123", res)
    check("带二维码图片", str(res["data"].get("image", "")).startswith("data:image/png;base64,"),
          str(res["data"].get("image", ""))[:30])

    mon.conf["cookie"] = ""
    stub_web.request.payload = {"key": "KEY123"}
    res = await mon.api_login_poll()
    check("未扫码不保存", res["status"] == "ok" and res["data"]["status"] == 86101
          and not mon.conf.get("cookie"), res)

    mon._client.poll_status = 86090
    res = await mon.api_login_poll()
    check("已扫码待确认", res["data"]["status"] == 86090 and not mon.conf.get("cookie"), res["data"])

    # 登录成功会重建 HTTP 客户端，这里让重建出来的也是假客户端，避免真联网
    import astrbot_plugin_bili_monitor.main as main_mod

    mon._client.poll_status = 0
    real_factory = main_mod.BiliClient
    main_mod.BiliClient = lambda *a, **k: FakeClient()
    try:
        res = await mon.api_login_poll()
    finally:
        main_mod.BiliClient = real_factory
    check("成功后保存 cookie", res["data"]["cookie_set"] is True, res["data"])
    check("cookie 内容正确",
          "SESSDATA=abc" in mon.conf.get("cookie", "") and "bili_jct=def" in mon.conf.get("cookie", ""),
          mon.conf.get("cookie"))
    check("成功后返回概览", bool(res["data"].get("overview")), list(res["data"]))
    check("记录绑定账号", res["data"].get("account", {}).get("name") == "测试君", res["data"].get("account"))
    check("概览里带账号", res["data"]["overview"]["config"]["account"]["mid"] == "42",
          res["data"]["overview"]["config"]["account"])

    mon._pending_login = None
    stub_web.request.payload = {}
    res = await mon.api_login_poll()
    check("缺 key 时报错", res[1] == 400, res)

    from astrbot.api.event import AstrMessageEvent

    mon.conf["admin_only_login"] = True
    check("管理员可扫码登录", mon._can_login(AstrMessageEvent(admin=True)))
    check("非管理员被拒", not mon._can_login(AstrMessageEvent(admin=False)))
    mon.conf["admin_only_login"] = False
    check("关掉限制后谁都能登", mon._can_login(AstrMessageEvent(admin=False)))

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
