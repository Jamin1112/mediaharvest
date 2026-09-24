"""HTML 解析：从静态标记里抽取所有媒体资源。

覆盖的发现渠道：
  * ``img/src`` ``video/src`` ``source/src`` ``audio/src`` ``embed`` ``iframe``
  * 懒加载属性（data-src / data-original / data-lazy-src / data-echo ...）
  * ``srcset`` / ``data-srcset``（按宽度选主图，其余标为 secondary）
  * 内联 ``style`` 与 ``<style>`` 里的 ``background-image: url()``
  * ``og:image`` / ``twitter:image`` / ``og:video`` 等 meta
  * 内联 ``<script>`` 与 JSON-LD 中的绝对媒体 URL
  * ``<a href>`` 直接指向媒体文件
  * ``<video poster>`` 作为图片
  * ``<track>`` 字幕
"""
from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List, Optional, Set
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .models import MediaItem, MediaType, Source
from .utils import (
    URL_IN_TEXT_RE,
    classify,
    clean_url,
    ext_from_content_type,
    looks_like_media_url,
    resolve_url,
    strip_tracking,
    url_ext,
)

# --------------------------------------------------------------------------
# 属性表
# --------------------------------------------------------------------------

#: 直接承载资源地址的属性
DIRECT_ATTRS = (
    "src", "href", "data-src", "data-original", "data-lazy", "data-lazy-src",
    "data-echo", "data-url", "data-image", "data-img", "data-thumb",
    "data-thumbnail", "data-cover", "data-poster", "data-video",
    "data-video-src", "data-mp4", "data-file", "data-href", "data-source",
    "data-actualsrc", "data-origin-src", "data-big", "data-large",
    "data-real-src", "data-src-large", "data-zoom-image", "data-image-src",
    "file", "content", "url", "vurl", "videourl",
)

#: 懒加载相关属性（单独标记来源）
LAZY_ATTRS = (
    "data-src", "data-original", "data-lazy", "data-lazy-src", "data-echo",
    "data-actualsrc", "data-origin-src", "data-real-src", "data-big",
    "data-large", "data-src-large", "data-zoom-image", "data-image",
    "data-img", "data-url", "data-original-src", "data-defer-src",
)

SRCSET_ATTRS = ("srcset", "data-srcset", "data-lazy-srcset", "imagesrcset")

#: 需要跳过的属性值（占位图 / 空白）
PLACEHOLDER_HINTS = (
    "data:image/gif;base64,r0lgodlhaqaba",  # 1x1 透明 gif
    "data:image/gif;base64,r0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7".lower(),
    "blank.gif", "spacer.gif", "placeholder", "loading.gif", "grey.gif",
    "px.gif", "1x1.png", "transparent.png",
)

CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""", re.I)
SCRIPT_URL_RE = re.compile(
    r"""["']((?:https?:)?//[^"'\s<>]+?\.(?:jpg|jpeg|png|gif|webp|avif|bmp|svg|"""
    r"""mp4|m4v|mov|webm|mkv|flv|m3u8|mpd|mp3|m4a|aac|ts)(?:\?[^"'\s<>]*)?)["']""",
    re.I,
)

#: 脚本里的流地址可能没有标准扩展名位置（如 ".../master.m3u8?sign=x"），
#: 这个正则专门匹配带 m3u8/mpd 的任意引号字符串
_STREAM_IN_SCRIPT_RE = re.compile(
    r"""["']((?:https?:)?//[^"'\s<>]*?\.(?:m3u8|mpd)(?:\?[^"'\s<>]*)?)["']""",
    re.I,
)

MEDIA_TAG_TYPES: Dict[str, MediaType] = {
    "img": MediaType.IMAGE,
    "image": MediaType.IMAGE,
    "video": MediaType.VIDEO,
    "audio": MediaType.AUDIO,
    "source": MediaType.OTHER,   # 由父标签决定
    "embed": MediaType.VIDEO,
    "object": MediaType.VIDEO,
    "iframe": MediaType.OTHER,
    "track": MediaType.OTHER,
}


def _is_placeholder(url: str) -> bool:
    low = url.lower()
    if not low:
        return True
    if "data:image" in low and "base64" in low:
        # 内联的小图标（<2KB）视为占位
        if len(url) < 2048:
            return True
    return any(h in low for h in PLACEHOLDER_HINTS)


def _parse_srcset(value: str) -> List[str]:
    """解析 srcset，返回按宽度升序排列的候选 URL。"""
    out: List[tuple] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        pieces = part.split()
        if not pieces:
            continue
        url = pieces[0]
        # 形如 "a.jpg 2x, b.jpg 800w" —— 无描述符时按 1x
        if len(pieces) > 1:
            desc = pieces[1].lower()
            if desc.endswith("w"):
                try:
                    out.append((int(desc[:-1]), url))
                    continue
                except ValueError:
                    pass
            elif desc.endswith("x"):
                try:
                    out.append((int(float(desc[:-1]) * 1000), url))
                    continue
                except ValueError:
                    pass
        out.append((0, url))
    out.sort(key=lambda t: t[0])
    return [u for _, u in out]


class Extractor:
    """把一份 HTML 变成 :class:`MediaItem` 列表。"""

    def __init__(
        self,
        base_url: str,
        *,
        same_host_only: bool = False,
        min_image_px: int = 0,
        include_segments: bool = False,
    ) -> None:
        self.base_url = base_url
        self.same_host_only = same_host_only
        self.min_image_px = min_image_px
        self.include_segments = include_segments
        self._seen: Set[str] = set()
        self.items: List[MediaItem] = []

    # ---- 入口 --------------------------------------------------------

    def extract(self, html: str) -> List[MediaItem]:
        if not html:
            return []
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")

        self._extract_meta(soup)
        self._extract_tags(soup)
        self._extract_css(soup)
        self._extract_scripts(html)
        self._extract_anchors(soup)
        return self.items

    # ---- 各渠道 ------------------------------------------------------

    def _extract_meta(self, soup: BeautifulSoup) -> None:
        meta_keys = {
            "og:image": MediaType.IMAGE, "og:image:url": MediaType.IMAGE,
            "og:image:secure_url": MediaType.IMAGE,
            "twitter:image": MediaType.IMAGE, "twitter:image:src": MediaType.IMAGE,
            "image": MediaType.IMAGE, "thumbnail": MediaType.IMAGE,
            "og:video": MediaType.VIDEO, "og:video:url": MediaType.VIDEO,
            "og:video:secure_url": MediaType.VIDEO,
            "twitter:player:stream": MediaType.VIDEO,
            "og:audio": MediaType.AUDIO, "og:audio:url": MediaType.AUDIO,
        }
        # og:video 有时指向的是**播放器页面**而非媒体文件（B 站即如此），
        # 页面会用 og:video:type 声明真实类型，据其排除非媒体链接。
        declared_types: Dict[str, str] = {}
        for tag in soup.find_all("meta"):
            key = (tag.get("property") or tag.get("name") or "").strip().lower()
            if not key.endswith(":type"):
                continue
            if not key.startswith(("og:video", "og:audio")):
                continue
            # og:video:type -> og:video；og:audio:type -> og:audio
            base = key[: -len(":type")]
            declared_types[base] = (tag.get("content") or "").strip().lower()

        for tag in soup.find_all("meta"):
            key = (tag.get("property") or tag.get("name") or "").strip().lower()
            content = tag.get("content") or ""
            if key in meta_keys and content:
                mtype = meta_keys[key]
                # 排除「声明为 HTML 的 og:video/og:audio」——那是播放器页面
                if key == "og:video" or key.startswith("og:video:") or \
                        key == "og:audio" or key.startswith("og:audio:"):
                    base = "og:video" if key.startswith("og:video") else "og:audio"
                    dtype = declared_types.get(base, "")
                    if "html" in dtype:
                        continue
                self._add(content, mtype, Source.META)

        # <link rel="image_src"> / apple-touch-icon / icon
        for tag in soup.find_all("link"):
            rel = " ".join(tag.get("rel") or []).lower()
            href = tag.get("href") or ""
            if not href:
                continue
            if "image_src" in rel or "apple-touch-icon" in rel:
                self._add(href, MediaType.IMAGE, Source.META)
            elif rel.strip() in {"icon", "shortcut icon"}:
                self._add(href, MediaType.IMAGE, Source.META)

    def _extract_tags(self, soup: BeautifulSoup) -> None:
        for tag in soup.find_all(True):
            name = (tag.name or "").lower()
            if name in {"script", "style", "meta", "link", "noscript"}:
                continue
            base_type = MEDIA_TAG_TYPES.get(name)
            parent_name = (tag.parent.name or "").lower() if isinstance(tag.parent, Tag) else ""
            if name == "source":
                # <video><source> / <picture><source>
                base_type = MediaType.VIDEO if parent_name in {"video", "audio"} else MediaType.IMAGE
            if base_type is None and name not in {"a", "div", "span", "li", "section"}:
                continue

            # 1) srcset 优先（多分辨率）
            for attr in SRCSET_ATTRS:
                value = tag.get(attr)
                if not value or not isinstance(value, str):
                    continue
                urls = _parse_srcset(value)
                if not urls:
                    continue
                group = f"srcset:{tag.name}:{id(tag) % 100000}"
                best = urls[-1]  # 最大分辨率作为主选
                for u in urls:
                    self._add(
                        u,
                        base_type if base_type != MediaType.OTHER else MediaType.IMAGE,
                        Source.SRCSET,
                        group=group,
                        primary=(u == best),
                    )

            # 2) 直接属性
            if base_type is not None:
                for attr in DIRECT_ATTRS:
                    value = tag.get(attr)
                    if not value or not isinstance(value, str):
                        continue
                    if attr == "content" and name not in {"meta"}:
                        continue
                    src = Source.LAZY if attr in LAZY_ATTRS else Source.TAG
                    self._add(value, base_type, src)

            # 3) poster 一律当图片；track 当字幕
            if name == "video":
                poster = tag.get("poster")
                if poster:
                    self._add(poster, MediaType.IMAGE, Source.TAG)
            if name == "track":
                src = tag.get("src")
                if src:
                    self._add(src, MediaType.OTHER, Source.TAG)

            # 4) 内联 style
            style = tag.get("style")
            if style:
                for m in CSS_URL_RE.finditer(style):
                    self._add(m.group(1), MediaType.IMAGE, Source.STYLE)

    def _extract_css(self, soup: BeautifulSoup) -> None:
        for style_tag in soup.find_all("style"):
            text = style_tag.string or style_tag.get_text() or ""
            for m in CSS_URL_RE.finditer(text):
                self._add(m.group(1), MediaType.IMAGE, Source.STYLE)

    def _extract_scripts(self, html: str) -> None:
        # 脚本里的 URL 常被转义（"https:\/\/a.com\/x.jpg" 或 \u002F），
        # 先做一次宽松还原再匹配，否则会漏掉大量真实地址。
        probe = html
        if "\\/" in probe or "\\u002" in probe.lower():
            probe = (
                probe.replace("\\/", "/")
                .replace("\\u002F", "/")
                .replace("\\u002f", "/")
                .replace("\\u0026", "&")
            )

        for m in SCRIPT_URL_RE.finditer(probe):
            self._add(m.group(1), MediaType.OTHER, Source.SCRIPT)

        # 形如 "playUrl":"https://.../master.m3u8" 的流地址
        for m in _STREAM_IN_SCRIPT_RE.finditer(probe):
            self._add(m.group(1), MediaType.OTHER, Source.SCRIPT)

        # JSON-LD
        for m in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            probe, re.I | re.S,
        ):
            blob = m.group(1).strip()
            try:
                data = json.loads(blob)
            except Exception:
                continue
            for url in _iter_urls_in_json(data):
                self._add(url, MediaType.OTHER, Source.SCRIPT)

    def _extract_anchors(self, soup: BeautifulSoup) -> None:
        for tag in soup.find_all("a"):
            href = tag.get("href")
            if not href or not isinstance(href, str):
                continue
            kind = classify(href)
            if kind in (MediaType.IMAGE, MediaType.VIDEO, MediaType.AUDIO, MediaType.HLS, MediaType.DASH):
                self._add(href, kind, Source.ANCHOR)

    # ---- 添加 --------------------------------------------------------

    def _add(
        self,
        raw: str,
        mtype: MediaType,
        source: Source,
        *,
        group: str = "",
        primary: bool = True,
    ) -> None:
        if not raw or _is_placeholder(raw):
            return
        url = resolve_url(self.base_url, raw)
        if not url:
            return
        url = strip_tracking(url)
        if url in self._seen:
            return

        real_type = classify(url)
        if real_type == MediaType.OTHER:
            if mtype == MediaType.OTHER:
                return
            real_type = mtype
        elif mtype != MediaType.OTHER and real_type != mtype:
            # 标签语义与扩展名冲突时以语义为准（例如 <video src="x.php">）
            if real_type == MediaType.SEGMENT and not self.include_segments:
                return
            real_type = mtype

        if real_type == MediaType.SEGMENT and not self.include_segments:
            return
        if real_type == MediaType.OTHER:
            return

        if self.same_host_only:
            from urllib.parse import urlsplit
            if urlsplit(url).netloc.split(":")[0] != urlsplit(self.base_url).netloc.split(":")[0]:
                return

        self._seen.add(url)
        self.items.append(
            MediaItem(
                url=url,
                type=real_type,
                source=source,
                page_url=self.base_url,
                ext=url_ext(url),
                group=group,
                primary=primary,
                referer=self.base_url,
            )
        )


def _iter_urls_in_json(data: object, depth: int = 0) -> Iterable[str]:
    """递归遍历 JSON，取出看起来像媒体地址的字符串。"""
    if depth > 12:
        return
    if isinstance(data, str):
        if data.startswith(("http://", "https://", "//")):
            yield data
        return
    if isinstance(data, dict):
        for value in data.values():
            yield from _iter_urls_in_json(value, depth + 1)
    elif isinstance(data, (list, tuple)):
        for value in data:
            yield from _iter_urls_in_json(value, depth + 1)


def extract_from_html(
    html: str,
    base_url: str,
    *,
    same_host_only: bool = False,
    include_segments: bool = False,
) -> List[MediaItem]:
    """便捷函数：从 HTML 里抽取媒体条目。"""
    return Extractor(
        base_url,
        same_host_only=same_host_only,
        include_segments=include_segments,
    ).extract(html)
