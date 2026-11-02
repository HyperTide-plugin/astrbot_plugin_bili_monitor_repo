"""B 站接口封装。

只依赖 aiohttp / hashlib，不引入 bilibili-api 之类的重依赖。
覆盖原插件用到的接口：

    x/relation/stat                     粉丝数
    x/space/wbi/acc/info                UP 主信息（需要 WBI 签名）
    x/polymer/web-dynamic/v1/feed/space 动态列表（需要 WBI 签名）
    x/polymer/web-dynamic/v1/detail     动态详情
    live_user/v1/Master/info            直播间号
    room/v1/Room/get_info               直播状态 / 人气
    x/frontend/finger/spi               匿名 buvid3 / buvid4
    x/web-interface/nav                 WBI 密钥
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
import uuid
from typing import Any, Optional
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

import aiohttp

from astrbot import logger

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# WBI 签名用的字符重排表（B 站前端那份，固定不变）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

# 风控用的浏览器指纹占位（照抄网页版，能降低 -352 概率）
DM_IMG_STR = "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ"
DM_COVER_IMG_STR = (
    "QU5HTEUgKEludGVsLCBJbnRlbChSKSBVSEQgR3JhcGhpY3MgNjMwICgweDAwMDAzRTlCKSBEaXJlY3QzRDExIHZzXzVfMCBwc181XzApLCBvciBzaW1pbGFy"
)

# 这些错误码基本都是风控/鉴权，值得提示用户填 Cookie
RISK_CODES = {-352, -412, -509, -799, -101}


class BiliError(Exception):
    """接口调用失败。"""


class BiliRiskError(BiliError):
    """被风控或需要登录。"""


class BiliClient:
    def __init__(self, cookie: str = "", proxy: str = "", cooldown: int = 300):
        self.user_cookie = (cookie or "").strip()
        self.proxy = (proxy or "").strip()
        self.cooldown = max(30, int(cooldown))
        self._session: Optional[aiohttp.ClientSession] = None
        self._buvid: dict[str, str] = {}
        self._wbi: Optional[tuple[str, str, float]] = None
        self._lock = asyncio.Lock()
        self._warmed = False
        # 被 WAF 拦下后进入冷却，别在冷却期里继续打接口（越打越封）
        self._blocked_until = 0.0
        self._block_reason = ""

    # ------------------------------------------------------------------ 基础设施

    def blocked_for(self) -> float:
        """还要冷却多少秒；0 表示正常。"""
        return max(0.0, self._blocked_until - time.time())

    def _block(self, reason: str) -> None:
        self._blocked_until = time.time() + self.cooldown
        self._block_reason = reason
        logger.warning(
            f"[bili_monitor] B 站返回风控（{reason}），暂停 {self.cooldown} 秒后再试。"
            "机房/云服务器 IP 常见此问题，配 cookie 或 proxy 可解。"
        )

    async def _session_or_new(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                headers={
                    "User-Agent": UA,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Referer": "https://space.bilibili.com/",
                    "Origin": "https://space.bilibili.com",
                    # 下面这些是"像个真浏览器"的指纹头，机房 IP 少了它们更容易 412
                    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-site",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _cookie_header(self) -> str:
        jar: dict[str, str] = {}
        if self._buvid.get("buvid3"):
            jar["buvid3"] = self._buvid["buvid3"]
        if self._buvid.get("buvid4"):
            jar["buvid4"] = self._buvid["buvid4"]
        # 网页端每次访问都会带的几个 cookie，缺了容易被当成脚本
        jar.setdefault("b_nut", str(int(time.time())))
        jar.setdefault("_uuid", self._uuid())
        jar.setdefault("b_lsid", uuid.uuid4().hex[:8].upper() + "_" + uuid.uuid4().hex[:8].upper())
        if self.user_cookie:
            for part in self.user_cookie.replace("\n", ";").split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k:
                        jar[k] = v
        return "; ".join(f"{k}={v}" for k, v in jar.items())

    def _uuid(self) -> str:
        """_uuid 的格式是 8-4-4-4-12 的十六进制 + 5 位数字后缀。"""
        if not hasattr(self, "_uuid_cache"):
            self._uuid_cache = (
                f"{uuid.uuid4().hex[:8]}-{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:4]}-"
                f"{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:12]}{str(int(time.time() * 1000))[-5:]}"
            )
        return self._uuid_cache

    async def _ensure_buvid(self) -> None:
        if not self._buvid.get("buvid3"):
            async with self._lock:
                if not self._buvid.get("buvid3"):
                    try:
                        js = await self._get_json(
                            "https://api.bilibili.com/x/frontend/finger/spi", need_cookie=False
                        )
                        data = js.get("data") or {}
                        if data.get("b_3"):
                            self._buvid = {"buvid3": data["b_3"], "buvid4": data.get("b_4", "")}
                    except Exception as e:  # 拿不到也能跑，只是更容易被风控
                        logger.debug(f"[bili_monitor] 获取 buvid 失败: {e!r}")
        await self._warmup()

    async def _warmup(self) -> None:
        """先用一个公开接口把新指纹"激活"。

        实测：全新的 buvid3 直接打动态接口，经常返回 code=0 但列表为空；
        先打一次 web-interface/view，紧接着的动态请求就正常了。
        """
        if self._warmed:
            return
        self._warmed = True
        try:
            await self._get_json(
                "https://api.bilibili.com/x/web-interface/view",
                {"bvid": "BV1GJ411x7h7"},
                referer="https://www.bilibili.com/",
            )
            await asyncio.sleep(0.6)
        except Exception as e:
            logger.debug(f"[bili_monitor] 指纹预热失败（不影响后续）: {e!r}")

    def _proxy_args(self) -> dict:
        """把 http://user:pass@host:port 拆成 aiohttp 需要的 proxy + proxy_auth。"""
        if not self.proxy:
            return {}
        parts = urlsplit(self.proxy)
        if not parts.username:
            return {"proxy": self.proxy}
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        auth = aiohttp.BasicAuth(unquote(parts.username), unquote(parts.password or ""))
        return {"proxy": f"{parts.scheme}://{host}{port}", "proxy_auth": auth}

    async def _get_json(
        self,
        url: str,
        params: Any = None,
        *,
        need_cookie: bool = True,
        referer: str = "",
    ) -> dict:
        sess = await self._session_or_new()
        headers = {}
        if referer:
            headers["Referer"] = referer
        if need_cookie:
            ck = self._cookie_header()
            if ck:
                headers["Cookie"] = ck
        async with sess.get(url, params=params, headers=headers, **self._proxy_args()) as resp:
            text = await resp.text()
            if resp.status != 200:
                # 412 是 WAF 直接拦（常见于机房 IP），429/403 是限流
                if resp.status in (412, 429, 403):
                    try:
                        body = json.loads(text)
                        msg = body.get("message") or text[:80]
                    except Exception:
                        msg = text[:80]
                    reason = f"HTTP {resp.status} {msg}"
                    self._block(reason)
                    raise BiliRiskError(reason)
                raise BiliError(f"HTTP {resp.status}: {text[:150]}")
            try:
                return json.loads(text)
            except Exception:
                raise BiliError(f"响应不是 JSON: {text[:150]}")

    async def _api(
        self,
        url: str,
        params: Any = None,
        *,
        sign: bool = False,
        retry: int = 2,
        need_cookie: bool = True,
        referer: str = "",
    ) -> Any:
        """统一请求：自动补 buvid / WBI 签名，风控与网络错误重试。

        referer 很重要：不带 https://space.bilibili.com/<uid> 时，WBI 接口
        大概率直接 412 / -352。这是实测出来的，不是玄学。
        """
        blocked = self.blocked_for()
        if blocked > 0:
            raise BiliRiskError(f"冷却中，还剩 {int(blocked)} 秒（{self._block_reason}）")

        last: Exception = BiliError("未知错误")
        for attempt in range(retry + 1):
            try:
                await self._ensure_buvid()
                p = dict(params or {})
                if sign:
                    p = await self._sign(p)
                js = await self._get_json(
                    url, p, need_cookie=need_cookie, referer=referer
                )
                code = int(js.get("code") or 0)
                if code == 0:
                    return js.get("data")
                msg = js.get("message") or ""
                if code in RISK_CODES:
                    raise BiliRiskError(f"code={code} {msg}")
                raise BiliError(f"code={code} {msg}")
            except BiliRiskError:
                # 风控不重试：越打越封，交给冷却
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, BiliError) as e:
                last = e
                if attempt >= retry:
                    break
                await asyncio.sleep(1.2 * (attempt + 1))
        raise last

    # ------------------------------------------------------------------ WBI

    async def _wbi_keys(self) -> tuple[str, str]:
        now = time.time()
        if self._wbi and now - self._wbi[2] < 3600:
            return self._wbi[0], self._wbi[1]
        # 未登录时 nav 返回 code=-101，但 data.wbi_img 依然有效
        js = await self._get_json("https://api.bilibili.com/x/web-interface/nav")
        img = (js.get("data") or {}).get("wbi_img") or {}
        img_url, sub_url = img.get("img_url", ""), img.get("sub_url", "")
        if not img_url or not sub_url:
            raise BiliError("获取 WBI 密钥失败（nav 返回里没有 wbi_img）")
        ik = img_url.rsplit("/", 1)[-1].split(".")[0]
        sk = sub_url.rsplit("/", 1)[-1].split(".")[0]
        self._wbi = (ik, sk, now)
        return ik, sk

    async def _sign(self, params: dict) -> dict:
        ik, sk = await self._wbi_keys()
        orig = ik + sk
        mixin_key = "".join(orig[i] for i in MIXIN_KEY_ENC_TAB if i < len(orig))[:32]
        p = {k: str(v) for k, v in params.items() if v is not None}
        p["wts"] = str(int(time.time()))
        p = {k: re.sub(r"[!'()*]", "", v) for k, v in p.items()}
        query = urlencode(sorted(p.items()), quote_via=quote)
        p["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
        return p

    # ------------------------------------------------------------------ 业务接口

    async def user_info(self, uid: str) -> dict:
        """UP 主基本信息：name / face / sign / level。"""
        params = {
            "mid": str(uid),
            "platform": "web",
            "web_location": "1550101",
            "dm_img_list": "[]",
            "dm_img_str": DM_IMG_STR,
            "dm_cover_img_str": DM_COVER_IMG_STR,
        }
        return await self._api(
            "https://api.bilibili.com/x/space/wbi/acc/info",
            params,
            sign=True,
            referer=f"https://space.bilibili.com/{uid}",
        ) or {}

    async def follower_count(self, uid: str) -> int:
        data = await self._api(
            "https://api.bilibili.com/x/relation/stat",
            {"vmid": str(uid)},
            referer=f"https://space.bilibili.com/{uid}",
        ) or {}
        try:
            return int(data.get("follower") or 0)
        except (TypeError, ValueError):
            return 0

    async def dynamics(self, uid: str, offset: str = "", retries: int = 3) -> dict:
        """动态列表。返回 {items: [...], offset, has_more}。

        B 站对匿名请求的软反爬：大约一半的请求会返回 code=0 但 items 为空，
        而且每次请求独立随机，换参数也不保证成功。这里的办法是多次重试，
        并在两套参数之间交替（带不带 dm_* 指纹参数），降低相关性。
        """
        base = {
            "host_mid": str(uid),
            "offset": offset,
            "timezone_offset": "-480",
            "platform": "web",
            "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
        }
        extra = {
            "web_location": "333.999",
            "dm_img_list": "[]",
            "dm_img_str": DM_IMG_STR,
            "dm_cover_img_str": DM_COVER_IMG_STR,
        }
        data: dict = {}
        attempts = max(1, retries)
        for attempt in range(attempts):
            params = dict(base)
            if attempt % 2 == 0:
                params.update(extra)
            data = await self._api(
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
                params,
                sign=True,
                referer=f"https://space.bilibili.com/{uid}/dynamic",
            ) or {}
            if (data.get("items") or []) or attempt >= attempts - 1:
                break
            logger.debug(f"[bili_api] {uid} 第 {attempt + 1} 次拿到空动态，换参数重试")
            await asyncio.sleep(2.0 + attempt * 2.0 + random.uniform(0, 1.0))
        return {
            "items": data.get("items") or [],
            "offset": data.get("offset") or "",
            "has_more": bool(data.get("has_more")),
        }

    async def dynamic_detail(self, dyn_id: str) -> dict:
        params = {
            "id": str(dyn_id),
            "timezone_offset": "-480",
            "platform": "web",
            "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
        }
        data = await self._api(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail",
            params,
            referer=f"https://t.bilibili.com/{dyn_id}",
        ) or {}
        return (data.get("item") or {}) if isinstance(data, dict) else {}

    async def live_master(self, uid: str) -> dict:
        """主播基本信息（昵称/头像）。注意：这个接口已经不再返回 room_id 了。"""
        data = await self._api(
            "https://api.live.bilibili.com/live_user/v1/Master/info",
            {"uid": str(uid)},
            referer=f"https://live.bilibili.com/{uid}",
        ) or {}
        return (data.get("info") or {}) if isinstance(data, dict) else {}

    async def live_room_id(self, uid: str) -> int:
        """UP 主 → 直播间号。

        `live_user/v1/Master/info` 现在只返回 uid/uname/face，没有 room_id，
        所以改走 `room/v1/Room/getRoomInfoOld`，再兜底读 `acc/info` 的 live_room。
        """
        try:
            data = await self._api(
                "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld",
                {"mid": str(uid)},
                referer=f"https://space.bilibili.com/{uid}",
            ) or {}
            room = int(data.get("roomid") or 0)
            if room > 0:
                return room
        except BiliError as e:
            logger.debug(f"[bili_api] getRoomInfoOld({uid}) 失败：{e}")

        try:
            info = await self.user_info(uid)
            live_room = info.get("live_room") or {}
            return int(live_room.get("roomid") or 0)
        except BiliError as e:
            logger.debug(f"[bili_api] acc/info 兜底取房间号失败：{e}")
            return 0

    async def live_room(self, room_id: int | str) -> dict:
        """直播间实时状态。"""
        data = await self._api(
            "https://api.live.bilibili.com/room/v1/Room/get_info",
            {"room_id": str(room_id)},
            referer=f"https://live.bilibili.com/{room_id}",
        ) or {}
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ 扫码登录

    QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

    async def nav_info(self) -> dict:
        """当前 Cookie 对应的登录账号（没登录时 is_login=False）。"""
        js = await self._get_json(
            "https://api.bilibili.com/x/web-interface/nav",
            referer="https://www.bilibili.com/",
        )
        data = js.get("data") or {}
        return {
            "is_login": bool(data.get("isLogin")),
            "name": data.get("uname") or "",
            "mid": str(data.get("mid") or ""),
            "face": data.get("face") or "",
        }

    async def qr_login_generate(self) -> dict:
        """生成登录二维码，返回 {url, key}。"""
        js = await self._get_json(
            self.QR_GENERATE, need_cookie=False, referer="https://www.bilibili.com/"
        )
        data = js.get("data") or {}
        return {"url": data.get("url") or "", "key": data.get("qrcode_key") or ""}

    async def qr_login_poll(self, key: str) -> dict:
        """查一次扫码状态。

        status: 0 成功 / 86101 未扫码 / 86090 已扫码待确认 / 86038 已过期
        """
        sess = await self._session_or_new()
        headers = {"Referer": "https://www.bilibili.com/"}
        async with sess.get(
            self.QR_POLL,
            params={"qrcode_key": key, "source": "main-fe-header"},
            headers=headers,
            **self._proxy_args(),
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise BiliError(f"HTTP {resp.status}: {text[:120]}")
            try:
                body = json.loads(text)
            except Exception:
                raise BiliError(f"响应不是 JSON: {text[:120]}")

            if int(body.get("code") or 0) != 0:
                raise BiliError(f"code={body.get('code')} {body.get('message')}")

            data = body.get("data") or {}
            cookies: dict[str, str] = {}
            for cookie in resp.cookies.values():
                cookies[cookie.key] = cookie.value
            # 兜底：成功时 data.url 的 query 里也带着那几个关键 cookie
            cookies.update(self._cookies_from_url(data.get("url") or ""))

            # 注意：成功时 code 是 0，不能用 `or -1`（0 会被当成假值）
            raw_code = data.get("code")
            status = int(raw_code) if raw_code is not None else -1
            return {
                "status": status,
                "message": data.get("message") or "",
                "cookies": cookies,
                "url": data.get("url") or "",
            }

    @staticmethod
    def _cookies_from_url(url: str) -> dict[str, str]:
        out: dict[str, str] = {}
        if not url:
            return out
        query = parse_qs(urlsplit(url).query)
        for name in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid"):
            values = query.get(name)
            if values:
                out[name] = values[0]
        return out

    # ------------------------------------------------------------------ 小工具

    @staticmethod
    def dynamic_url(dyn_id: str) -> str:
        return f"https://t.bilibili.com/{dyn_id}"
