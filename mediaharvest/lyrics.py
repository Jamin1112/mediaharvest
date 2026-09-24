"""歌词抓取：按平台接口取 LRC 歌词，并与译文合并。

各平台的歌词接口不通用，因此这里用一张「平台 key → 取歌词函数」的
小注册表：新增平台只需实现一个返回 ``Lyrics`` 的函数并注册。
拿不到歌词不是错误——很多曲目本来就无歌词，调用方按「尽力而为」处理。

接口均为平台**公开**的只读接口，不涉及登录态或签名破解；
需要登录才能取歌词的平台会直接返回空，而不是伪造结果。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

import httpx

from . import music as music_mod

#: 歌词接口通常要求带 Referer，否则会被 CDN 拒绝。
_DEFAULT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}


@dataclass
class Lyrics:
    """一首歌的歌词。"""

    text: str = ""          # 主歌词（通常是 LRC）
    translation: str = ""   # 翻译/音译歌词
    source: str = ""        # 来源平台 key

    @property
    def merged(self) -> str:
        """原文与译文按时间轴合并后的 LRC。"""
        if self.translation:
            return music_mod.merge_lrc(self.text, self.translation)
        return self.text

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())


async def _get_json(client: httpx.AsyncClient, url: str, referer: str = "") -> Optional[Dict[str, Any]]:
    """取 JSON，失败返回 None（歌词失败不应该影响下载流程）。"""
    headers = dict(_DEFAULT_HEADERS)
    if referer:
        headers["Referer"] = referer
    try:
        resp = await client.get(url, headers=headers)
    except (httpx.HTTPError, OSError):
        return None
    if resp.status_code >= 400:
        return None
    try:
        return json.loads(resp.text)
    except (json.JSONDecodeError, ValueError):
        return None


# --------------------------------------------------------------------------
# 网易云
# --------------------------------------------------------------------------

async def netease_lyrics(
    client: httpx.AsyncClient, track_id: str, *, page_url: str = ""
) -> Lyrics:
    """网易云歌词。

    用 ``/api/song/lyric`` 公开接口，``lv=1&kv=1&tv=-1`` 分别请求
    原文、卡拉OK 与翻译歌词；我们只要原文（``lrc``）和译文（``tlyric``）。
    """
    if not track_id.isdigit():
        return Lyrics(source="netease")
    url = (f"https://music.163.com/api/song/lyric?id={track_id}"
           f"&lv=1&kv=1&tv=-1")
    data = await _get_json(client, url, referer=page_url or "https://music.163.com/")
    if not data:
        return Lyrics(source="netease")
    lrc = ((data.get("lrc") or {}).get("lyric") or "").strip()
    trans = ((data.get("tlyric") or {}).get("lyric") or "").strip()
    return Lyrics(text=lrc, translation=trans, source="netease")


#: ``平台 key -> (client, track_id, page_url) -> Lyrics``
LYRICS_PARSERS: Dict[str, Callable[..., Any]] = {
    "netease": netease_lyrics,
}


def supports_lyrics(platform_key: str) -> bool:
    """该平台是否有可用的歌词接口。"""
    return platform_key in LYRICS_PARSERS


async def fetch_lyrics(
    client: httpx.AsyncClient,
    platform_key: str,
    track_id: str,
    *,
    page_url: str = "",
) -> Lyrics:
    """按平台取歌词；平台无接口时返回空 :class:`Lyrics`。"""
    parser = LYRICS_PARSERS.get(platform_key)
    if not parser or not track_id:
        return Lyrics(source=platform_key)
    try:
        return await parser(client, track_id, page_url=page_url)
    except Exception:
        # 歌词是锦上添花，任何异常都降级成「没有歌词」
        return Lyrics(source=platform_key)


__all__ = [
    "LYRICS_PARSERS",
    "Lyrics",
    "fetch_lyrics",
    "netease_lyrics",
    "supports_lyrics",
]
