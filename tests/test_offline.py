"""离线冒烟测试：不联网，验证解析、去重、推送与直播状态机。"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import setup  # noqa: E402

setup()

from astrbot.api.star import Context  # noqa: E402

from astrbot_plugin_bili_monitor.main import (  # noqa: E402
    BiliMonitor,
    fmt_duration,
    in_quiet_hours,
    parse_subscriptions,
    render_tpl,
)
from astrbot_plugin_bili_monitor.parser import parse_dynamic  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


def item(i, ts, name="测试UP", major=None, text=None, type_="DYNAMIC_TYPE_DRAW", pinned=False, orig=None):
    modules = {
        "module_author": {"mid": 123, "name": name, "face": "//i0.hdslb.com/bfs/face/a.jpg", "pub_ts": ts},
        "module_dynamic": {
            "desc": {"text": text if text is not None else f"动态内容{i}"},
            "major": major,
        },
    }
    if pinned:
        modules["module_tag"] = {"text": "置顶"}
    data = {"id_str": str(i), "type": type_, "modules": modules}
    if orig:
        data["orig"] = orig
    return data


# ---------------------------------------------------------------- 配置解析
print("[1] 订阅配置解析")
subs = parse_subscriptions("208259 | aiocqhttp:GroupMessage:1 | 官方")
check("单行", subs == [{"uid": "208259", "sessions": ["aiocqhttp:GroupMessage:1"], "note": "官方"}], subs)

subs = parse_subscriptions("# 注释\n2 | a,b\n208259 | c\n2 | d")
check("注释+合并", [(s["uid"], s["sessions"]) for s in subs] == [("2", ["a", "b", "d"]), ("208259", ["c"])], subs)

subs = parse_subscriptions("123：aiocqhttp:GroupMessage:9")
check("冒号写法", subs[0]["uid"] == "123" and subs[0]["sessions"] == ["aiocqhttp:GroupMessage:9"], subs)

check("空配置", parse_subscriptions("") == [])
check("垃圾行跳过", parse_subscriptions("hello world") == [])

# ---------------------------------------------------------------- 勿扰时段
print("[2] 勿扰时段")
from datetime import datetime

check("夜间命中", in_quiet_hours("23:30-07:30", datetime(2026, 1, 1, 23, 45)))
check("凌晨命中", in_quiet_hours("23:30-07:30", datetime(2026, 1, 1, 6, 0)))
check("白天不命中", not in_quiet_hours("23:30-07:30", datetime(2026, 1, 1, 12, 0)))
check("留空不启用", not in_quiet_hours("", datetime(2026, 1, 1, 23, 45)))
check("非法值不崩", not in_quiet_hours("abc", datetime(2026, 1, 1, 23, 45)))

# ---------------------------------------------------------------- 模板
print("[3] 模板渲染")
check("正常占位", render_tpl("{nick} 发了 {kind}", nick="AB", kind="动态") == "AB 发了 动态")
check("缺占位符", render_tpl("a{missing}b") == "ab")
check("花括号不崩", render_tpl("a{b") == "a{b")

# ---------------------------------------------------------------- 动态解析
print("[4] 动态卡片解析")
draw = parse_dynamic(
    item(
        1,
        1700000000,
        major={"type": "MAJOR_TYPE_DRAW", "draw": {"items": [{"src": "//i0.hdslb.com/1.jpg"}, {"src": "//i0.hdslb.com/2.jpg"}]}},
    )
)
check("图文类型", draw["kind"] == "dynamic", draw)
check("图片归一化", draw["images"] == ["https://i0.hdslb.com/1.jpg", "https://i0.hdslb.com/2.jpg"], draw["images"])
check("链接", draw["link"] == "https://t.bilibili.com/1", draw["link"])

arch = parse_dynamic(
    item(
        2,
        1700000100,
        type_="DYNAMIC_TYPE_AV",
        major={"type": "MAJOR_TYPE_ARCHIVE", "archive": {"title": "视频标题", "cover": "//i0.hdslb.com/c.jpg", "jump_url": "https://www.bilibili.com/video/BV1xx", "desc": "简介"}},
    )
)
check("视频类型", arch["kind"] == "video", arch["kind"])
check("视频标题", arch["title"] == "视频标题")
check("视频封面", arch["cover"] == "https://i0.hdslb.com/c.jpg")
check("视频链接", arch["link"] == "https://www.bilibili.com/video/BV1xx")

art = parse_dynamic(item(3, 1700000200, type_="DYNAMIC_TYPE_ARTICLE", major={"type": "MAJOR_TYPE_ARTICLE", "article": {"title": "专栏标题", "covers": ["//i0.hdslb.com/a.jpg"], "jump_url": "https://www.bilibili.com/read/cv1"}}))
check("专栏类型", art["kind"] == "article" and art["title"] == "专栏标题", art)

audio = parse_dynamic(item(4, 1700000300, type_="DYNAMIC_TYPE_AUDIO", major={"type": "MAJOR_TYPE_AUDIO", "audio": {"title": "音频标题", "cover": "//i0.hdslb.com/au.jpg", "jump_url": "https://www.bilibili.com/audio/au1"}}))
check("音频类型", audio["kind"] == "audio" and audio["title"] == "音频标题", audio)

live_major = {
    "type": "MAJOR_TYPE_LIVE_RCMD",
    "live_rcmd": {"content": '{"live_play_info":{"title":"直播标题","cover":"//i0.hdslb.com/live.jpg","room_id":8888}}'},
}
live = parse_dynamic(item(5, 1700000400, type_="DYNAMIC_TYPE_LIVE_RCMD", major=live_major))
check("直播卡片", live["kind"] == "live" and live["link"] == "https://live.bilibili.com/8888", live)
check("直播标题", live["title"] == "直播标题", live["title"])

orig = item(99, 1700000000, name="原UP", text="原动态正文")
fwd = parse_dynamic(item(6, 1700000500, type_="DYNAMIC_TYPE_FORWARD", text="转发理由", orig=orig))
check("转发类型", fwd["kind"] == "forward", fwd["kind"])
check("转发原动态", fwd["orig"] and fwd["orig"]["nick"] == "原UP", fwd["orig"])
check("转发正文含来源", "原UP" in fwd["text"], fwd["text"])

pinned = parse_dynamic(item(7, 1600000000, pinned=True))
check("置顶识别", pinned["pinned"] is True)

opus = parse_dynamic(item(8, 1700000600, text="", major={"type": "MAJOR_TYPE_OPUS", "opus": {"title": "", "summary": {"text": "opus 正文"}, "pics": [{"url": "//i0.hdslb.com/o.jpg"}], "jump_url": "//www.bilibili.com/opus/123"}}))
check("opus 链接归一化", opus["link"] == "https://www.bilibili.com/opus/123", opus["link"])
check("opus 正文兜底", opus["text"] == "opus 正文", opus["text"])

opus_video = parse_dynamic(item(9, 1700000700, text="", major={"type": "MAJOR_TYPE_OPUS", "opus": {"title": "", "summary": {"text": ""}, "pics": [{"url": "//i0.hdslb.com/v.jpg"}], "jump_url": "//www.bilibili.com/video/BV1xx"}}))
check("opus 视频识别", opus_video["kind"] == "video", opus_video["kind"])
check("opus 封面兜底", opus_video["cover"] == "https://i0.hdslb.com/v.jpg", opus_video["cover"])

check("坏数据不崩", parse_dynamic({}) is None and parse_dynamic(None) is None)

# ---------------------------------------------------------------- 推送逻辑
print("[5] 动态去重与推送")


class FakeClient:
    def __init__(self):
        self.items = []
        self.live_status = 0
        self.online = 0
        self.room_id = 555
        self.last_room = None

    async def dynamics(self, uid, offset=""):
        return {"items": list(self.items), "offset": "", "has_more": False}

    async def live_master(self, uid):
        return {"room_id": self.room_id, "uname": "直播UP", "face": ""}

    async def live_room_id(self, uid):
        return self.room_id

    async def live_room(self, room_id):
        self.last_room = room_id
        return {
            "live_status": self.live_status,
            "online": self.online,
            "title": "直播标题",
            "cover": "//i0.hdslb.com/live.jpg",
        }


def build_monitor(conf):
    ctx = Context()
    mon = BiliMonitor(ctx, conf)
    mon._client = FakeClient()
    return mon, ctx


CONF = {
    "subscriptions": "123 | aiocqhttp:GroupMessage:1",
    "image_mode": False,
    "attach_images": False,
    "check_interval": 60,
    "max_new_per_round": 5,
    "first_run_hours": 24,
}


async def test_push():
    now = int(__import__("time").time())
    mon, ctx = build_monitor(dict(CONF))
    mon._client.items = [item(10, now), item(11, now + 60), item(12, now + 120)]
    state = mon._state.setdefault("123", {})
    await mon._check_dynamics({"uid": "123", "sessions": ["aiocqhttp:GroupMessage:1"]}, state)
    check("首轮推送 3 条", len(ctx.sent) == 3, len(ctx.sent))
    check("推送到配置会话", all(s[0] == "aiocqhttp:GroupMessage:1" for s in ctx.sent))
    check("按时间升序", "动态内容12" in str(ctx.sent[-1][1]))

    ctx.sent.clear()
    await mon._check_dynamics({"uid": "123", "sessions": ["aiocqhttp:GroupMessage:1"]}, state)
    check("重复轮次不重复推", len(ctx.sent) == 0, len(ctx.sent))

    mon._client.items = [item(13, now + 180)] + mon._client.items
    await mon._check_dynamics({"uid": "123", "sessions": ["aiocqhttp:GroupMessage:1"]}, state)
    check("新动态只推新的", len(ctx.sent) == 1, len(ctx.sent))

    # 无会话不推送
    ctx.sent.clear()
    await mon._check_dynamics({"uid": "999", "sessions": []}, mon._state.setdefault("999", {}))
    check("无会话不推送", len(ctx.sent) == 0)

    # 勿扰时段：不推送但状态要更新
    ctx.sent.clear()
    mon2, ctx2 = build_monitor({**CONF, "quiet_hours": "00:00-23:59"})
    mon2._client.items = [item(20, now)]
    st2 = mon2._state.setdefault("123", {})
    await mon2._check_dynamics({"uid": "123", "sessions": ["aiocqhttp:GroupMessage:1"]}, st2)
    check("勿扰不推送", len(ctx2.sent) == 0, len(ctx2.sent))
    check("勿扰仍记状态", "20" in st2.get("seen", []), st2.get("seen"))

    # 首次运行过滤过老动态
    ctx.sent.clear()
    mon3, ctx3 = build_monitor({**CONF, "first_run_hours": 1})
    mon3._client.items = [item(30, now - 86400), item(31, int(__import__("time").time()))]
    st3 = mon3._state.setdefault("123", {})
    await mon3._check_dynamics({"uid": "123", "sessions": ["aiocqhttp:GroupMessage:1"]}, st3)
    check("首次运行只推新的", len(ctx3.sent) == 1, len(ctx3.sent))

    # 置顶老动态跳过
    ctx.sent.clear()
    mon4, ctx4 = build_monitor(dict(CONF))
    mon4._client.items = [item(40, now - 86400 * 3, pinned=True), item(41, int(__import__("time").time()))]
    st4 = mon4._state.setdefault("123", {})
    await mon4._check_dynamics({"uid": "123", "sessions": ["aiocqhttp:GroupMessage:1"]}, st4)
    check("置顶老动态不推", len(ctx4.sent) == 1, len(ctx4.sent))

    # 模板开关
    ctx.sent.clear()
    mon5, ctx5 = build_monitor({**CONF, "notify_video": False})
    mon5._client.items = [item(50, now, type_="DYNAMIC_TYPE_AV", major={"type": "MAJOR_TYPE_ARCHIVE", "archive": {"title": "t", "cover": "", "jump_url": "u", "desc": "d", "bvid": "BV1"}})]
    st5 = mon5._state.setdefault("123", {})
    await mon5._check_dynamics({"uid": "123", "sessions": ["aiocqhttp:GroupMessage:1"]}, st5)
    check("类型开关生效", len(ctx5.sent) == 0, len(ctx5.sent))


async def test_live():
    mon, ctx = build_monitor(dict(CONF))
    state = mon._state.setdefault("123", {})
    sub = {"uid": "123", "sessions": ["aiocqhttp:GroupMessage:1"]}

    mon._client.live_status = 1
    mon._client.online = 1000
    await mon._check_live(sub, state)
    check("开播推送", len(ctx.sent) == 1 and "开播" in str(ctx.sent[0][1]), ctx.sent)
    check("开播记状态", state.get("live") == 1 and state.get("peak") == 1000, state)

    ctx.sent.clear()
    mon._client.online = 3000
    await mon._check_live(sub, state)
    check("二轮不重复推", len(ctx.sent) == 0)
    check("人气峰值更新", state.get("peak") == 3000, state.get("peak"))

    ctx.sent.clear()
    mon._client.live_status = 0
    state["live_start"] = int(__import__("time").time()) - 3720
    await mon._check_live(sub, state)
    check("下播推送", len(ctx.sent) == 1 and "下播" in str(ctx.sent[0][1]), ctx.sent)
    check("下播含时长", "1小时2分钟" in str(ctx.sent[0][1]), str(ctx.sent[0][1]))

    ctx.sent.clear()
    await mon._check_live(sub, state)
    check("下播只推一次", len(ctx.sent) == 0)

    check("时长格式", fmt_duration(3720) == "1小时2分钟" and fmt_duration(90) == "1分钟" and fmt_duration(5) == "5秒")


async def main():
    await test_push()
    print("[6] 直播状态机")
    await test_live()

    print("[7] 截图地址")
    mon, _ = build_monitor({**CONF, "image_service": "http://x/shot.php?url="})
    url = mon._screenshot_url("https://t.bilibili.com/1")
    check("截图 URL 编码", url == "http://x/shot.php?url=https%3A%2F%2Ft.bilibili.com%2F1", url)
    mon2, _ = build_monitor({**CONF, "image_service": ""})
    check("没配服务就不截图", mon2._screenshot_url("https://t.bilibili.com/1") == "")

    print("[8] 三列表配置")
    mon3, _ = build_monitor({
        "uids": ["208259", "946974", "4x6"],
        "push_targets": ["aiocqhttp:GroupMessage:1", "aiocqhttp:FriendMessage:2"],
        "room_overrides": ["946974:173551", "bad-line", "208259:0"],
        "strict_isolation": False,
    })
    subs = mon3._subscriptions()
    check("UID 列表解析", [s["uid"] for s in subs] == ["208259", "946974", "46"], [s["uid"] for s in subs])
    check("默认推送会补给未配置的", all(len(s["sessions"]) == 2 for s in subs), subs[0]["sessions"])
    check("房间号覆盖", subs[1]["room_id"] == 173551 and subs[0]["room_id"] == 0,
          [(s["uid"], s["room_id"]) for s in subs])

    mon4, _ = build_monitor({"subscriptions": "123 | aiocqhttp:GroupMessage:9"})
    check("旧配置兼容", [s["uid"] for s in mon4._subscriptions()] == ["123"], mon4._subscriptions())

    mon5, _ = build_monitor(dict(CONF))
    await mon5._check_live(
        {"uid": "123", "sessions": [], "room_id": 8888},
        mon5._state.setdefault("123", {}),
    )
    check("手填房间号优先", mon5._client.last_room == 8888, mon5._client.last_room)

    mon6, _ = build_monitor(dict(CONF))
    st6 = mon6._state.setdefault("123", {})
    await mon6._check_live({"uid": "123", "sessions": [], "room_id": 0}, st6)
    check("自动获取房间号", st6.get("room_id") == 555 and mon6._client.last_room == 555,
          (st6.get("room_id"), mon6._client.last_room))
    check("自动路径补昵称", st6.get("name") == "直播UP", st6.get("name"))

    print("[9] 每个 UP 推送隔离")
    mon7, _ = build_monitor({
        "uids": ["111", "222", "333"],
        "routes": [
            "111|aiocqhttp:GroupMessage:A",
            "111|aiocqhttp:GroupMessage:B",
            "222:aiocqhttp:GroupMessage:C",
        ],
        "push_targets": ["aiocqhttp:GroupMessage:DEFAULT"],
        "strict_isolation": True,
    })
    subs7 = {s["uid"]: s for s in mon7._subscriptions()}
    check("一个 UP 多个群", subs7["111"]["sessions"] == ["aiocqhttp:GroupMessage:A", "aiocqhttp:GroupMessage:B"],
          subs7["111"]["sessions"])
    check("冒号写法也认", subs7["222"]["sessions"] == ["aiocqhttp:GroupMessage:C"], subs7["222"]["sessions"])
    check("没配的 UP 不回落默认", subs7["333"]["sessions"] == [], subs7["333"]["sessions"])
    check("own_targets 标记", subs7["111"]["own_targets"] and not subs7["333"]["own_targets"],
          [subs7["111"]["own_targets"], subs7["333"]["own_targets"]])

    mon8, _ = build_monitor({
        "uids": ["111", "333"],
        "routes": ["111|aiocqhttp:GroupMessage:A"],
        "push_targets": ["aiocqhttp:GroupMessage:DEFAULT"],
        "strict_isolation": False,
    })
    subs8 = {s["uid"]: s for s in mon8._subscriptions()}
    check("关隔离才回落", subs8["333"]["sessions"] == ["aiocqhttp:GroupMessage:DEFAULT"], subs8["333"]["sessions"])
    check("专属群优先于默认", subs8["111"]["sessions"] == ["aiocqhttp:GroupMessage:A"], subs8["111"]["sessions"])

    # 真正跑一次推送，确认只发到本 UP 的群
    now9 = int(__import__("time").time())
    mon9, ctx9 = build_monitor({
        "uids": ["111", "222"],
        "routes": ["111|aiocqhttp:GroupMessage:A", "222|aiocqhttp:GroupMessage:C"],
        "strict_isolation": True,
        "image_mode": False,
        "attach_images": False,
    })
    mon9._client.items = [item(900, now9)]
    sub111 = next(s for s in mon9._subscriptions() if s["uid"] == "111")
    await mon9._check_dynamics(sub111, mon9._state.setdefault("111", {}))
    check("只推向自己的群", [s[0] for s in ctx9.sent] == ["aiocqhttp:GroupMessage:A"], [s[0] for s in ctx9.sent])

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
