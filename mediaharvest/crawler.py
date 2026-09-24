"""抓取编排层：把静态解析、无头渲染、网络嗅探、yt-dlp 串成一条流水线。

设计原则是「逐步升级」：先用最便宜的静态 HTTP 解析，只有在结果不足时才
启用更重的无头浏览器；对于已知的视频站点再用 yt-dlp 兜底。这样既能应对
「兼容所有类型网址」的需求，又不会对普通静态页面浪费资源。
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set
from urllib.parse import urljoin, urlsplit

from . import ytdlp as ytdlp_mod
from .browser import BrowserRenderer, PlaywrightUnavailable, playwright_available
from .extractor import Extractor
from .fetcher import DEFAULT_UA, Fetcher, same_site
from .models import MediaItem, MediaType, PageCapture, Source
from .utils import classify, ext_from_content_type, resolve_url, url_ext

try:
    from . import bilibili

    BILIBILI_AVAILABLE = True
except Exception:  # pragma: no cover - 防御性
    bilibili = None  # type: ignore[assignment]
    BILIBILI_AVAILABLE = False

#: 静态解析结果少于这个数量时，自动升级到无头渲染
AUTO_RENDER_THRESHOLD = 3

#: 这些站点几乎必然是 JS 渲染 / 有反爬，直接上浏览器
KNOWN_JS_SITES = (
    "xiaohongshu.com", "douyin.com", "tiktok.com", "weibo.com", "zhihu.com",
    "instagram.com", "twitter.com", "x.com", "facebook.com", "bilibili.com",
    "kuaishou.com", "taobao.com", "tmall.com", "jd.com", "pinterest.com",
    "reddit.com", "threads.net", "tumblr.com", "weixin.qq.com", "mp.weixin.qq.com",
)

#: 这些站点交给 yt-dlp 效果最好（视频平台）
KNOWN_VIDEO_SITES = (
    "youtube.com", "youtu.be", "bilibili.com", "douyin.com", "tiktok.com",
    "twitter.com", "x.com", "instagram.com", "vimeo.com", "dailymotion.com",
    "kuaishou.com", "weibo.com", "acfun.cn", "iqiyi.com", "youku.com",
    "qq.com", "twitch.tv", "reddit.com", "facebook.com", "soundcloud.com",
)

#: 判断「页面是 SPA 空壳」的信号
SPA_MARKERS = (
    'id="app"', "id='app'", 'id="root"', "id='root'", "__NEXT_DATA__",
    "__NUXT__", "window.__INITIAL_STATE__", "enable javascript",
    "请开启javascript", "请启用javascript", "noscript",
)


@dataclass
class CrawlOptions:
    """一次抓取的全部可调参数。"""

    # 渲染策略：auto（按需）/ always / never
    render: str = "auto"
    # yt-dlp 策略：auto（视频站点+嗅探不足时）/ always / never
    use_ytdlp: str = "auto"
    # 只保留同站资源
    same_host_only: bool = False
    # 站内深度爬取
    max_depth: int = 0
    max_pages: int = 1
    # 跟随链接的正则（为空则跟随同站所有链接）
    link_pattern: str = ""
    # 是否把 .ts/.m4s 分片也列为条目
    include_segments: bool = False
    # 无头浏览器
    headless: bool = True
    scroll: bool = True
    browser_timeout: float = 30.0
    wait_after_load: float = 1.5
    # 网络
    timeout: float = 20.0
    retries: int = 2
    proxy: str = ""
    cookies: Dict[str, str] = field(default_factory=dict)
    cookie_string: str = ""
    user_agent: str = ""
    cookies_from_browser: str = ""
    cookie_file: str = ""
    # 过滤
    min_width: int = 0
    types: Sequence[str] = ()      # 只保留指定类型，如 ("image","video")
    dedupe_thumbnails: bool = True

    def wants(self, mtype: MediaType) -> bool:
        if not self.types:
            return True
        return mtype.value in set(self.types)


@dataclass
class CrawlReport:
    """一次抓取的汇总结果。"""

    start_url: str
    pages: List[PageCapture] = field(default_factory=list)
    items: List[MediaItem] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        for page in self.pages:
            if page.title:
                return page.title
        return ""

    def count_by_type(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for item in self.items:
            out[item.type.value] = out.get(item.type.value, 0) + 1
        return out


ProgressCb = Callable[[str, str], None]


class Crawler:
    """抓取主流程。"""

    def __init__(self, options: Optional[CrawlOptions] = None) -> None:
        self.opt = options or CrawlOptions()
        self.fetcher = Fetcher(
            timeout=self.opt.timeout,
            retries=self.opt.retries,
            proxy=self.opt.proxy or None,
            cookies=self.opt.cookies,
            cookie_string=self.opt.cookie_string,
            user_agent=self.opt.user_agent or DEFAULT_UA,
        )

    # ---- 生命周期 ----------------------------------------------------

    async def __aenter__(self) -> "Crawler":
        await self.fetcher.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.fetcher.close()

    # ---- 主入口 ------------------------------------------------------

    async def crawl(
        self, url: str, *, progress: Optional[ProgressCb] = None
    ) -> CrawlReport:
        """抓取入口：自动识别 URL 类型并选择最合适的通道。"""
        report = CrawlReport(start_url=url)
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
            report.start_url = url

        kind = classify(url)

        # 1) 直接就是媒体文件 / 流
        if kind in (MediaType.IMAGE, MediaType.VIDEO, MediaType.AUDIO,
                    MediaType.HLS, MediaType.DASH, MediaType.SEGMENT):
            if progress:
                progress("direct", f"URL 本身即为媒体资源（{kind.label}）")
            item = MediaItem(
                url=url, type=kind, source=Source.TAG, page_url=url,
                ext=url_ext(url), referer=url,
            )
            page = PageCapture(url=url, final_url=url, title=url.rsplit("/", 1)[-1])
            page.items = [item]
            report.pages.append(page)
            report.items = [item]
            return report

        # 2) 普通网页：按深度逐层抓取
        visited: Set[str] = set()
        queue: List[tuple] = [(url, 0)]
        collected: List[MediaItem] = []

        while queue and len(visited) < max(1, self.opt.max_pages):
            current, depth = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            if progress:
                progress("page", f"抓取页面 [{len(visited)}]: {current}")

            try:
                page = await self._crawl_page(current, progress=progress)
            except Exception as exc:
                msg = f"{current} -> {type(exc).__name__}: {exc}"
                report.errors.append(msg)
                if progress:
                    progress("error", msg)
                continue

            report.pages.append(page)
            collected.extend(page.items)

            # 站内深度爬取
            if depth < self.opt.max_depth and len(visited) < self.opt.max_pages:
                for link in self._page_links(page, current)[: self.opt.max_pages * 4]:
                    if link not in visited:
                        queue.append((link, depth + 1))

        report.items = self._merge([collected], report)
        if progress:
            progress("done", f"共发现 {len(report.items)} 个媒体资源")
        return report

    # ---- 单页抓取 ----------------------------------------------------

    async def _crawl_page(
        self, url: str, *, progress: Optional[ProgressCb] = None
    ) -> PageCapture:
        """抓取单个页面：静态 → 渲染 → yt-dlp 三级升级。"""
        page = PageCapture(url=url)
        items: List[MediaItem] = []

        # --- 第一级：静态 HTTP ---
        html = ""
        try:
            resp = await self.fetcher.fetch_page(url)
            page.status = resp.status
            page.final_url = resp.final_url
            page.meta["content_type"] = resp.content_type

            if not resp.is_html:
                # 实际是媒体文件（服务器返回了二进制）
                kind = classify(resp.final_url, resp.content_type)
                if kind not in (MediaType.OTHER, MediaType.IMAGE, MediaType.VIDEO,
                                MediaType.AUDIO, MediaType.HLS, MediaType.DASH):
                    kind = MediaType.OTHER
                if kind != MediaType.OTHER:
                    items.append(MediaItem(
                        url=resp.final_url, type=kind, source=Source.TAG,
                        page_url=url, ext=url_ext(resp.final_url),
                        content_type=resp.content_type, referer=url,
                        size=len(resp.content),
                    ))
                    page.items = items
                    return page
                raise RuntimeError(f"返回的不是网页也不是媒体 ({resp.content_type})")

            html = resp.text
            page.html = html
            # 复用服务端下发的 Cookie
            if resp.cookies:
                self.fetcher.set_cookies(resp.cookies)

            soup_title = _extract_title(html)
            page.title = soup_title

            extractor = Extractor(
                resp.final_url or url,
                same_host_only=self.opt.same_host_only,
                include_segments=self.opt.include_segments,
            )
            items = extractor.extract(html)
            if progress:
                progress("static", f"静态解析得到 {len(items)} 个资源")
        except Exception as exc:
            page.errors.append(f"静态抓取失败: {type(exc).__name__}: {exc}")
            if progress:
                progress("error", f"静态抓取失败: {exc}")

        rendered_items: List[MediaItem] = []
        need_render = self._should_render(url, html, items)

        # --- 第二级：无头浏览器渲染 + 网络嗅探 ---
        if need_render:
            ok, reason = playwright_available()
            if not ok:
                page.errors.append(f"跳过渲染: {reason}")
                if progress:
                    progress("warn", f"无法渲染（{reason}），仅使用静态结果")
            else:
                if progress:
                    progress("render", "使用无头浏览器渲染并嗅探网络请求…")
                try:
                    rendered_items, render_page = await self._render(url, progress=progress)
                    if render_page.html:
                        page.html = render_page.html
                        page.rendered = True
                    if render_page.title and not page.title:
                        page.title = render_page.title
                    if render_page.final_url:
                        page.final_url = render_page.final_url
                    page.meta["rendered"] = True
                    page.meta["requests"] = len(render_page.all_requests)
                    page.errors.extend(render_page.errors)
                    if render_page.cookies:
                        self.fetcher.set_cookies(render_page.cookies)
                        page.meta["cookies"] = len(render_page.cookies)
                except PlaywrightUnavailable as exc:
                    page.errors.append(str(exc))
                except Exception as exc:
                    page.errors.append(f"渲染失败: {type(exc).__name__}: {exc}")

        # --- 第三级：站点专用解析（B 站等）---
        site_items: List[MediaItem] = []
        if BILIBILI_AVAILABLE and bilibili.is_bilibili(url):
            if progress:
                progress("site", "识别到 B 站，调用专用接口解析视频流…")
            try:
                parser = bilibili.BilibiliParser(self.fetcher, page_url=url)
                site_items, info = await parser.parse(url)
                if site_items:
                    if progress:
                        progress("site", f"B 站解析到 {len(site_items)} 个资源")
                    if info.get("title") and not page.title:
                        page.title = info["title"]
                    page.meta["bilibili"] = {
                        "bvid": info.get("bvid"), "duration": info.get("duration"),
                    }
                elif progress:
                    progress("site", "B 站接口未返回可用视频流")
            except Exception as exc:
                page.errors.append(f"B 站解析失败: {type(exc).__name__}: {exc}")

        # --- 第四级：yt-dlp ---
        ytdlp_items: List[MediaItem] = []
        if self._should_use_ytdlp(url, items + rendered_items + site_items):
            if progress:
                progress("ytdlp", "调用 yt-dlp 解析站点视频…")
            try:
                ytdlp_items = await ytdlp_mod.probe_items(
                    url,
                    page_url=url,
                    cookies_from_browser=self.opt.cookies_from_browser,
                    cookie_file=self.opt.cookie_file,
                    proxy=self.opt.proxy,
                )
                if ytdlp_items:
                    if progress:
                        progress("ytdlp", f"yt-dlp 找到 {len(ytdlp_items)} 个资源")
                    if not page.title:
                        page.title = ytdlp_items[0].title or page.title
                elif progress:
                    progress("ytdlp", "yt-dlp 未找到可用资源")
            except Exception as exc:
                page.errors.append(f"yt-dlp 失败: {type(exc).__name__}: {exc}")

        page.items = self._merge([items, rendered_items, site_items, ytdlp_items],
                                 None, page_url=url)
        return page

    async def _render(self, url: str, *, progress: Optional[ProgressCb] = None):
        """执行一次无头渲染，返回 (items, RenderResult)。"""
        renderer = BrowserRenderer(
            headless=self.opt.headless,
            timeout=self.opt.browser_timeout,
            user_agent=self.opt.user_agent,
            proxy=self.opt.proxy or None,
            cookies=self.opt.cookies or None,
            scroll=self.opt.scroll,
            wait_after_load=self.opt.wait_after_load,
            include_segments=self.opt.include_segments,
        )
        result = await renderer.render(url)
        items = list(result.items)

        # 渲染后的 DOM 再解析一次（能拿到 JS 插入的 img/video）
        if result.html:
            from .extractor import Extractor as _Ex

            base = result.final_url or url
            items += _Ex(
                base,
                same_host_only=self.opt.same_host_only,
                include_segments=self.opt.include_segments,
            ).extract(result.html)

        # 内联 script 里直接出现 m3u8/mp4 也捞一遍
        if result.html:
            items += _scan_inline_media(result.html, result.final_url or url)

        if progress:
            progress("render", f"渲染嗅探得到 {len(items)} 个资源")
        return items, result

    # ---- 策略判断 ----------------------------------------------------

    def _host_of(self, url: str) -> str:
        try:
            return urlsplit(url).netloc.lower().split(":")[0]
        except ValueError:
            return ""

    def _is_known(self, url: str, table: Sequence[str]) -> bool:
        host = self._host_of(url)
        return any(host == s or host.endswith("." + s) for s in table)

    def _should_render(self, url: str, html: str, items: Sequence[MediaItem]) -> bool:
        if self.opt.render == "never":
            return False
        if self.opt.render == "always":
            return True
        if not html:
            return True  # 静态抓取失败，值得一试
        if self._is_known(url, KNOWN_JS_SITES):
            return True
        # 内容太少
        if len(items) < AUTO_RENDER_THRESHOLD:
            return True
        # 有 video 标签但没有可用 src，说明源是动态注入的
        if "<video" in html and not any(i.type in (MediaType.VIDEO, MediaType.HLS) for i in items):
            return True
        # SPA 空壳特征
        low = html.lower()
        if any(m.lower() in low for m in SPA_MARKERS) and len(items) < 10:
            return True
        # 静态只找到图片但页面有播放器
        if "player" in low and not any(i.type == MediaType.VIDEO for i in items):
            return True
        return False

    def _should_use_ytdlp(self, url: str, items: Sequence[MediaItem]) -> bool:
        if self.opt.use_ytdlp == "never":
            return False
        if not ytdlp_mod.ytdlp_available():
            return False
        if self.opt.use_ytdlp == "always":
            return True
        if self._is_known(url, KNOWN_VIDEO_SITES):
            return True
        # 已找到 m3u8 或 mp4 就不必再调
        if any(i.type in (MediaType.HLS, MediaType.DASH) for i in items):
            return False
        if any(i.type == MediaType.VIDEO for i in items):
            return False
        return False

    # ---- 链接发现 ----------------------------------------------------

    def _page_links(self, page: PageCapture, base_url: str) -> List[str]:
        """从页面里挑出值得继续爬的站内链接。"""
        if not page.html:
            return []
        pattern = re.compile(self.opt.link_pattern) if self.opt.link_pattern else None
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(page.html, "lxml")
        except Exception:
            return []

        out: List[str] = []
        seen: Set[str] = set()
        for tag in soup.find_all("a"):
            href = tag.get("href")
            if not href or not isinstance(href, str):
                continue
            link = resolve_url(page.final_url or base_url, href)
            if not link or link in seen:
                continue
            # 跳过明显不是内容页的链接
            low = link.lower()
            if any(low.endswith(ext) for ext in (".jpg", ".png", ".gif", ".mp4", ".zip", ".pdf")):
                continue
            if not same_site(link, base_url):
                continue
            if pattern and not pattern.search(link):
                continue
            seen.add(link)
            out.append(link)
        return out

    # ---- 合并与过滤 --------------------------------------------------

    def _merge(
        self,
        groups: Sequence[Sequence[MediaItem]],
        report: Optional[CrawlReport] = None,
        *,
        page_url: str = "",
    ) -> List[MediaItem]:
        """合并多来源条目：去重、按优先级排序、应用过滤规则。"""
        merged: Dict[str, MediaItem] = {}
        for group in groups:
            for item in group:
                if not item.url:
                    continue
                if item.page_url == "":
                    item.page_url = page_url
                if not item.referer:
                    item.referer = item.page_url or page_url

                existing = merged.get(item.url)
                if existing is None:
                    merged[item.url] = item
                else:
                    # 保留更「可靠」的来源：网络嗅探 > 标签解析
                    if existing.source != Source.NETWORK and item.source == Source.NETWORK:
                        existing.source = item.source
                    if item.content_type and not existing.content_type:
                        existing.content_type = item.content_type
                    if item.size and not existing.size:
                        existing.size = item.size
                    # 已判定为流媒体的，升级类型
                    if item.type in (MediaType.HLS, MediaType.DASH) and existing.type not in (
                        MediaType.HLS, MediaType.DASH
                    ):
                        existing.type = item.type

        items = list(merged.values())

        # 应用过滤
        filtered: List[MediaItem] = []
        for item in items:
            if not self.opt.wants(item.type):
                continue
            if self.opt.same_host_only and page_url:
                if not same_site(item.url, page_url):
                    continue
            if self.opt.min_width and item.width and item.width < self.opt.min_width:
                continue
            if item.type == MediaType.SEGMENT and not self.opt.include_segments:
                continue
            item.meta.setdefault("ext_guess", item.ext)
            filtered.append(item)

        # 有 HLS/DASH 时丢掉零散分片，避免噪声
        if any(i.type in (MediaType.HLS, MediaType.DASH) for i in filtered):
            filtered = [i for i in filtered if i.type != MediaType.SEGMENT]

        return sort_items(filtered)


def sort_items(items: Sequence[MediaItem]) -> List[MediaItem]:
    """排序：视频优先、真实资源优先、体积大的优先。

    体积在这里是关键信号——流媒体的**初始化分片**（如 B 站的
    ``xxx-1-30216.m4s``）只有十几 KB，却和真实视频同为 ``video`` 类型，
    若不按体积区分，它们会排在完整视频前面误导用户。
    """
    type_rank = {
        MediaType.VIDEO: 0, MediaType.HLS: 0, MediaType.DASH: 0,
        MediaType.IMAGE: 1, MediaType.AUDIO: 2, MediaType.SEGMENT: 3,
        MediaType.OTHER: 4,
    }
    source_rank = {
        Source.NETWORK: 0, Source.SCRIPT: 1, Source.META: 2, Source.TAG: 3,
        Source.LAZY: 4, Source.SRCSET: 5, Source.ANCHOR: 6, Source.STYLE: 7,
        Source.MANIFEST: 8,
    }

    #: 小于此体积的「视频」几乎必然是初始化分片/占位，降级排序
    TINY_VIDEO_BYTES = 512 * 1024

    def is_real_media(i: MediaItem) -> int:
        """0 = 看起来是完整媒体，1 = 疑似碎片/占位。"""
        if i.type in (MediaType.VIDEO, MediaType.HLS, MediaType.DASH, MediaType.AUDIO):
            # 标了 muxed/needs_audio 的是专用解析器给出的真实流
            if i.meta.get("muxed") or i.meta.get("needs_audio"):
                return 0
            if i.size is not None and i.size < TINY_VIDEO_BYTES:
                return 1
            # 无体积信息且是初始化分片命名（-1-xxxxx.m4s），视为碎片
            if i.size is None and re.search(r"-1-\d{4,}\.m4s", i.url, re.I):
                return 1
        return 0

    return sorted(
        items,
        key=lambda i: (
            0 if i.primary else 1,
            is_real_media(i),
            type_rank.get(i.type, 9),
            source_rank.get(i.source, 9),
            -(i.size or 0),
            -(i.width or 0),
            i.url,
        ),
    )


def _extract_title(html: str) -> str:
    """从 HTML 里取标题。"""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not m:
        return ""
    import html as _html

    title = _html.unescape(m.group(1)).strip()
    title = re.sub(r"\s+", " ", title)
    return title[:200]


_INLINE_STREAM_RE = re.compile(
    r"""["'\(]([^"'\(\)\s]+?\.(?:m3u8|mpd)(?:\?[^"'\)\s<]*)?)["'\)]""", re.I
)


def _scan_inline_media(html: str, base_url: str) -> List[MediaItem]:
    """从 HTML/脚本文本里直接捞 m3u8 / mpd 地址。"""
    out: List[MediaItem] = []
    seen: Set[str] = set()
    for m in _INLINE_STREAM_RE.finditer(html):
        url = resolve_url(base_url, m.group(1))
        if not url or url in seen:
            continue
        seen.add(url)
        kind = classify(url)
        if kind == MediaType.OTHER:
            kind = MediaType.HLS if ".m3u8" in url.lower() else MediaType.DASH
        out.append(
            MediaItem(
                url=url, type=kind, source=Source.SCRIPT, page_url=base_url,
                ext=url_ext(url), referer=base_url,
            )
        )
    return out


__all__ = ["Crawler", "CrawlOptions", "CrawlReport", "sort_items"]
