"""B 站（bilibili）专用解析器。

B 站无法只靠通用手段抓到视频，原因是：

  * 视频地址不在 HTML 里，而是播放器通过 XHR 从 ``playurl`` 接口取回
  * 该接口有风控，缺少正确的 ``Referer`` / ``User-Agent`` 会返回 HTTP 412
  * 下载分片时同样必须带 ``Referer``，否则 403

本模块走官方 Web 接口两步获取：

  1. ``x/web-interface/view``  → aid / cid / 标题 / 封面
  2. ``x/player/playurl``      → 真实媒体地址

**关于 durl 与 DASH 的取舍（实测结论）**：未登录状态下对同一视频实测，

  * ``fnval=1``（durl，音视频**已混合**）最高可拿到 **720P**，单文件即完整可播
  * ``fnval=4048``（DASH，音视频**分离**）反而只给到 480P，且需额外合并

因此优先使用 durl：画质更高、无需合并、失败面更小。当 durl 不可用时
（例如需要登录的高清内容）再回落到 DASH，并把音频轨作为独立条目返回，
由下载器合并。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .models import MediaItem, MediaType, Source
from .utils import sanitize_filename

#: 画质代码 → 名称
QUALITY_NAMES = {
    127: "8K 超高清", 126: "杜比视界", 125: "HDR 真彩", 120: "4K 超清",
    116: "1080P60", 112: "1080P+", 80: "1080P", 74: "720P60",
    64: "720P", 32: "480P", 16: "360P", 6: "240P",
}

#: 编码兼容性优先级（数字越小越兼容）
CODEC_PRIORITY = {"avc": 0, "hvc": 1, "hev": 1, "av01": 2}

#: 期望的画质请求顺序（从高到低）
QUALITY_TRY = (127, 120, 116, 112, 80, 74, 64, 32, 16)


def is_bilibili(url: str) -> bool:
    try:
        host = urlsplit(url).netloc.lower().split(":")[0]
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in ("bilibili.com", "b23.tv"))


def extract_bvid(url: str) -> str:
    """从各种 B 站 URL 形式里取出 BV 号 / av 号。"""
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if m:
        return m.group(1)
    m = re.search(r"/video/av(\d+)", url, re.I)
    if m:
        return "av" + m.group(1)
    return ""


def _codec_rank(codecs: str) -> int:
    low = (codecs or "").lower()
    for key, rank in CODEC_PRIORITY.items():
        if low.startswith(key):
            return rank
    return 9


class BilibiliParser:
    """通过 B 站 Web 接口解析视频流地址。"""

    API_VIEW = "https://api.bilibili.com/x/web-interface/view"
    API_PLAYURL = "https://api.bilibili.com/x/player/playurl"

    def __init__(self, fetcher: Any, *, page_url: str = "") -> None:
        self.fetcher = fetcher
        self.page_url = page_url

    async def parse(self, url: str) -> Tuple[List[MediaItem], Dict[str, Any]]:
        """解析视频，返回 ``(媒体条目列表, 视频元信息)``。"""
        bvid = extract_bvid(url)
        if not bvid:
            return [], {}

        referer = self.page_url or f"https://www.bilibili.com/video/{bvid}/"
        headers = {"Referer": referer, "Origin": "https://www.bilibili.com"}

        info = await self._api_view(bvid, headers, referer)
        if not info:
            return [], {}

        title = info.get("title") or ""
        cover = info.get("pic") or ""
        duration = info.get("duration")
        aid = info.get("aid")
        pages = info.get("pages") or []
        cid = pages[0].get("cid") if pages else info.get("cid")
        if not cid:
            return [], {}

        items: List[MediaItem] = []
        meta: Dict[str, Any] = {
            "title": title, "cover": cover, "duration": duration,
            "bvid": bvid, "aid": aid, "cid": cid, "backend": "bilibili",
        }

        # 封面图
        if cover:
            items.append(MediaItem(
                url=cover, type=MediaType.IMAGE, source=Source.META,
                page_url=referer, ext="jpg", title=f"{title} 封面".strip(),
                filename="cover.jpg", referer=referer,
                meta={"backend": "bilibili", "role": "cover"},
            ))

        # 首选：durl（音视频混合，实测画质更高且免合并）
        durl_items = await self._try_durl(aid, cid, headers, referer, title, duration, bvid)
        items.extend(durl_items)

        # 回落：DASH（分离流，需合并）
        if not durl_items:
            dash = await self._api_playurl_dash(aid, cid, headers, referer)
            if dash:
                items.extend(
                    self._dash_to_items(dash, referer, title, duration, bvid)
                )
                meta["mode"] = "dash"

        return items, meta

    # ---- 接口调用 ----------------------------------------------------

    async def _api_view(
        self, bvid: str, headers: Dict[str, str], referer: str
    ) -> Optional[Dict[str, Any]]:
        param = "bvid" if bvid.startswith("BV") else "aid"
        value = bvid if bvid.startswith("BV") else bvid[2:]
        try:
            resp = await self.fetcher.get(
                f"{self.API_VIEW}?{param}={value}", referer=referer, headers=headers
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
        except Exception:
            return None
        if data.get("code") != 0:
            return None
        return data.get("data") or None

    async def _try_durl(
        self,
        aid: Any,
        cid: Any,
        headers: Dict[str, str],
        referer: str,
        title: str,
        duration: Optional[float],
        bvid: str,
    ) -> List[MediaItem]:
        """请求音视频混合流，返回条目；不可用则返回空列表。

        从高到低尝试画质：接口会返回它实际允许的最高画质，
        因此请求 1080P 可能实际拿到 720P，以响应里的 quality 为准。
        """
        best: Optional[Dict[str, Any]] = None
        for qn in QUALITY_TRY:
            payload = await self._api_playurl(
                aid, cid, headers, referer, qn=qn, fnval=1
            )
            if not payload:
                continue
            durl = payload.get("durl") or []
            if not durl:
                continue
            quality = int(payload.get("quality") or 0)
            if best is None or quality > int(best["quality"]):
                best = {"quality": quality, "durl": durl, "format": payload.get("format")}
            # 已拿到最高档就不必再试
            if quality >= QUALITY_TRY[0]:
                break

        if not best:
            return []

        quality = int(best["quality"])
        label = QUALITY_NAMES.get(quality, str(quality))
        durl = best["durl"]

        # 接口的 format 字段形如 "mp4720" / "flv720"，取前缀判断容器。
        # 实测即使是 mp4 也会返回 ftyp(isom)，故以接口前缀为准并回落 mp4。
        fmt = (best.get("format") or "mp4").lower()
        ext = "flv" if fmt.startswith("flv") else "mp4"

        # durl 可能是多个片段（长视频分段），逐段作为独立条目并按序命名
        items: List[MediaItem] = []
        single = len(durl) == 1
        for idx, seg in enumerate(durl):
            u = seg.get("url")
            if not u:
                continue
            # 备用线路（backup_url）可提升成功率
            backups = seg.get("backup_url") or []
            name_title = title if single else f"{title} 第{idx + 1}段"
            items.append(MediaItem(
                url=u,
                type=MediaType.VIDEO,
                source=Source.NETWORK,
                page_url=referer,
                ext=ext,
                filename=self._filename(title, label, idx, single, ext),
                duration=duration,
                title=name_title,
                referer=referer,
                group="bilibili:video",
                primary=True,
                content_type="video/mp4" if ext == "mp4" else "video/x-flv",
                size=seg.get("size"),
                meta={
                    "backend": "bilibili",
                    "mode": "durl",
                    "quality": label,
                    "quality_id": quality,
                    "muxed": True,          # 已含音轨，无需合并
                    "bvid": bvid,
                    "segment": idx + 1,
                    "segments": len(durl),
                    "backup_urls": backups,
                    "length_ms": seg.get("length"),
                },
            ))
        return items

    async def _api_playurl_dash(
        self, aid: Any, cid: Any, headers: Dict[str, str], referer: str
    ) -> Optional[Dict[str, Any]]:
        payload = await self._api_playurl(
            aid, cid, headers, referer, qn=127, fnval=4048, extra="&fnver=0&fourk=1"
        )
        if not payload:
            return None
        return payload.get("dash") or None

    async def _api_playurl(
        self,
        aid: Any,
        cid: Any,
        headers: Dict[str, str],
        referer: str,
        *,
        qn: int,
        fnval: int,
        extra: str = "",
    ) -> Optional[Dict[str, Any]]:
        query = f"avid={aid}&cid={cid}&qn={qn}&fnval={fnval}&fourk=1{extra}"
        try:
            resp = await self.fetcher.get(
                f"{self.API_PLAYURL}?{query}", referer=referer, headers=headers
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
        except Exception:
            return None
        if data.get("code") != 0:
            return None
        return data.get("data") or None

    @staticmethod
    def _filename(title: str, label: str, idx: int, single: bool, ext: str) -> str:
        stem = sanitize_filename(title, max_len=60) or "bilibili"
        suffix = "" if single else f"_part{idx + 1}"
        return f"{stem}_{label}{suffix}.{ext}"

    # ---- DASH 回落 ---------------------------------------------------

    def _dash_to_items(
        self,
        dash: Dict[str, Any],
        referer: str,
        title: str,
        duration: Optional[float],
        bvid: str,
    ) -> List[MediaItem]:
        """把 DASH 描述转成条目（音视频分离，需下载后合并）。"""
        items: List[MediaItem] = []

        tracks: List[Tuple[int, int, Dict[str, Any]]] = []
        for v in dash.get("video") or []:
            tracks.append((int(v.get("id") or 0), _codec_rank(v.get("codecs") or ""), v))
        if not tracks:
            return items
        tracks.sort(key=lambda t: (t[0], -t[1]))

        best_q = tracks[-1][0]
        group = sorted([t for t in tracks if t[0] == best_q], key=lambda t: t[1])
        best = group[0][2]
        best_url = best.get("baseUrl") or best.get("base_url") or ""
        if not best_url:
            return items

        label = QUALITY_NAMES.get(best_q, str(best_q))
        height = best.get("height")
        stem = sanitize_filename(title, max_len=60) or "bilibili"

        items.append(MediaItem(
            url=best_url, type=MediaType.VIDEO, source=Source.NETWORK,
            page_url=referer, ext="m4s",
            filename=f"{stem}_{label}_{height}p.mp4" if height else f"{stem}_{label}.mp4",
            width=best.get("width"), height=height, duration=duration,
            title=title, referer=referer, group="bilibili:video", primary=True,
            meta={"backend": "bilibili", "mode": "dash", "quality": label,
                  "codecs": best.get("codecs", ""), "needs_audio": True, "bvid": bvid},
        ))

        # 音频轨（取码率最高），标记需合并
        audios = sorted(
            dash.get("audio") or [],
            key=lambda a: int(a.get("bandwidth") or 0), reverse=True,
        )
        if audios:
            au = audios[0].get("baseUrl") or audios[0].get("base_url")
            if au:
                items.append(MediaItem(
                    url=au, type=MediaType.AUDIO, source=Source.NETWORK,
                    page_url=referer, ext="m4s", duration=duration,
                    title=f"{title} 音频", referer=referer,
                    group="bilibili:audio", primary=True,
                    meta={"backend": "bilibili", "mode": "dash",
                          "role": "audio", "merge_with_video": True, "bvid": bvid},
                ))
        return items


__all__ = ["BilibiliParser", "extract_bvid", "is_bilibili", "QUALITY_NAMES"]
