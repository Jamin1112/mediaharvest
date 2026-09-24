"""无头浏览器渲染与网络嗅探（Playwright）。

静态解析拿不到的内容靠这里兜底：

  * 等待 JS 执行完成后再取 DOM，解析动态插入的媒体
  * 监听全部网络响应/请求，按 Content-Type 与 URL 特征嗅探真实媒体地址
    （这是抓到抖音、小红书这类「视频地址藏在 XHR 里」的站点的关键）
  * 自动滚动页面触发懒加载
  * 可选的登录等待，登录后把 Cookie 回传给 HTTP 层复用

浏览器二进制默认放在项目内的 ``.playwright-browsers``（沙箱内可写），
可通过 ``PLAYWRIGHT_BROWSERS_PATH`` 覆盖。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from .models import MediaItem, MediaType, Source
from .utils import classify, ext_from_content_type, resolve_url, strip_tracking, url_ext

#: 项目内浏览器目录，保证沙箱环境下也能安装
_DEFAULT_BROWSERS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".playwright-browsers"
)


def ensure_browser_path() -> str:
    """确保 PLAYWRIGHT_BROWSERS_PATH 指向项目内目录。"""
    current = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if current and current not in {"0", ""}:
        return current
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _DEFAULT_BROWSERS_DIR
    return _DEFAULT_BROWSERS_DIR


class PlaywrightUnavailable(RuntimeError):
    """Playwright 或浏览器未安装。"""


def playwright_available() -> Tuple[bool, str]:
    """检查 Playwright 是否可用，返回 (是否可用, 原因)。"""
    try:
        ensure_browser_path()
        from playwright.async_api import async_playwright  # noqa: F401
    except Exception as exc:
        return False, f"未安装 playwright: {exc}"
    path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if path and path not in {"0"} and os.path.isdir(path):
        if not any(e.startswith("chromium") for e in os.listdir(path)):
            return False, f"浏览器未安装到 {path}，请运行: python -m playwright install chromium"
    return True, ""


# --------------------------------------------------------------------------
# 网络嗅探
# --------------------------------------------------------------------------

#: 这些 Content-Type 说明响应体就是媒体
_MEDIA_CT_PREFIX = ("image/", "video/", "audio/")
_MEDIA_CT_EXACT = {
    "application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl",
    "application/dash+xml", "application/octet-stream",
}

#: 这些 URL 特征强烈暗示是媒体（即使没有扩展名）
_URL_HINTS = (
    "/video/", "/videos/", "/media/", "/stream/", "/hls/", "/dash/",
    "/play/", "/resource/", "mime_type=video", "mime_type=image",
)

#: 明显不是媒体的请求，提前排除，减少噪声
_URL_DENY = (
    ".js", ".css", ".woff", ".woff2", ".ttf", ".html", ".htm",
    "/api/", "/log", "/report", "/beacon", "/collect", "/track",
    "google-analytics", "doubleclick", "googletagmanager", "sentry", "baidu.com/hm",
    ".json", ".xml", "favicon",
)


class Sniffer:
    """记录浏览器发出的请求，挑出媒体资源。"""

    def __init__(self, page_url: str, *, include_segments: bool = False) -> None:
        self.page_url = page_url
        self.include_segments = include_segments
        self.items: Dict[str, MediaItem] = {}
        self._content_types: Dict[str, str] = {}
        self._sizes: Dict[str, int] = {}
        self.all_requests: List[str] = []

    # ---- Playwright 回调 --------------------------------------------

    async def on_response(self, response: Any) -> None:
        try:
            url = response.url
        except Exception:
            return
        if not url or url.startswith(("data:", "blob:")):
            # blob: 无法直接下载，但可从页面里拿到（见 extract_blob_sources）
            return
        self.all_requests.append(url)
        if any(d in url.lower() for d in _URL_DENY):
            return

        headers = {}
        try:
            headers = await response.all_headers()
        except Exception:
            pass
        ctype = (headers.get("content-type") or "").split(";")[0].strip().lower()
        if not ctype:
            try:
                ctype = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            except Exception:
                ctype = ""

        self._content_types[url] = ctype
        try:
            clen = headers.get("content-length") or ""
            if clen.isdigit():
                self._sizes[url] = int(clen)
        except Exception:
            pass

        kind = self._judge(url, ctype)
        if kind is None:
            return
        if kind == MediaType.SEGMENT and not self.include_segments:
            return

        # 由分片/清单推断出的主媒体地址已在 crawler 里处理
        self.items.setdefault(
            url,
            MediaItem(
                url=url,
                type=kind,
                source=Source.NETWORK,
                page_url=self.page_url,
                ext=url_ext(url) or ext_from_content_type(ctype),
                content_type=ctype,
                size=self._sizes.get(url),
                referer=self.page_url,
            ),
        )

    def on_request(self, request: Any) -> None:
        """兜底：有些媒体在请求发出后立刻被取消（如 preload）。"""
        try:
            url = request.url
        except Exception:
            return
        if not url or url.startswith(("data:", "blob:")):
            return
        self.all_requests.append(url)

    def _judge(self, url: str, ctype: str) -> Optional[MediaType]:
        low = url.lower()
        if any(d in low for d in _URL_DENY):
            return None

        # 1) Content-Type 最可靠
        if ctype.startswith(_MEDIA_CT_PREFIX):
            kind = classify(url, ctype)
            return MediaType.OTHER if kind == MediaType.OTHER else kind
        if ctype in {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl"}:
            return MediaType.HLS
        if ctype == "application/dash+xml":
            return MediaType.DASH
        # octet-stream 需结合 URL 判断
        if ctype == "application/octet-stream":
            kind = classify(url)
            return kind if kind != MediaType.OTHER else None

        # 2) URL 特征
        kind = classify(url)
        if kind != MediaType.OTHER:
            return kind
        if any(h in low for h in _URL_HINTS):
            # 无扩展名但路径像媒体，标为 other 由用户确认
            return None
        return None

    # ---- 结果 --------------------------------------------------------

    def media_items(self) -> List[MediaItem]:
        return list(self.items.values())


# --------------------------------------------------------------------------
# 渲染器
# --------------------------------------------------------------------------

class BrowserRenderer:
    """用 Chromium 渲染页面并嗅探媒体。"""

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout: float = 30.0,
        user_agent: str = "",
        proxy: Optional[str] = None,
        cookies: Optional[Dict[str, str]] = None,
        scroll: bool = True,
        wait_after_load: float = 1.5,
        include_segments: bool = False,
        block_media: bool = False,
    ) -> None:
        self.headless = headless
        self.timeout = timeout
        self.user_agent = user_agent
        self.proxy = proxy
        self.cookies = dict(cookies or {})
        self.scroll = scroll
        self.wait_after_load = wait_after_load
        self.include_segments = include_segments
        self.block_media = block_media

    async def render(self, url: str) -> "RenderResult":
        """渲染页面，返回最终 HTML、Cookie 与嗅探结果。"""
        ok, reason = playwright_available()
        if not ok:
            raise PlaywrightUnavailable(reason)

        ensure_browser_path()
        from playwright.async_api import async_playwright

        result = RenderResult(url=url)
        async with async_playwright() as pw:
            launch_args: Dict[str, Any] = {
                "headless": self.headless,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--mute-audio",
                ],
            }
            if self.proxy:
                launch_args["proxy"] = {"server": self.proxy}
            browser = await pw.chromium.launch(**launch_args)
            try:
                context_args: Dict[str, Any] = {
                    "viewport": {"width": 1440, "height": 900},
                    "locale": "zh-CN",
                    "ignore_https_errors": True,
                }
                if self.user_agent:
                    context_args["user_agent"] = self.user_agent
                context = await browser.new_context(**context_args)

                # 反自动化检测（尽量温和，不破坏正常站点）
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )

                if self.cookies:
                    try:
                        await context.add_cookies([
                            {"name": k, "value": v, "url": url} for k, v in self.cookies.items()
                        ])
                    except Exception as exc:
                        result.errors.append(f"注入 Cookie 失败: {exc}")

                page = await context.new_page()
                sniffer = Sniffer(url, include_segments=self.include_segments)

                if self.block_media:
                    # 只嗅探不下载：拦截媒体请求以加速（保留 URL 记录）
                    async def route_handler(route: Any) -> None:
                        try:
                            sniffer.all_requests.append(route.request.url)
                        except Exception:
                            pass
                        await route.abort()

                    try:
                        await page.route(
                            "**/*.{png,jpg,jpeg,gif,webp,avif,svg,mp4,webm,m3u8,ts}",
                            route_handler,
                        )
                    except Exception:
                        pass

                page.on("response", lambda r: asyncio.create_task(_safe(sniffer.on_response(r))))
                page.on("request", sniffer.on_request)

                try:
                    response = await page.goto(
                        url, wait_until="domcontentloaded", timeout=self.timeout * 1000
                    )
                    if response is not None:
                        result.status = response.status
                        result.final_url = response.url
                except Exception as exc:
                    result.errors.append(f"页面加载异常: {type(exc).__name__}: {exc}")

                # 等待网络安静（长轮询站点会超时，忽略即可）
                try:
                    await page.wait_for_load_state("networkidle", timeout=min(8000, self.timeout * 1000))
                except Exception:
                    pass

                if self.scroll:
                    await self._auto_scroll(page, result)

                if self.wait_after_load:
                    await asyncio.sleep(self.wait_after_load)

                # 触发播放器加载真实视频源
                await self._kick_players(page, result)

                try:
                    result.html = await page.content()
                except Exception as exc:
                    result.errors.append(f"读取 DOM 失败: {exc}")
                try:
                    result.title = await page.title()
                except Exception:
                    pass

                # 收集 Cookie 供 HTTP 层复用
                try:
                    for ck in await context.cookies():
                        result.cookies[ck["name"]] = ck["value"]
                except Exception:
                    pass

                result.items = sniffer.media_items()
                result.all_requests = sniffer.all_requests
                try:
                    await context.close()
                except Exception:
                    pass
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
        return result

    async def _auto_scroll(self, page: Any, result: "RenderResult", rounds: int = 6) -> None:
        """滚动到底部触发懒加载，同时记录高度变化。"""
        try:
            last_height = 0
            for _ in range(rounds):
                height = await page.evaluate(
                    "() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
                )
                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(0.8)
                if height == last_height:
                    break
                last_height = height
            await page.evaluate("() => window.scrollTo(0, 0)")
        except Exception as exc:
            result.errors.append(f"自动滚动失败: {exc}")

    async def _kick_players(self, page: Any, result: "RenderResult") -> None:
        """尝试让 <video> 真正开始加载，从而暴露真实媒体地址。"""
        try:
            await page.evaluate(
                """() => {
                    document.querySelectorAll('video, audio').forEach(el => {
                        try {
                            el.muted = true;
                            if (el.pause) el.pause();
                            const p = el.play && el.play();
                            if (p && p.catch) p.catch(() => {});
                        } catch (e) {}
                    });
                }"""
            )
            await asyncio.sleep(1.0)
        except Exception:
            pass

        # 提取 blob: 视频的真实源（部分站点用 MSE，源在 JS 变量里）
        try:
            sources = await page.evaluate(
                """() => {
                    const out = [];
                    document.querySelectorAll('video, audio').forEach(el => {
                        if (el.currentSrc) out.push(el.currentSrc);
                        if (el.src) out.push(el.src);
                        el.querySelectorAll('source').forEach(s => { if (s.src) out.push(s.src); });
                    });
                    return out;
                }"""
            )
            result.blob_sources = [s for s in (sources or []) if isinstance(s, str)]
        except Exception:
            pass


async def _safe(coro: Any) -> None:
    """吞掉回调里的异常，避免影响主流程。"""
    try:
        await coro
    except Exception:
        pass


class RenderResult:
    """渲染结果。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.final_url = url
        self.title = ""
        self.html = ""
        self.status = 0
        self.items: List[MediaItem] = []
        self.cookies: Dict[str, str] = {}
        self.all_requests: List[str] = []
        self.blob_sources: List[str] = []
        self.errors: List[str] = []


__all__ = [
    "BrowserRenderer",
    "PlaywrightUnavailable",
    "RenderResult",
    "Sniffer",
    "ensure_browser_path",
    "playwright_available",
]
