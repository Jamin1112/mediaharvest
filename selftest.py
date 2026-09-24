#!/usr/bin/env python3
"""开发用自检脚本：验证各模块在真实网络下的表现。

用法::

    ./.venv/bin/python selftest.py https://example.com
    ./.venv/bin/python selftest.py --all
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mediaharvest import ytdlp as ytdlp_mod
from mediaharvest.browser import playwright_available
from mediaharvest.crawler import CrawlOptions, Crawler
from mediaharvest.hls import find_ffmpeg, parse_master, parse_media_playlist

# 公开的测试素材源
SAMPLE_MP4 = "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/Big_Buck_Bunny_360_10s_1MB.mp4"
SAMPLE_HLS = "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"
SAMPLE_IMG = "https://picsum.photos/400/300"


def line(tag: str, msg: str) -> None:
    print(f"  [{tag}] {msg}")


async def test_static() -> bool:
    print("\n=== 1. 静态页面解析 ===")
    opts = CrawlOptions(render="never", use_ytdlp="never")
    async with Crawler(opts) as crawler:
        report = await crawler.crawl("https://www.wikipedia.org/")
    print(f"  标题: {report.title[:60]}")
    print(f"  条目: {len(report.items)}")
    for item in report.items[:8]:
        print(f"    - {item.type.value:6s} {item.source.value:8s} {item.url[:80]}")
    if report.errors:
        for err in report.errors[:3]:
            line("err", err)
    return len(report.items) > 0


async def test_direct_media() -> bool:
    print("\n=== 2. 直接媒体 URL ===")
    ok = True
    for url, expect in ((SAMPLE_IMG, "image"), (SAMPLE_MP4, "video"), (SAMPLE_HLS, "hls")):
        opts = CrawlOptions(render="never", use_ytdlp="never")
        async with Crawler(opts) as crawler:
            report = await crawler.crawl(url)
        got = report.items[0].type.value if report.items else "NONE"
        mark = "OK " if got == expect else "FAIL"
        print(f"  {mark} {url[:60]} -> {got} (期望 {expect})")
        ok = ok and got == expect
    return ok


async def test_hls_parse() -> bool:
    print("\n=== 3. HLS 清单解析 ===")
    from mediaharvest.fetcher import Fetcher

    async with Fetcher() as fetcher:
        resp = await fetcher.get(SAMPLE_HLS)
        text = resp.text
        if "#EXT-X-STREAM-INF" in text:
            variants = parse_master(text, SAMPLE_HLS)
            print(f"  master playlist: {len(variants)} 路码流")
            for v in variants:
                print(f"    - {v.describe()}")
            target = variants[-1].url
            resp = await fetcher.get(target)
            text = resp.text
        pl = parse_media_playlist(text, SAMPLE_HLS)
        print(f"  分片: {len(pl.segments)}  时长: {pl.total_duration:.1f}s  加密: {pl.encryption}")
        print(f"  首个分片: {pl.segments[0].url[:80] if pl.segments else '-'}")
        return len(pl.segments) > 0


async def test_hls_download() -> bool:
    print("\n=== 4. HLS 下载 + 拼接 ===")
    import tempfile

    from mediaharvest.fetcher import Fetcher
    from mediaharvest.hls import HlsDownloader

    ffmpeg = find_ffmpeg()
    print(f"  ffmpeg: {ffmpeg or '(未找到，将输出 .ts)'}")

    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, "sample.ts")
        async with Fetcher() as fetcher:
            dl = HlsDownloader(fetcher, concurrency=6, ffmpeg=ffmpeg)
            await dl.download(SAMPLE_HLS, dest)
        size = os.path.getsize(dest) if os.path.exists(dest) else 0
        print(f"  输出: {size} 字节")
        if size == 0:
            return False
        with open(dest, "rb") as fh:
            head = fh.read(4)
        print(f"  文件头: {head!r} (TS 同步字节应为 b'\\x47')")
        if ffmpeg:
            from mediaharvest.hls import remux_to_mp4

            mp4 = await remux_to_mp4(dest, ffmpeg)
            if mp4 and os.path.exists(mp4):
                with open(mp4, "rb") as fh:
                    head = fh.read(12)
                print(f"  remux 后: {os.path.getsize(mp4)} 字节, 头={head[4:8]!r} (ftyp 表示合法 mp4)")
                return head[4:8] == b"ftyp"
        return size > 10000


async def test_render() -> bool:
    print("\n=== 5. 无头渲染 + 网络嗅探 ===")
    ok, reason = playwright_available()
    if not ok:
        print(f"  跳过: {reason}")
        return True
    opts = CrawlOptions(render="always", use_ytdlp="never", scroll=False, wait_after_load=2.0)
    async with Crawler(opts) as crawler:
        report = await crawler.crawl("https://news.ycombinator.com/")
    page = report.pages[0] if report.pages else None
    print(f"  渲染: {page.rendered if page else '-'}  请求数: {page.meta.get('requests') if page else '-'}")
    print(f"  条目: {len(report.items)}  标题: {report.title[:50]}")
    for item in report.items[:5]:
        print(f"    - {item.type.value:6s} {item.source.value:8s} {item.url[:70]}")
    return True


async def test_ytdlp() -> bool:
    print("\n=== 6. yt-dlp 可用性 ===")
    if not ytdlp_mod.ytdlp_available():
        print("  未安装 yt-dlp")
        return False
    print(f"  命令: {' '.join(ytdlp_mod.ytdlp_command() or [])}")
    return True


async def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1

    if args[0] == "--all":
        tests = [
            ("静态解析", test_static),
            ("直接媒体", test_direct_media),
            ("HLS 解析", test_hls_parse),
            ("HLS 下载", test_hls_download),
            ("无头渲染", test_render),
            ("yt-dlp", test_ytdlp),
        ]
    else:
        url = args[0]
        print(f"目标: {url}")
        opts = CrawlOptions()
        async with Crawler(opts) as crawler:
            report = await crawler.crawl(
                url, progress=lambda k, m: print(f"  [{k}] {m}")
            )
        print(f"\n标题: {report.title}")
        print(f"条目: {len(report.items)}  类型分布: {report.count_by_type()}")
        for item in report.items[:20]:
            print(f"  - {item.type.value:6s} {item.source.value:8s} {item.display_name[:40]:42s} {item.url[:70]}")
        for err in report.errors[:5]:
            print(f"  [err] {err}")
        return 0

    results = []
    for name, fn in tests:
        try:
            results.append((name, await fn()))
        except Exception as exc:
            print(f"  [EXCEPTION] {type(exc).__name__}: {exc}")
            import traceback

            traceback.print_exc()
            results.append((name, False))

    print("\n=== 汇总 ===")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
