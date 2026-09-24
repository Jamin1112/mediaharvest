#!/usr/bin/env python3
"""端到端验证：下载 HLS → 原生转封装 → 用 Chromium 真实播放校验。

这是最强的正确性验证：如果 MP4 的 moov/stbl 结构有误，浏览器会加载失败
或报告错误的时长/分辨率。

用法::
    ./.venv/bin/python verify_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 用中等码流，避免下载几百 MB
HLS_URL = "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"
VARIANT = "848x480"


async def step_download(dest: str) -> str:
    from mediaharvest.fetcher import Fetcher
    from mediaharvest.hls import HlsDownloader

    print("==> 1/4 下载 HLS 分片并拼接")
    async with Fetcher() as fetcher:
        dl = HlsDownloader(fetcher, concurrency=8)
        await dl.download(HLS_URL, dest, variant_hint=VARIANT)
    size = os.path.getsize(dest)
    with open(dest, "rb") as fh:
        head = fh.read(1)
    print(f"    {dest}  {size:,} 字节  首字节={head!r} (TS 同步字节应为 b'G')")
    assert head == b"G", "输出不是合法 TS"
    return dest


def step_probe(ts_path: str) -> None:
    from mediaharvest.remux import probe_ts

    print("==> 2/4 探测 TS 流信息")
    info = probe_ts(ts_path)
    print(f"    {info}")
    return info


def step_remux(ts_path: str, mp4_path: str) -> str:
    from mediaharvest.remux import ts_to_mp4

    print("==> 3/4 原生转封装为 MP4（不使用 ffmpeg）")
    ok = ts_to_mp4(ts_path, mp4_path)
    print(f"    返回: {ok}")
    if not ok:
        print("    转封装失败")
        return ""
    size = os.path.getsize(mp4_path)
    with open(mp4_path, "rb") as fh:
        head = fh.read(12)
    print(f"    {mp4_path}  {size:,} 字节  ftyp={head[4:8]!r}")
    assert head[4:8] == b"ftyp", "输出不是合法 MP4"
    return mp4_path


async def step_playback(mp4_path: str) -> dict:
    """用 Chromium 真实播放，读取元数据。

    必须通过 HTTP 提供文件：Chromium 会以 "URL safety check" 拒绝
    file:// 的媒体加载，直接用 file:// 会误报为解码失败。
    """

    import http.server
    import socketserver
    import threading

    from mediaharvest.browser import ensure_browser_path

    ensure_browser_path()
    from playwright.async_api import async_playwright

    print("==> 4/4 用 Chromium 播放验证")

    serve_dir = os.path.dirname(os.path.abspath(mp4_path))
    name = os.path.basename(mp4_path)

    class _QuietHandler(http.server.SimpleHTTPRequestHandler):
        """静音访问日志，并忽略浏览器提前断开连接造成的 BrokenPipe。"""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=serve_dir, **kwargs)

        def log_message(self, *args, **kwargs):
            pass

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    with socketserver.TCPServer(("127.0.0.1", 0), _QuietHandler) as httpd:
        httpd.allow_reuse_address = True
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        uri = f"http://127.0.0.1:{port}/{name}"
        html = f"""<!DOCTYPE html><html><body>
        <video id="v" src="{uri}" preload="metadata"></video>
        </body></html>"""

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(html)
                try:
                    await page.wait_for_function(
                        "() => { const v = document.getElementById('v');"
                        " return v.readyState >= 1 || v.error; }",
                        timeout=30000,
                    )
                except Exception as exc:
                    print(f"    等待元数据超时: {exc}")
                info = await page.evaluate(
                    """() => {
                        const v = document.getElementById('v');
                        return {
                            width: v.videoWidth, height: v.videoHeight,
                            duration: v.duration, readyState: v.readyState,
                            error: v.error ? (v.error.code + ':' + v.error.message) : null,
                        };
                    }"""
                )
                await browser.close()
        finally:
            httpd.shutdown()

    err = info.get("error")
    if err:
        print(f"    播放错误: {err}")
    print(f"    videoWidth={info['width']}  videoHeight={info['height']}  "
          f"duration={info['duration']:.2f}s  readyState={info['readyState']}")
    return info


async def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mh_verify_")
    ts_path = os.path.join(tmp, "sample.ts")
    mp4_path = os.path.join(tmp, "sample.mp4")
    failed = []

    try:
        await step_download(ts_path)
    except Exception as exc:
        print(f"    下载失败: {type(exc).__name__}: {exc}")
        shutil.rmtree(tmp, ignore_errors=True)
        return 1

    try:
        info = step_probe(ts_path)
        if not info.get("video"):
            failed.append("probe_ts 未识别出视频流")
    except Exception as exc:
        print(f"    探测失败: {type(exc).__name__}: {exc}")
        failed.append("probe_ts 异常")

    remuxed = ""
    try:
        remuxed = step_remux(ts_path, mp4_path)
        if not remuxed:
            failed.append("ts_to_mp4 返回 False")
    except Exception as exc:
        print(f"    转封装异常: {type(exc).__name__}: {exc}")
        failed.append(f"ts_to_mp4 异常: {exc}")

    if remuxed:
        try:
            result = await step_playback(remuxed)
            if result.get("error"):
                failed.append(f"浏览器播放报错: {result['error']}")
            if not result.get("width"):
                failed.append("videoWidth 为 0，说明轨道信息有误")
            if not result.get("duration") or result["duration"] <= 0:
                failed.append("duration 无效")
        except Exception as exc:
            print(f"    播放验证异常: {type(exc).__name__}: {exc}")
            failed.append(f"播放验证异常: {exc}")

    print()
    if failed:
        print("结果: 失败")
        for f in failed:
            print(f"  - {f}")
        code = 1
    else:
        print("结果: 全部通过 ✓")
        code = 0

    # 清理大文件
    shutil.rmtree(tmp, ignore_errors=True)
    print("（临时文件已清理）")
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
