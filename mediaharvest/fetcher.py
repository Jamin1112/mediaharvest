"""HTTP 抓取层：带重试、代理、Cookie、编码嗅探的稳健下载器。"""
from __future__ import annotations

import asyncio
import codecs
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import httpx

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?\s*([a-zA-Z0-9_\-]+)""", re.I)
HTTP_EQUIV_RE = re.compile(
    rb"""<meta[^>]+content=["'][^"']*charset=\s*([a-zA-Z0-9_\-]+)""", re.I
)


def decode_body(body: bytes, content_type: str = "") -> str:
    """尽最大努力把响应体解码成文本，避免中文乱码。"""
    # 1) HTTP 头里的 charset
    candidates: List[str] = []
    m = re.search(r"charset=([a-zA-Z0-9_\-]+)", content_type or "", re.I)
    if m:
        candidates.append(m.group(1))
    # 2) HTML meta 里的 charset（只看前 8KB）
    head = body[:8192]
    for rx in (CHARSET_RE, HTTP_EQUIV_RE):
        mm = rx.search(head)
        if mm:
            try:
                candidates.append(mm.group(1).decode("ascii", "ignore"))
            except Exception:  # pragma: no cover - 防御性
                pass
    # 3) 常见默认
    candidates += ["utf-8", "gb18030", "big5", "latin-1"]

    seen = set()
    for enc in candidates:
        enc = (enc or "").strip().lower()
        if not enc or enc in seen:
            continue
        seen.add(enc)
        try:
            codecs.lookup(enc)
        except LookupError:
            continue
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", "replace")


def looks_like_html(resp: httpx.Response) -> bool:
    ct = (resp.headers.get("content-type") or "").lower()
    if "html" in ct or "xml" in ct:
        return True
    if ct and not any(k in ct for k in ("text/", "json", "javascript")):
        return False
    head = resp.content[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<head" in head


class Fetcher:
    """异步 HTTP 客户端封装。

    统一处理：连接池、重试、UA、Referer、Cookie、代理。
    """

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        retries: int = 2,
        proxy: Optional[str] = None,
        cookies: Optional[Dict[str, str]] = None,
        cookie_string: str = "",
        headers: Optional[Dict[str, str]] = None,
        user_agent: str = DEFAULT_UA,
        verify: bool = True,
    ) -> None:
        self.timeout = timeout
        self.retries = max(0, retries)
        self.user_agent = user_agent
        self.cookies = dict(cookies or {})
        if cookie_string:
            for part in cookie_string.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    self.cookies[k.strip()] = v.strip()
        self.extra_headers = dict(headers or {})
        self._client: Optional[httpx.AsyncClient] = None
        self._proxy = proxy
        self._verify = verify

    # ---- 生命周期 ----------------------------------------------------

    async def __aenter__(self) -> "Fetcher":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def start(self) -> None:
        if self._client is not None:
            return
        limits = httpx.Limits(max_connections=32, max_keepalive_connections=16)
        kwargs: Dict[str, Any] = {
            "timeout": httpx.Timeout(self.timeout, connect=min(10.0, self.timeout)),
            "follow_redirects": True,
            "max_redirects": 10,
            "limits": limits,
            "headers": {"User-Agent": self.user_agent, **self.extra_headers},
            "cookies": self.cookies,
            "verify": self._verify,
        }
        if self._proxy:
            kwargs["proxy"] = self._proxy
        self._client = httpx.AsyncClient(**kwargs)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Fetcher 尚未启动，请先 await fetcher.start()")
        return self._client

    def set_cookies(self, cookies: Dict[str, str]) -> None:
        """注入 Cookie（无头浏览器登录后回传用）。"""
        self.cookies.update(cookies)
        if self._client is not None:
            for k, v in cookies.items():
                self._client.cookies.set(k, v)

    # ---- 请求 --------------------------------------------------------

    async def get(
        self,
        url: str,
        *,
        referer: str = "",
        headers: Optional[Dict[str, str]] = None,
        retries: Optional[int] = None,
    ) -> httpx.Response:
        """带重试的 GET，失败抛出最后一个异常。"""
        attempts = self.retries if retries is None else max(0, retries)
        hdrs: Dict[str, str] = {}
        if referer:
            hdrs["Referer"] = referer
        if headers:
            hdrs.update(headers)
        last: Optional[Exception] = None
        for attempt in range(attempts + 1):
            try:
                resp = await self.client.get(url, headers=hdrs or None)
                # 5xx / 429 值得重试
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < attempts:
                    await resp.aclose()
                    await asyncio.sleep(0.6 * (attempt + 1))
                    continue
                return resp
            except (httpx.TransportError, httpx.HTTPError) as exc:
                last = exc
                if attempt < attempts:
                    await asyncio.sleep(0.6 * (attempt + 1))
                    continue
                raise
        raise last if last else RuntimeError(f"请求失败: {url}")

    async def get_text(
        self, url: str, *, referer: str = "", headers: Optional[Dict[str, str]] = None
    ) -> Tuple[str, str, httpx.Response]:
        resp = await self.get(url, referer=referer, headers=headers)
        ctype = resp.headers.get("content-type", "")
        return decode_body(resp.content, ctype), ctype, resp

    # ---- 页面抓取 ----------------------------------------------------

    async def fetch_page(self, url: str) -> "PageResponse":
        resp = await self.get(url)
        ctype = resp.headers.get("content-type", "")
        text = decode_body(resp.content, ctype) if looks_like_html(resp) or "text" in ctype else ""
        return PageResponse(
            url=url,
            final_url=str(resp.url),
            status=resp.status_code,
            headers=dict(resp.headers),
            text=text,
            content=resp.content,
            cookies=dict(resp.cookies),
        )


class PageResponse:
    """一次页面请求的结果快照。"""

    __slots__ = ("url", "final_url", "status", "headers", "text", "content", "cookies")

    def __init__(
        self,
        *,
        url: str,
        final_url: str,
        status: int,
        headers: Dict[str, str],
        text: str,
        content: bytes,
        cookies: Dict[str, str],
    ) -> None:
        self.url = url
        self.final_url = final_url
        self.status = status
        self.headers = headers
        self.text = text
        self.content = content
        self.cookies = cookies

    @property
    def is_html(self) -> bool:
        ct = (self.headers.get("content-type") or "").lower()
        return "html" in ct or "xml" in ct or self.text.lstrip()[:64].lower().startswith("<!doctype html")

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


def same_site(a: str, b: str) -> bool:
    """粗略判断两个 URL 是否同站（比较注册域的最后两段）。"""
    def reg(host: str) -> str:
        host = host.split(":")[0].lower()
        parts = host.split(".")
        if len(parts) >= 3 and parts[-2] in {"com", "net", "org", "gov", "edu", "co"}:
            return ".".join(parts[-3:])
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    try:
        return reg(urlsplit(a).netloc) == reg(urlsplit(b).netloc)
    except ValueError:
        return False
