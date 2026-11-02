"""B 站 UP 主监控 —— 移植自萌Q插件「bili监控助手」。

原插件是易语言写的原生 DLL（导出 IR_* / MQ_*，跑在萌Q MQNT 框架里），
配置全靠 GUI 写 INI。这里改成 AstrBot 的形态：

    * 订阅关系写在插件配置里（订阅列表 + 推送会话）
    * 聊天指令只读：/bili list、/bili info <UID>、/bili id
    * 后台协程轮询动态与直播，命中后主动推送到配置的会话
    * 动态 / 视频 / 专栏 / 音频 / 转发 / 直播 开播与下播
    * 可选图片模式（沿用原插件的第三方截图站，可关）
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import random
import re
import tempfile
import time
import traceback
from datetime import datetime
from typing import Any
from urllib.parse import quote

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.star import Context, Star

try:
    from astrbot.api import logger
except Exception:  # 兼容少数版本
    from astrbot import logger

try:
    from astrbot.api import AstrBotConfig
except Exception:  # 兼容少数版本
    from astrbot.core.config.astrbot_config import AstrBotConfig

try:  # 插件仪表盘用的 Web API helper（v4.9+）
    from astrbot.api.web import error_response, json_response
    from astrbot.api.web import request as web_request
except Exception:  # 老版本没有这套 API，仪表盘会自动降级
    error_response = None
    json_response = None
    web_request = None

from .bili_api import BiliClient, BiliError, BiliRiskError
from .parser import KIND_LABEL, parse_dynamic
from . import qr as qr_mod

try:  # 持久化目录，拿不到就退化到工作目录
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover
    def get_astrbot_data_path() -> str:
        return os.getcwd()


PLUGIN_DIR_NAME = "astrbot_plugin_bili_monitor"
STATE_VERSION = 2

# 默认推送模板，占位符：{nick} {uid} {text} {title} {link} {time} {kind} {duration} {peak}
DEFAULT_TPL: dict[str, str] = {
    "dynamic": "【动态提醒】{nick} 发布了新动态\n{text}\n{link}",
    "video": "【视频提醒】{nick} 发布了新视频\n{title}\n{link}",
    "article": "【专栏提醒】{nick} 发布了新专栏\n{title}\n{link}",
    "audio": "【音频提醒】{nick} 发布了新音频\n{title}\n{link}",
    "forward": "【转发提醒】{nick}\n{text}\n{link}",
    "live_on": "【开播提醒】{nick} 开播啦~\n{title}\n{link}",
    "live_off": "【下播提醒】{nick} 下播啦！\n直播时长：{duration}\n人气峰值：{peak}",
}

TPL_KEY = {
    "dynamic": "tpl_dynamic",
    "video": "tpl_video",
    "article": "tpl_article",
    "audio": "tpl_audio",
    "forward": "tpl_forward",
    "live_on": "tpl_live_on",
    "live_off": "tpl_live_off",
}


class _BlankDict(dict):
    def __missing__(self, key: str) -> str:  # 占位符没给值就当空串
        return ""


def render_tpl(tpl: str, **kwargs: Any) -> str:
    try:
        out = tpl.format_map(_BlankDict(**kwargs))
    except Exception:
        out = tpl
    return out.strip()


def in_quiet_hours(spec: str, now: datetime | None = None) -> bool:
    """判断当前是否在勿扰时段，spec 形如 "23:30-07:30"。"""
    spec = (spec or "").strip()
    if not spec or "-" not in spec:
        return False
    now = now or datetime.now()
    left, right = spec.split("-", 1)

    def to_minutes(x: str) -> int:
        x = x.strip().replace("：", ":")
        hh, _, mm = x.partition(":")
        return int(hh) * 60 + int(mm or 0)

    try:
        start, end = to_minutes(left), to_minutes(right)
    except Exception:
        return False
    if start == end:
        return False
    cur = now.hour * 60 + now.minute
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def parse_subscriptions(raw: Any, default_sessions: list[str] | None = None) -> list[dict]:
    """解析订阅配置。

    每行一条：  UID | 推送会话1,推送会话2 | 备注
    也接受 UID：会话1,会话2 的写法；# 开头是注释。
    没写会话的行会套用 default_sessions。
    """
    if isinstance(raw, list):
        raw = "\n".join(str(x) for x in raw)
    defaults = [str(s).strip() for s in (default_sessions or []) if str(s).strip()]
    text = str(raw or "")
    merged: dict[str, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"[|｜\t]", line)]
        head = parts[0]
        # 兼容 "UID：会话列表" 写法，只认数字开头的才算
        colon = re.match(r"^\s*(\d+)\s*[:：]\s*(.+)$", head)
        if colon:
            parts = [colon.group(1), colon.group(2)] + parts[1:]
        uid = re.sub(r"[^0-9]", "", parts[0])
        if not uid:
            logger.warning(f"[bili_monitor] 订阅行缺少 UID，已跳过：{line}")
            continue
        sessions: list[str] = []
        if len(parts) >= 2:
            sessions = [s for s in re.split(r"[,，;；\s]+", parts[1]) if s]
        note = parts[2] if len(parts) >= 3 else ""
        entry = merged.setdefault(uid, {"uid": uid, "sessions": [], "note": note})
        for s in sessions:
            if s not in entry["sessions"]:
                entry["sessions"].append(s)
        if note and not entry["note"]:
            entry["note"] = note

    for entry in merged.values():
        if not entry["sessions"] and defaults:
            entry["sessions"] = list(defaults)
    return list(merged.values())


def fmt_ts(ts: int) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return "-"


def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours}小时{minutes}分钟"
    if minutes:
        return f"{minutes}分钟"
    return f"{seconds}秒"


def image_component(url: str):
    """兼容不同小版本的 fromURL / from_url。"""
    for name in ("fromURL", "from_url"):
        fn = getattr(Image, name, None)
        if callable(fn):
            try:
                return fn(url)
            except Exception:
                continue
    return Image(file=url)


def _data_dir() -> str:
    base = get_astrbot_data_path()
    path = os.path.join(base, "plugin_data", PLUGIN_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _temp_dir() -> str:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

        return get_astrbot_temp_path()
    except Exception:
        return tempfile.gettempdir()


def image_from_bytes(data: bytes, name: str = "bili_qr.png"):
    """把 PNG 落盘再交给消息组件，兼容不同版本的文件构造方式。"""
    path = os.path.join(_temp_dir(), name)
    with open(path, "wb") as f:
        f.write(data)
    for attr in ("fromFileSystem", "from_file_system"):
        fn = getattr(Image, attr, None)
        if callable(fn):
            try:
                return fn(path)
            except Exception:
                continue
    return Image(file=path)


class BiliMonitor(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.conf = config
        self._client = BiliClient(
            self._cfg("cookie", ""),
            self._cfg("proxy", ""),
            int(self._cfg("risk_cooldown", 300)),
        )
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._state: dict[str, dict] = {}
        self._state_path = os.path.join(_data_dir(), "state.json")
        self._last_risk_warn = 0.0
        self._last_sub_warn = 0.0
        self._last_block_notice = 0.0
        self._pending_login: dict | None = None
        # 当前 Cookie 是谁的账号：{"logged_in", "name", "mid", "face", "ts"}
        self._account: dict = {"logged_in": False, "name": "", "mid": "", "face": "", "ts": 0}
        self._load_state()
        self._register_web_apis()

    # ------------------------------------------------------------------ 配置

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.conf.get(key) if self.conf is not None else None
        except Exception:
            value = None
        if value is None or value == "":
            return default
        return value

    def _bool_cfg(self, key: str, default: bool = True) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "false", "no", "off", "")
        return bool(value)

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        """配置项统一成字符串列表：既接受 list，也接受逗号/换行分隔的字符串。"""
        if isinstance(value, str):
            value = re.split(r"[,，;；\s]+", value)
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if str(v).strip()]

    def _uids(self) -> list[str]:
        """订阅的 UP 主 UID 列表。"""
        out: list[str] = []
        for item in self._as_list(self._cfg("uids", [])):
            uid = re.sub(r"[^0-9]", "", item)
            if uid and uid not in out:
                out.append(uid)
        return out

    def _room_overrides(self) -> dict[str, int]:
        """手填的直播间号，格式 "UID:房间号"，没写的就自动获取。"""
        out: dict[str, int] = {}
        for item in self._as_list(self._cfg("room_overrides", [])):
            m = re.match(r"^(\d+)\s*[:：=]\s*(\d+)$", item)
            if not m:
                continue
            uid, room = m.group(1), int(m.group(2))
            if room > 0:
                out[uid] = room
        return out

    def _targets(self) -> list[str]:
        """默认推送会话。严格隔离下它不会补给任何 UP 主，只作为批量配置的来源。"""
        return self._as_list(self._cfg("push_targets", []))

    def _routes(self) -> dict[str, list[str]]:
        """每个 UP 主自己的推送会话。

        配置里每行一条，两种写法都认：
            946974|aiocqhttp:GroupMessage:123456
            946974:aiocqhttp:GroupMessage:123456

        会话 ID 自己就带冒号，所以冒号写法只按**第一个**冒号切。
        """
        out: dict[str, list[str]] = {}
        for item in self._as_list(self._cfg("routes", [])):
            if "|" in item:
                uid_part, _, target = item.partition("|")
            else:
                m = re.match(r"^\s*(\d+)\s*[:：]\s*(.+)$", item)
                if not m:
                    continue
                uid_part, target = m.group(1), m.group(2)
            uid = re.sub(r"[^0-9]", "", uid_part)
            target = target.strip()
            if not uid or not target:
                continue
            bucket = out.setdefault(uid, [])
            if target not in bucket:
                bucket.append(target)
        return out

    def _subscriptions(self) -> list[dict]:
        """把配置拼成内部订阅结构。

        推送目标按 UP 主隔离：每个 UP 只发给自己配的群。
        没配专属群时，只有关闭了严格隔离才回落到默认推送。
        """
        uids = self._uids()
        targets = self._targets()
        strict = self._bool_cfg("strict_isolation", True)

        # 兼容旧版的 subscriptions 文本配置（升级上来的配置还能用）
        if not uids:
            legacy = parse_subscriptions(self._cfg("subscriptions", ""), targets)
            if legacy:
                return legacy
            return []

        overrides = self._room_overrides()
        routes = self._routes()

        subs: list[dict] = []
        for uid in uids:
            own = routes.get(uid) or []
            if own:
                sessions = list(own)
            elif strict:
                sessions = []
            else:
                sessions = list(targets)
            subs.append(
                {
                    "uid": uid,
                    "sessions": sessions,
                    "room_id": overrides.get(uid, 0),
                    "note": "",
                    "own_targets": bool(own),
                }
            )

        orphan = [uid for uid in overrides if uid not in uids]
        if orphan:
            logger.debug(f"[bili_monitor] 这些 UID 填了直播间号但没订阅：{', '.join(orphan)}")
        orphan_routes = [uid for uid in routes if uid not in uids]
        if orphan_routes:
            logger.debug(f"[bili_monitor] 这些 UID 配了推送群但没订阅：{', '.join(orphan_routes)}")
        return subs

    # ------------------------------------------------------------------ 状态

    def _load_state(self) -> None:
        try:
            with open(self._state_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("uids"), dict):
                self._state = data["uids"]
            elif isinstance(data, dict):
                self._state = {k: v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            self._state = {}
        except Exception as e:
            logger.warning(f"[bili_monitor] 读取状态文件失败，将重建：{e!r}")
            self._state = {}

    def _save_state(self) -> None:
        payload = {"version": STATE_VERSION, "uids": self._state}
        tmp = self._state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._state_path)
        except Exception as e:
            logger.warning(f"[bili_monitor] 保存状态失败：{e!r}")

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        if self._task and not self._task.done():
            return
        if not self._bool_cfg("enable", True):
            logger.info("[bili_monitor] enable=false，后台监控未启动")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info("[bili_monitor] 后台监控已启动")

    async def terminate(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        await self._client.close()
        self._save_state()

    # ------------------------------------------------------------------ 主循环

    async def _loop(self) -> None:
        await asyncio.sleep(8)  # 等 AstrBot 的各个平台适配器就绪
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("[bili_monitor] 轮询异常：\n" + traceback.format_exc())
            # 订阅多的时候自动拉长周期，否则一轮还没跑完就要开始下一轮
            sub_count = len(self._subscriptions())
            interval = max(30, int(self._cfg("check_interval", 90)), sub_count * 4)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        blocked = self._client.blocked_for()
        if blocked > 0:
            now = time.time()
            if now - self._last_block_notice > 600:
                self._last_block_notice = now
                logger.warning(
                    f"[bili_monitor] B 站接口冷却中（还剩 {int(blocked)} 秒），本轮跳过。"
                    "若持续 412，请在配置里填 cookie 或 proxy。"
                )
            return

        subs = self._subscriptions()
        if not subs:
            now = time.time()
            if now - self._last_sub_warn > 600:
                self._last_sub_warn = now
                logger.info("[bili_monitor] 还没有配置订阅（配置项 subscriptions），本轮跳过")
            return

        # 顺带确认一下当前 Cookie 是哪个账号（10 分钟缓存，不会频繁请求）
        await self._refresh_account()

        for idx, sub in enumerate(subs):
            if self._stop.is_set():
                return
            try:
                await self._check_uid(sub)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(f"[bili_monitor] 检查 {sub['uid']} 异常：\n{traceback.format_exc()}")
            if idx < len(subs) - 1:
                # B 站对匿名请求有软限流（返回 code=0 但 items 为空），拉开一点间隔
                await asyncio.sleep(2.0 + random.uniform(0, 1.0))
        self._save_state()

    async def _check_uid(self, sub: dict) -> None:
        uid = sub["uid"]
        state = self._state.setdefault(uid, {})
        if any(self._bool_cfg(f"notify_{k}", True) for k in ("dynamic", "video", "article", "audio", "forward")):
            await self._check_dynamics(sub, state)
        if self._bool_cfg("notify_live", True) and self._bool_cfg("live_monitor", True):
            await self._check_live(sub, state)
        state["last_check"] = int(time.time())

    # ------------------------------------------------------------------ 动态

    async def _fetch_dynamics(self, uid: str) -> list[dict]:
        """取一页动态（空结果的重试在 BiliClient 里做）。"""
        try:
            data = await self._client.dynamics(uid)
        except BiliRiskError as e:
            self._warn_risk(uid, e)
            return []
        except BiliError as e:
            logger.warning(f"[bili_monitor] 拉取 {uid} 动态失败：{e}")
            return []
        return [p for p in (parse_dynamic(i) for i in (data.get("items") or [])) if p]

    async def _check_dynamics(self, sub: dict, state: dict) -> None:
        uid = sub["uid"]
        items = await self._fetch_dynamics(uid)
        if not items:
            return

        state["name"] = state.get("name") or items[0]["nick"]
        state["face"] = state.get("face") or items[0]["face"]

        seen: list[str] = list(state.get("seen") or [])
        seen_set = set(seen)
        first_run = not seen_set
        now = int(time.time())
        fresh_hours = max(0, int(self._cfg("first_run_hours", 24)))

        pending: list[dict] = []
        for item in items:
            if item["id"] in seen_set:
                continue
            # 置顶的老动态不推（原插件也有这层过滤）
            if item["pinned"] and now - item["ts"] > 3600:
                seen.append(item["id"])
                seen_set.add(item["id"])
                continue
            # 首次运行只推最近 N 小时的，避免一上线刷屏；填 0 表示首次一条都不推
            if first_run:
                too_old = fresh_hours <= 0 or (item["ts"] and now - item["ts"] > fresh_hours * 3600)
                if too_old:
                    seen.append(item["id"])
                    seen_set.add(item["id"])
                    continue
            pending.append(item)

        pending.sort(key=lambda x: x["ts"])
        limit = max(1, int(self._cfg("max_new_per_round", 5)))
        skipped = pending[:-limit] if len(pending) > limit else []
        for item in skipped:
            if item["id"] not in seen_set:
                seen.append(item["id"])
                seen_set.add(item["id"])
        pending = pending[-limit:]

        quiet = self._in_quiet()
        has_sessions = bool([s for s in (sub.get("sessions") or []) if s])
        if not has_sessions and pending:
            logger.info(f"[bili_monitor] {uid} 有 {len(pending)} 条更新，但没有配置推送会话")
        for item in pending:
            if item["id"] not in seen_set:
                seen.append(item["id"])
                seen_set.add(item["id"])
            if not self._bool_cfg(f"notify_{item['kind']}", True):
                continue
            if quiet:
                logger.info(f"[bili_monitor] 勿扰时段，跳过推送 {item['kind']} {item['id']}")
                continue
            if not has_sessions:
                continue
            await self._push_dynamic(sub, item)

        state["seen"] = seen[-300:]
        state["last_ts"] = max((i["ts"] for i in items), default=state.get("last_ts", 0))

    async def _push_dynamic(self, sub: dict, item: dict) -> None:
        kind = item["kind"] if item["kind"] in DEFAULT_TPL else "dynamic"
        tpl = self._cfg(TPL_KEY.get(kind, "tpl_dynamic")) or DEFAULT_TPL[kind]
        text = (item["text"] or "").strip()
        if len(text) > 500:
            text = text[:500] + "…"
        title = (item["title"] or "").strip()
        if len(title) > 120:
            title = title[:120] + "…"
        # 视频/专栏/音频的模板只有标题，没有正文；opus 卡片里标题常为空，拿正文顶上
        if not title and item["kind"] in ("video", "article", "audio"):
            title = text[:60] + ("…" if len(text) > 60 else "")
            text = ""

        message = render_tpl(
            tpl,
            nick=item["nick"] or sub["uid"],
            uid=sub["uid"],
            text=text,
            title=title,
            link=item["link"],
            time=fmt_ts(item["ts"]),
            kind=KIND_LABEL.get(item["kind"], "动态"),
        )
        if not message:
            message = f"{item['nick']} 更新了{KIND_LABEL.get(item['kind'], '动态')}\n{item['link']}"

        images: list[str] = []
        if self._bool_cfg("image_mode", True) and item["kind"] in ("dynamic", "article", "forward"):
            shot = self._screenshot_url(item["link"])
            if shot:
                images = [shot]
        elif item["kind"] in ("video", "audio"):
            images = [item["cover"]] if item["cover"] else item["images"][:1]
        elif self._bool_cfg("attach_images", True):
            images = item["images"][:4]

        await self._push(
            sub,
            message,
            images=images,
            at_all=self._bool_cfg("at_all_dynamic", False),
        )

    # ------------------------------------------------------------------ 直播

    async def _check_live(self, sub: dict, state: dict) -> None:
        uid = sub["uid"]
        manual_room = int(sub.get("room_id") or 0)
        if manual_room:
            # 手填的直播间号优先，不覆盖、也不去接口查
            room_id = manual_room
            state["room_id"] = manual_room
        else:
            room_id = int(state.get("room_id") or 0)
            if not room_id:
                try:
                    room_id = await self._client.live_room_id(uid)
                except BiliRiskError as e:
                    self._warn_risk(uid, e)
                    return
                if room_id:
                    state["room_id"] = room_id
                else:
                    logger.debug(f"[bili_monitor] {uid} 没查到直播间号（可能没开通过直播间）")
        if not room_id:
            return

        # 昵称/头像只在缺失时补一次，别每轮都多打一个请求
        if not state.get("name"):
            try:
                master = await self._client.live_master(uid)
                state["name"] = master.get("uname") or ""
                state["face"] = state.get("face") or master.get("face") or ""
            except BiliError:
                pass

        try:
            info = await self._client.live_room(room_id)
        except BiliRiskError as e:
            self._warn_risk(uid, e)
            return
        except BiliError as e:
            if not manual_room:
                # 缓存的房间号可能失效（UP 主换了直播间），清掉下轮重新解析
                state["room_id"] = 0
            logger.debug(f"[bili_monitor] 取直播间 {room_id} 状态失败：{e}")
            return

        status = int(info.get("live_status") or 0)
        online = int(info.get("online") or 0)
        prev = state.get("live")
        title = (info.get("title") or "").strip()
        cover = info.get("system_cover") or info.get("user_cover") or info.get("cover") or ""
        if cover.startswith("//"):
            cover = "https:" + cover
        link = f"https://live.bilibili.com/{room_id}"
        nick = state.get("name") or uid
        quiet = self._in_quiet()

        if status == 1:
            if prev != 1:
                state["live_start"] = int(time.time())
                state["peak"] = online
                if not quiet:
                    tpl = self._cfg("tpl_live_on") or DEFAULT_TPL["live_on"]
                    message = render_tpl(
                        tpl, nick=nick, uid=uid, title=title, link=link,
                        time=fmt_ts(int(time.time())), kind="直播",
                    )
                    await self._push(
                        sub, message,
                        images=[cover] if cover else [],
                        at_all=self._bool_cfg("at_all_live", False),
                    )
            else:
                state["peak"] = max(int(state.get("peak") or 0), online)
        elif status == 0 and prev == 1:
            started = int(state.get("live_start") or 0)
            duration = int(time.time()) - started if started else 0
            peak = int(state.get("peak") or 0)
            if not quiet:
                tpl = self._cfg("tpl_live_off") or DEFAULT_TPL["live_off"]
                message = render_tpl(
                    tpl, nick=nick, uid=uid, title=title, link=link,
                    time=fmt_ts(int(time.time())), kind="直播",
                    duration=fmt_duration(duration), peak=f"{peak:,}",
                )
                await self._push(sub, message)
            state["live_start"] = 0
            state["peak"] = 0
        state["live"] = status

    # ------------------------------------------------------------------ 推送

    def _screenshot_url(self, link: str) -> str:
        service = str(self._cfg("image_service", "") or "").strip()
        if not service:
            return ""
        if "?" not in service:
            service += "?url="
        return service + quote(link, safe="")

    def _in_quiet(self) -> bool:
        return in_quiet_hours(str(self._cfg("quiet_hours", "") or ""))

    async def _push(
        self,
        sub: dict,
        text: str,
        images: list[str] | None = None,
        at_all: bool = False,
    ) -> None:
        images = [u for u in (images or []) if u][:4]
        sessions = [s for s in (sub.get("sessions") or []) if s]
        if not sessions:
            logger.info(f"[bili_monitor] {sub['uid']} 有更新，但没有配置推送会话")
            return

        sender = getattr(self.context, "send_message", None)
        if not callable(sender):
            logger.error(
                "[bili_monitor] 当前 AstrBot 没有 Context.send_message，无法主动推送；"
                "请确认 AstrBot 版本 >= 4.0"
            )
            return

        for umo in sessions:
            chain = MessageChain()
            if at_all and "GroupMessage" in umo:
                chain.chain.append(At(qq="all"))
            chain.chain.append(Plain(text))
            for url in images:
                chain.chain.append(image_component(url))
            try:
                await sender(umo, chain)
            except Exception as e:
                logger.error(f"[bili_monitor] 推送 {umo} 失败：{e!r}，退回纯文本")
                try:
                    fallback = MessageChain()
                    fallback.chain.append(Plain(text))
                    await sender(umo, fallback)
                except Exception as e2:
                    logger.error(f"[bili_monitor] 纯文本兜底也失败：{e2!r}")
            await asyncio.sleep(0.4)

    def _warn_risk(self, uid: str, err: Exception) -> None:
        now = time.time()
        if now - self._last_risk_warn < 1800:
            return
        self._last_risk_warn = now
        wait = int(self._client.blocked_for())
        tip = f"，{wait} 秒后自动重试" if wait else ""
        logger.warning(
            f"[bili_monitor] B 站接口风控（UID {uid}: {err}）{tip}。"
            "机房 IP 常见 412：在插件配置里填 cookie（至少 SESSDATA），"
            "或配置 proxy（如 http://user:pass@host:port）后重试。"
        )

    # ------------------------------------------------------------------ 指令（只读）

    def _split_args(self, event: AstrMessageEvent) -> list[str]:
        """定位指令词后面的参数。

        不同唤醒前缀下 message_str 的形态不一样，所以不按位置切，
        而是找到 "bili" 这个词，取它后面的全部。
        """
        tokens = (getattr(event, "message_str", "") or "").strip().split()
        for idx, token in enumerate(tokens):
            if token.lstrip("/").lower() in ("bili", "b站监控", "哔哩监控"):
                return tokens[idx + 1:]
        return tokens

    def _help_text(self) -> str:
        return (
            "B站监控助手（只读指令）\n"
            "/bili list — 查看已订阅的 UP 主\n"
            "/bili info <UID> — 查询 UP 主资料、最新动态、直播状态\n"
            "/bili login — 扫码登录 B 站，自动保存 Cookie\n"
            "/bili id — 查看当前会话 ID（填推送群聊时用）\n\n"
            "订阅关系在插件配置里改，分四个列表：\n"
            "  1. 订阅的 UP 主 UID\n"
            "  2. 直播间号（可留空自动获取，格式 UID:房间号）\n"
            "  3. 默认推送（只在关闭严格隔离时生效）\n"
            "  4. 每个 UP 主的推送群（UID|会话ID，可多行）"
        )

    def _list_text(self) -> str:
        subs = self._subscriptions()
        if not subs:
            return (
                "还没有配置订阅。\n"
                "请到插件配置里填「订阅的 UP 主 UID」和「推送的群聊」，"
                "直播间号可以留空自动获取。"
            )
        lines = [f"已订阅 {len(subs)} 个 UP 主："]
        for idx, sub in enumerate(subs, 1):
            st = self._state.get(sub["uid"]) or {}
            name = st.get("name") or "（未获取）"
            live = "直播中" if int(st.get("live") or 0) == 1 else ""
            checked = fmt_ts(int(st.get("last_check") or 0))
            room = int(sub.get("room_id") or st.get("room_id") or 0)
            room_text = f"房间 {room}" + ("（手填）" if sub.get("room_id") else "") if room else "房间未获取"
            if sub["sessions"]:
                push_text = f"推送 {len(sub['sessions'])} 个群"
                push_text += "（专属）" if sub.get("own_targets") else "（默认）"
            else:
                push_text = "无推送目标"
            lines.append(
                f"{idx}. {name}（UID {sub['uid']}）"
                f" {room_text} · {push_text} · 上次检查 {checked}"
                + (f" · {live}" if live else "")
            )
            if not sub["sessions"]:
                lines.append("   ⚠️ 这个 UP 主没有推送群，只会监控不会推送（仪表盘里单独配）")
        return "\n".join(lines)

    async def _info_text(self, uid: str) -> str:
        lines: list[str] = []
        try:
            info = await self._client.user_info(uid)
            name = info.get("name") or ""
            lines.append(f"【{name or uid}】UID {uid}")
            if info.get("sign"):
                lines.append(f"签名：{str(info['sign'])[:80]}")
        except BiliRiskError as e:
            return f"查询 {uid} 被风控了（{e}）。请在插件配置里填 Cookie 后重试。"
        except BiliError as e:
            return f"查询 {uid} 失败：{e}"

        try:
            fans = await self._client.follower_count(uid)
            lines.append(f"粉丝数：{fans:,}")
        except BiliError:
            pass

        try:
            data = await self._client.dynamics(uid)
            parsed = [p for p in (parse_dynamic(i) for i in (data.get("items") or [])) if p]
            latest = max(parsed, key=lambda x: x["ts"]) if parsed else None
            if latest:
                text = (latest["text"] or latest["title"] or "").replace("\n", " ")[:80]
                lines.append(
                    f"最新{KIND_LABEL.get(latest['kind'], '动态')}：{fmt_ts(latest['ts'])} {text}"
                )
                lines.append(latest["link"])
            else:
                lines.append("最近没有动态")
        except BiliRiskError as e:
            lines.append(f"动态查询被风控（{e}）")
        except BiliError as e:
            lines.append(f"动态查询失败：{e}")

        try:
            room_id = await self._client.live_room_id(uid)
            if room_id:
                room = await self._client.live_room(room_id)
                status = int(room.get("live_status") or 0)
                if status == 1:
                    lines.append(f"直播间：直播中 · {room.get('title') or ''}")
                else:
                    lines.append("直播间：未开播")
                lines.append(f"https://live.bilibili.com/{room_id}")
        except BiliError:
            pass

        return "\n".join(lines)

    @filter.command("bili")
    async def bili(self, event: AstrMessageEvent):
        if not self._bool_cfg("enable_commands", True):
            return
        # 兜底：万一宿主没有调用 initialize()，靠第一条指令把循环拉起来
        if self._bool_cfg("enable", True) and (self._task is None or self._task.done()):
            try:
                await self.initialize()
            except Exception as e:
                logger.warning(f"[bili_monitor] 启动后台任务失败：{e!r}")

        args = self._split_args(event)
        sub = (args[0].lower() if args else "help")

        if sub in ("help", "帮助", "?", "-h", "--help"):
            yield event.plain_result(self._help_text())
            return

        if sub in ("list", "列表", "ls"):
            yield event.plain_result(self._list_text())
            return

        if sub in ("id", "会话", "umo"):
            try:
                umo = event.unified_msg_origin
            except Exception:
                umo = ""
            yield event.plain_result(f"当前会话 ID：\n{umo}\n\n把它填到订阅那一行的推送会话里即可。")
            return

        if sub in ("login", "登录", "扫码"):
            if not self._can_login(event):
                yield event.plain_result(
                    "只有管理员可以触发扫码登录（配置项 admin_only_login）。\n"
                    "因为扫码会把机器人绑定到扫码人的 B 站账号上，默认只让管理员操作。"
                )
                return
            try:
                data = await self._qr_start()
            except BiliError as e:
                yield event.plain_result(f"生成二维码失败：{e}")
                return
            png = qr_mod.make_png(data["url"])
            components: list[Any] = [Plain(
                "请用「你希望机器人使用的」B 站账号扫码（建议小号，别用主号）。\n"
                "谁扫这个码，机器人以后就以谁的身份读取 B 站公开接口。\n"
                "扫码仅用于获取 Cookie，不会发布、点赞、关注或修改任何内容；"
                "随时可在 B 站「登录设备管理」里移除。3 分钟内有效。"
            )]
            if png:
                components.append(image_from_bytes(png))
            else:
                components.append(
                    Plain(f"二维码组件不可用（缺 qrcode 依赖），请手动打开链接：\n{data['url']}")
                )
            try:
                umo = event.unified_msg_origin
            except Exception:
                umo = ""
            if umo:
                asyncio.create_task(self._login_watch(umo, data["key"]))
            yield event.chain_result(components)
            return

        if sub in ("info", "查询", "查"):
            if len(args) < 2:
                yield event.plain_result("用法：/bili info <UID>")
                return
            uid = re.sub(r"[^0-9]", "", args[1])
            if not uid:
                yield event.plain_result("UID 只能是数字，比如 /bili info 208259")
                return
            text = await self._info_text(uid)
            yield event.plain_result(text)
            return

        yield event.plain_result(self._help_text())

    # ------------------------------------------------------------------ 扫码登录

    async def _refresh_account(self, force: bool = False) -> dict:
        """查一下当前 Cookie 是**谁的**账号，结果缓存 10 分钟。"""
        now = time.time()
        if not force and self._account.get("ts") and now - self._account["ts"] < 600:
            return self._account
        if not str(self._cfg("cookie", "") or "").strip():
            self._account = {
                "logged_in": False, "name": "", "mid": "", "face": "", "ts": int(now),
            }
            return self._account
        try:
            info = await self._client.nav_info()
            self._account = {
                "logged_in": bool(info.get("is_login")),
                "name": info.get("name") or "",
                "mid": str(info.get("mid") or ""),
                "face": info.get("face") or "",
                "ts": int(now),
            }
        except (BiliError, BiliRiskError) as e:
            logger.debug(f"[bili_monitor] 查询登录账号失败：{e}")
            self._account = {**self._account, "ts": int(now)}
        return self._account

    def _can_login(self, event: AstrMessageEvent) -> bool:
        """扫码登录会把机器人绑到扫码人的账号上，默认只允许管理员触发。"""
        if not self._bool_cfg("admin_only_login", True):
            return True
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return True  # 平台没有管理员概念时放行，否则谁也登不了

    async def _qr_start(self) -> dict:
        """生成登录二维码，返回 {key, url, image}。"""
        data = await self._client.qr_login_generate()
        key = data.get("key") or ""
        url = data.get("url") or ""
        if not key or not url:
            raise BiliError("B 站没有返回二维码（接口可能改版了）")
        self._pending_login = {"key": key, "url": url, "created": int(time.time())}
        image = qr_mod.data_uri(url)
        return {"key": key, "url": url, "image": image, "qr_available": bool(image)}

    async def _qr_poll(self, key: str) -> dict:
        result = await self._client.qr_login_poll(key)
        raw_status = result.get("status")
        status = int(raw_status) if raw_status is not None else -1
        out = {"status": status, "message": result.get("message") or "", "cookie_set": False}
        if status == 0:
            cookies = result.get("cookies") or {}
            if cookies:
                await self._save_login_cookies(cookies)
                out["cookie_set"] = True
                out["cookie_names"] = sorted(cookies)
                out["account"] = {
                    "name": self._account.get("name") or "",
                    "mid": self._account.get("mid") or "",
                }
            self._pending_login = None
        elif status == 86038:
            self._pending_login = None
        return out

    async def _save_login_cookies(self, cookies: dict) -> None:
        order = ["SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid"]
        parts = [f"{name}={cookies[name]}" for name in order if name in cookies]
        for name, value in cookies.items():
            if name not in order:
                parts.append(f"{name}={value}")
        if not parts:
            return
        self.conf["cookie"] = "; ".join(parts)
        await self._persist_config()
        await self._client.close()
        self._client = BiliClient(
            self._cfg("cookie", ""),
            self._cfg("proxy", ""),
            int(self._cfg("risk_cooldown", 300)),
        )
        self._account = {"logged_in": False, "name": "", "mid": "", "face": "", "ts": 0}
        await self._refresh_account(force=True)
        who = self._account.get("name") or "未知账号"
        mid = self._account.get("mid") or "?"
        logger.info(f"[bili_monitor] 扫码登录成功，已绑定账号 {who}（UID {mid}），Cookie 已保存")

    async def api_login_qr(self):
        try:
            return json_response(await self._qr_start())
        except BiliError as e:
            return error_response(f"生成二维码失败：{e}")

    async def api_login_poll(self):
        payload = await web_request.json(default={}) if web_request else {}
        key = str((payload or {}).get("key") or "").strip()
        if not key and self._pending_login:
            key = str(self._pending_login.get("key") or "")
        if not key:
            return error_response("缺少 qrcode_key，请重新生成二维码")
        try:
            out = await self._qr_poll(key)
        except BiliError as e:
            return error_response(f"查询扫码状态失败：{e}")
        if out.get("cookie_set"):
            out["overview"] = self._overview_payload()
        return json_response(out)

    async def _login_watch(self, umo: str, key: str) -> None:
        """聊天里发起扫码后，后台盯 3 分钟。"""
        deadline = time.time() + 180
        while time.time() < deadline:
            await asyncio.sleep(2)
            try:
                out = await self._qr_poll(key)
            except Exception:
                continue
            status = out.get("status")
            if status == 0:
                account = out.get("account") or {}
                who = account.get("name") or "未知账号"
                mid = account.get("mid") or "?"
                await self._send_text(
                    umo,
                    f"B 站登录成功，已绑定账号 {who}（UID {mid}），Cookie 已保存 ✅\n"
                    "（仅用于读取公开接口，不会对你的账号做任何写操作）",
                )
                return
            if status == 86038:
                await self._send_text(umo, "二维码已过期，请重新发送 /bili login")
                return

    async def _send_text(self, umo: str, text: str) -> None:
        sender = getattr(self.context, "send_message", None)
        if not callable(sender):
            return
        try:
            chain = MessageChain()
            chain.chain.append(Plain(text))
            await sender(umo, chain)
        except Exception as e:
            logger.warning(f"[bili_monitor] 发送登录结果失败：{e!r}")

    # ------------------------------------------------------------------ 仪表盘 Web API
    # 页面在 views/dashboard/index.html，通过 window.AstrBotPluginView 调这些接口。

    def _register_web_apis(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register) or json_response is None:
            logger.info(
                "[bili_monitor] 当前 AstrBot 不支持插件页面 API，仪表盘不可用"
                "（配置仍然可以在插件配置里改）"
            )
            return
        routes = [
            ("overview", self.api_overview, ["GET"], "B站监控：概览与订阅状态"),
            ("config", self.api_save_config, ["POST"], "B站监控：保存订阅配置"),
            ("check", self.api_check_now, ["POST"], "B站监控：立即检查一次"),
            ("test-push", self.api_test_push, ["POST"], "B站监控：发送测试推送"),
            ("login/qr", self.api_login_qr, ["POST"], "B站监控：生成登录二维码"),
            ("login/poll", self.api_login_poll, ["POST"], "B站监控：查询扫码状态"),
        ]
        for suffix, handler, methods, desc in routes:
            try:
                register(f"/{PLUGIN_DIR_NAME}/{suffix}", handler, methods, desc)
            except Exception as e:
                logger.warning(f"[bili_monitor] 注册仪表盘接口 /{suffix} 失败：{e!r}")

    def _overview_payload(self) -> dict:
        subs = self._subscriptions()
        items = []
        for sub in subs:
            st = self._state.get(sub["uid"]) or {}
            manual_room = int(sub.get("room_id") or 0)
            items.append(
                {
                    "uid": sub["uid"],
                    "name": st.get("name") or "",
                    "face": st.get("face") or "",
                    "room_id": manual_room or int(st.get("room_id") or 0),
                    "room_manual": bool(manual_room),
                    "live": int(st.get("live") or 0),
                    "live_start": int(st.get("live_start") or 0),
                    "peak": int(st.get("peak") or 0),
                    "last_check": int(st.get("last_check") or 0),
                    "seen": len(st.get("seen") or []),
                    "sessions": list(sub.get("sessions") or []),
                    "own_targets": bool(sub.get("own_targets")),
                }
            )
        return {
            "running": bool(self._task and not self._task.done()),
            "config": {
                "enable": self._bool_cfg("enable", True),
                "check_interval": int(self._cfg("check_interval", 90)),
                "quiet_hours": str(self._cfg("quiet_hours", "") or ""),
                "image_mode": self._bool_cfg("image_mode", True),
                "notify_live": self._bool_cfg("notify_live", True),
                "notify_dynamic": self._bool_cfg("notify_dynamic", True),
                "cookie_set": bool(str(self._cfg("cookie", "") or "").strip()),
                "proxy_set": bool(str(self._cfg("proxy", "") or "").strip()),
                "admin_only_login": self._bool_cfg("admin_only_login", True),
                "account": {
                    "logged_in": bool(self._account.get("logged_in")),
                    "name": self._account.get("name") or "",
                    "mid": self._account.get("mid") or "",
                    "face": self._account.get("face") or "",
                },
                "uids": [sub["uid"] for sub in subs],
                "room_overrides": [
                    f"{sub['uid']}:{int(sub['room_id'])}"
                    for sub in subs
                    if int(sub.get("room_id") or 0)
                ],
                "push_targets": self._targets(),
                "strict_isolation": self._bool_cfg("strict_isolation", True),
                "routes": [
                    f"{uid}|{target}"
                    for uid, targets in self._routes().items()
                    for target in targets
                ],
            },
            "items": items,
            "updated_at": int(time.time()),
        }

    async def api_overview(self):
        await self._refresh_account()
        return json_response(self._overview_payload())

    async def api_save_config(self):
        payload = await web_request.json(default={}) if web_request else {}
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        # UID 列表
        if "uids" in payload:
            uids = []
            for raw in payload.get("uids") or []:
                uid = re.sub(r"[^0-9]", "", str(raw))
                if uid and uid not in uids:
                    uids.append(uid)
            self.conf["uids"] = uids

        # 推送会话
        if "push_targets" in payload:
            targets = []
            for raw in payload.get("push_targets") or []:
                target = str(raw).strip()
                if target and target not in targets:
                    targets.append(target)
            self.conf["push_targets"] = targets

        # 每个 UP 主自己的推送群（格式 946974|aiocqhttp:GroupMessage:123）
        if "routes" in payload:
            routes: list[str] = []
            for raw in payload.get("routes") or []:
                item = str(raw).strip()
                if "|" in item:
                    uid_part, _, target = item.partition("|")
                else:
                    m = re.match(r"^\s*(\d+)\s*[:：]\s*(.+)$", item)
                    if not m:
                        continue
                    uid_part, target = m.group(1), m.group(2)
                uid = re.sub(r"[^0-9]", "", uid_part)
                target = target.strip()
                if not uid or not target:
                    continue
                line = f"{uid}|{target}"
                if line not in routes:
                    routes.append(line)
            self.conf["routes"] = routes

        if "strict_isolation" in payload:
            self.conf["strict_isolation"] = bool(payload["strict_isolation"])

        # 直播间号覆盖
        if "room_overrides" in payload:
            rooms = []
            for raw in payload.get("room_overrides") or []:
                item = str(raw).strip()
                m = re.match(r"^(\d+)\s*[:：=]\s*(\d+)$", item)
                if m and int(m.group(2)) > 0:
                    rooms.append(f"{m.group(1)}:{int(m.group(2))}")
            self.conf["room_overrides"] = rooms

        # 开关与数值
        for key in ("enable", "image_mode", "notify_live", "notify_dynamic"):
            if key in payload:
                self.conf[key] = bool(payload[key])
        if "check_interval" in payload:
            try:
                self.conf["check_interval"] = max(30, int(payload["check_interval"]))
            except (TypeError, ValueError):
                return error_response("check_interval 必须是整数")
        if "quiet_hours" in payload:
            quiet = str(payload["quiet_hours"] or "").strip()
            if quiet and not re.match(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$", quiet):
                return error_response("勿扰时段格式应为 23:30-07:30")
            self.conf["quiet_hours"] = quiet
        # Cookie / 代理只写入不读出
        rebuild = False
        cookie = str(payload.get("cookie") or "").strip()
        if cookie:
            self.conf["cookie"] = cookie
            rebuild = True
        proxy = str(payload.get("proxy") or "").strip()
        if proxy:
            self.conf["proxy"] = proxy
            rebuild = True
        if rebuild:
            await self._client.close()
            self._client = BiliClient(
                self._cfg("cookie", ""),
                self._cfg("proxy", ""),
                int(self._cfg("risk_cooldown", 300)),
            )
            logger.info("[bili_monitor] Cookie/代理已更新，接口客户端已重建")

        await self._persist_config()
        # 启停开关变化时重启轮询协程
        if "enable" in payload:
            await self._stop_loop()
            if self._bool_cfg("enable", True):
                await self.initialize()
        logger.info("[bili_monitor] 仪表盘保存了配置")
        return json_response({"ok": True, "overview": self._overview_payload()})

    async def _stop_loop(self) -> None:
        """只停轮询协程，不动客户端和状态（保存配置后重启用）。"""
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def api_check_now(self):
        payload = await web_request.json(default={}) if web_request else {}
        only = ""
        if isinstance(payload, dict):
            only = re.sub(r"[^0-9]", "", str(payload.get("uid") or ""))
        subs = self._subscriptions()
        if only:
            subs = [s for s in subs if s["uid"] == only]
        if not subs:
            return error_response("没有可检查的订阅（先添加 UP 主 UID）")

        results = []
        for sub in subs:
            uid = sub["uid"]
            before = len((self._state.get(uid) or {}).get("seen") or [])
            try:
                await self._check_uid(sub)
                after = len((self._state.get(uid) or {}).get("seen") or [])
                results.append({"uid": uid, "ok": True, "new": max(0, after - before)})
            except Exception as e:
                results.append({"uid": uid, "ok": False, "error": str(e)})
        self._save_state()
        return json_response({"ok": True, "results": results, "overview": self._overview_payload()})

    async def api_test_push(self):
        payload = await web_request.json(default={}) if web_request else {}
        text = str((payload or {}).get("text") or "").strip()
        if not text:
            text = "【B站监控助手】这是一条测试消息，收到说明推送链路是通的。"
        targets = list(self._targets())
        for group in self._routes().values():
            for target in group:
                if target not in targets:
                    targets.append(target)
        if not targets:
            return error_response("还没有配置推送群聊")
        await self._push({"uid": "test", "sessions": targets}, text)
        return json_response({"ok": True, "sent": len(targets)})

    async def _persist_config(self) -> None:
        saver = getattr(self.conf, "save_config", None)
        if callable(saver):
            try:
                result = saver()
                if inspect.isawaitable(result):
                    await result
                return
            except Exception as e:
                logger.warning(f"[bili_monitor] save_config 失败，改为直接写文件：{e!r}")
        try:
            path = os.path.join(
                get_astrbot_data_path(), "config", f"{PLUGIN_DIR_NAME}_config.json"
            )
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(self.conf or {}), f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"[bili_monitor] 写配置文件失败：{e!r}")
