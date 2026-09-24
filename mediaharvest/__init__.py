"""mediaharvest —— 网页图片 / 视频 / 音乐抓取下载工具。

支持四类目标，自动选择最合适的通道：

============  ====================================================
静态网页       HTTP 拉取 + HTML 解析（img/video/srcset/懒加载/CSS/脚本）
动态网页       无头浏览器渲染 + 网络请求嗅探
视频平台       yt-dlp 站点适配（YouTube/B站/抖音等 1800+ 站点）
音乐平台       音质选择 + 专辑/歌单整张抓 + ID3/歌词/封面写标签
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

抓音乐（专辑/歌单会整张展开，下载后自动写入标签与歌词）::

    options = CrawlOptions(quality="lossless", types=("audio",))
    report = await crawler.crawl("https://music.163.com/playlist?id=3778678")
"""
from __future__ import annotations

__version__ = "1.1.0"

from .crawler import Crawler, CrawlOptions, CrawlReport, sort_items
from .downloader import Downloader
from .fetcher import Fetcher
from .models import (
    DownloadResult,
    MediaItem,
    MediaType,
    MusicMeta,
    PageCapture,
    Source,
    TrackKind,
)
from .music import (
    MUSIC_PLATFORMS,
    QUALITY_PRESETS,
    classify_music_url,
    is_music_url,
    platform_for,
)
from .utils import classify, human_size, parse_size, sanitize_filename

__all__ = [
    "Crawler",
    "CrawlOptions",
    "CrawlReport",
    "Downloader",
    "DownloadResult",
    "Fetcher",
    "MUSIC_PLATFORMS",
    "MediaItem",
    "MediaType",
    "MusicMeta",
    "PageCapture",
    "QUALITY_PRESETS",
    "Source",
    "TrackKind",
    "classify",
    "classify_music_url",
    "human_size",
    "is_music_url",
    "parse_size",
    "platform_for",
    "sanitize_filename",
    "sort_items",
    "__version__",
]
