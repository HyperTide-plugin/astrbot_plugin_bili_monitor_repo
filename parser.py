"""B 站动态卡片解析。

web-dynamic 的 item 结构又深又乱，这里统一成：

    {
        "id":     动态 id（字符串）
        "uid":    发布者 mid
        "nick":   发布者昵称
        "face":   头像
        "ts":     发布时间戳
        "type":   DYNAMIC_TYPE_xx
        "kind":   dynamic / video / article / audio / forward / live
        "text":   正文
        "title":  视频/专栏/音频标题（没有就是空串）
        "images": 图片地址列表
        "cover":  封面（视频/专栏/音频/直播）
        "link":   点击跳转地址
        "pinned": 是否置顶动态
        "orig":   转发的原动态（同样结构，深度 1）
    }
"""

from __future__ import annotations

import json
from typing import Any

KIND_LABEL = {
    "dynamic": "动态",
    "video": "视频",
    "article": "专栏",
    "audio": "音频",
    "forward": "转发",
    "live": "直播",
}

_TYPE_TO_KIND = {
    "DYNAMIC_TYPE_AV": "video",
    "DYNAMIC_TYPE_ARTICLE": "article",
    "DYNAMIC_TYPE_AUDIO": "audio",
    "DYNAMIC_TYPE_FORWARD": "forward",
    "DYNAMIC_TYPE_LIVE": "live",
    "DYNAMIC_TYPE_LIVE_RCMD": "live",
}

_MAJOR_TO_KIND = {
    "MAJOR_TYPE_ARCHIVE": "video",
    "MAJOR_TYPE_ARTICLE": "article",
    "MAJOR_TYPE_AUDIO": "audio",
    "MAJOR_TYPE_LIVE_RCMD": "live",
    "MAJOR_TYPE_LIVE": "live",
}


def _fix_url(url: Any) -> str:
    """B 站图片/跳转地址经常是 // 开头或者 http。"""
    if not isinstance(url, str):
        return ""
    url = url.strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://") and ("hdslb.com" in url or "bilibili.com" in url):
        return "https://" + url[len("http://"):]
    return url


def _uniq(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _author(item: dict) -> dict:
    modules = item.get("modules") or {}
    return modules.get("module_author") or {}


def _dynamic_module(item: dict) -> dict:
    modules = item.get("modules") or {}
    return modules.get("module_dynamic") or {}


def _is_pinned(item: dict) -> bool:
    modules = item.get("modules") or {}
    tag = modules.get("module_tag") or {}
    return (tag.get("text") or "").strip() == "置顶"


def _parse_live_rcmd(major: dict) -> tuple[str, str, str]:
    """MAJOR_TYPE_LIVE_RCMD 的 content 是一段 JSON 字符串。"""
    raw = (major.get("live_rcmd") or {}).get("content") or ""
    title, cover, room_id = "", "", ""
    if raw:
        try:
            payload = json.loads(raw)
            info = (payload.get("live_play_info") or {}) if isinstance(payload, dict) else {}
            title = info.get("title") or ""
            cover = info.get("cover") or ""
            room_id = str(info.get("room_id") or "")
        except Exception:
            pass
    return title, _fix_url(cover), room_id


def parse_dynamic(item: dict, _depth: int = 0) -> dict | None:
    """把一条动态 item 解析成统一结构；解析不出来返回 None。"""
    if not isinstance(item, dict):
        return None

    dyn_id = str(item.get("id_str") or item.get("id") or "")
    if not dyn_id:
        return None

    author = _author(item)
    nick = author.get("name") or ""
    uid = str(author.get("mid") or "")
    face = _fix_url(author.get("face"))
    ts = int(author.get("pub_ts") or 0)

    raw_type = item.get("type") or ""
    kind = _TYPE_TO_KIND.get(raw_type, "")

    dyn = _dynamic_module(item)
    desc = dyn.get("desc") or {}
    text = (desc.get("text") or "").strip()
    major = dyn.get("major") or {}
    major_type = major.get("type") or ""
    if not kind:
        kind = _MAJOR_TO_KIND.get(major_type, "dynamic")

    title = ""
    cover = ""
    images: list[str] = []
    link = ""

    # 视频
    if major_type == "MAJOR_TYPE_ARCHIVE":
        archive = major.get("archive") or {}
        title = archive.get("title") or ""
        cover = _fix_url(archive.get("cover"))
        link = _fix_url(archive.get("jump_url")) or (
            f"https://www.bilibili.com/video/{archive.get('bvid')}" if archive.get("bvid") else ""
        )
        if not text:
            text = (archive.get("desc") or "").strip()

    # 专栏
    elif major_type == "MAJOR_TYPE_ARTICLE":
        article = major.get("article") or {}
        title = article.get("title") or ""
        covers = article.get("covers") or []
        cover = _fix_url(covers[0]) if covers else ""
        link = _fix_url(article.get("jump_url"))

    # 音频
    elif major_type == "MAJOR_TYPE_AUDIO":
        audio = major.get("audio") or {}
        title = audio.get("title") or ""
        cover = _fix_url(audio.get("cover"))
        link = _fix_url(audio.get("jump_url"))

    # 直播卡片
    elif major_type in ("MAJOR_TYPE_LIVE_RCMD", "MAJOR_TYPE_LIVE"):
        title, cover, room_id = _parse_live_rcmd(major)
        if not room_id:
            live = major.get("live") or {}
            room_id = str(live.get("room_id") or "")
            title = title or (live.get("title") or "")
            cover = cover or _fix_url(live.get("cover"))
        if room_id:
            link = f"https://live.bilibili.com/{room_id}"

    # 图文 / 动态正文
    elif major_type == "MAJOR_TYPE_OPUS":
        opus = major.get("opus") or {}
        title = opus.get("title") or ""
        summary = (opus.get("summary") or {}).get("text") or ""
        if summary and not text:
            text = summary.strip()
        images = [_fix_url(p.get("url")) for p in (opus.get("pics") or []) if isinstance(p, dict)]
        link = _fix_url(opus.get("jump_url"))

    elif major_type in ("MAJOR_TYPE_DRAW", "MAJOR_TYPE_COMMON"):
        draw = major.get("draw") or {}
        images = [_fix_url(i.get("src")) for i in (draw.get("items") or []) if isinstance(i, dict)]
        common = major.get("common") or {}
        if common:
            cover = _fix_url(common.get("cover"))
            title = title or (common.get("title") or "")
            link = link or _fix_url(common.get("jump_url"))
            if not text:
                text = (common.get("desc") or "").strip()

    # 兜底：任何卡片都试着抽 common / 第一张图
    if not cover:
        common = major.get("common") or {}
        cover = _fix_url(common.get("cover"))
    if not images and cover and kind in ("dynamic", "article"):
        images = [cover]

    if not link:
        link = f"https://t.bilibili.com/{dyn_id}"

    # 现在的 B 站常把视频/专栏/音频塞进 opus 卡片，靠跳转链接纠正类型
    if kind == "dynamic" and link:
        if "/video/" in link:
            kind = "video"
        elif "/read/" in link:
            kind = "article"
        elif "/audio/" in link:
            kind = "audio"
        if kind != "dynamic" and not cover and images:
            cover = images[0]

    # 转发：原动态单独解析一层
    orig = None
    if _depth == 0 and kind == "forward":
        orig = parse_dynamic(item.get("orig") or {}, _depth=1)

    # 转发时正文常见 "转发动态" 之类的占位，补一句来源
    if kind == "forward" and orig:
        src = orig.get("nick") or "原动态"
        head = f"转发了 {src} 的动态"
        if text:
            text = f"{head}\n{text}"
        else:
            text = head

    return {
        "id": dyn_id,
        "uid": uid,
        "nick": nick,
        "face": face,
        "ts": ts,
        "type": raw_type,
        "kind": kind,
        "text": text,
        "title": title,
        "images": _uniq(images)[:9],
        "cover": cover,
        "link": link,
        "pinned": _is_pinned(item),
        "orig": orig,
    }
