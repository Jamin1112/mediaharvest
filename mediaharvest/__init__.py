"""mediaharvest —— 网页图片 / 视频抓取下载工具。

支持三类目标，自动选择最合适的通道：

============  ====================================================
静态网页       HTTP 拉取 + HTML 解析（img/video/srcset/懒加载/CSS/脚本）
动态网页       无头浏览器渲染 + 网络请求嗅探
视频平台       yt-dlp 站点适配（YouTube/B站/抖音等 1800+ 站点）
============  ====================================================

流媒体方面内置 HLS(m3u8) 解析：自适应选码、AES-128 解密、分片并发下载与
拼接，并能在无 ffmpeg 的环境下用纯 Python 转封装成 MP4。

快速上手::

    from mediaharvest import Crawler, CrawlOptions, Downloader

    async def main():
        async with Crawler(CrawlOptions()) as crawler:
            report = await crawler.crawl("https://example.com")
            downloader = Downloader(crawler.fetcher, out_dir="downloads")
            await downloader.download_many(report.items)
"""
from __future__ import annotations

__version__ = "1.0.0"

from .crawler import Crawler, CrawlOptions, CrawlReport, sort_items
from .downloader import Downloader
from .fetcher import Fetcher
from .models import (
    DownloadResult,
    MediaItem,
    MediaType,
    PageCapture,
    Source,
)
from .utils import classify, human_size, parse_size, sanitize_filename

__all__ = [
    "Crawler",
    "CrawlOptions",
    "CrawlReport",
    "Downloader",
    "DownloadResult",
    "Fetcher",
    "MediaItem",
    "MediaType",
    "PageCapture",
    "Source",
    "classify",
    "human_size",
    "parse_size",
    "sanitize_filename",
    "sort_items",
    "__version__",
]
