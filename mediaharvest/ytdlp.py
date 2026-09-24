"""yt-dlp 集成：通用站点适配后端。

yt-dlp 支持 1800+ 站点（YouTube、Bilibili、抖音、TikTok、Twitter/X 等），
当静态解析和网络嗅探都拿不到视频时，用它兜底最有效。

这里只做「探测 + 交给 yt-dlp 下载」，不做格式转码。
未安装 yt-dlp 时所有函数安全降级，不影响其它功能。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple

from .models import MediaItem, MediaType, Source
from .utils import human_duration, url_ext

_YTDLP_CMD: Optional[List[str]] = None


def ytdlp_command() -> Optional[List[str]]:
    """返回可用的 yt-dlp 调用命令（模块方式或可执行文件），不可用返回 None。"""
    global _YTDLP_CMD
    if _YTDLP_CMD is not None:
        return _YTDLP_CMD or None

    # 1) 项目 venv 里的 python -m yt_dlp
    try:
        import yt_dlp  # noqa: F401

        import sys

        _YTDLP_CMD = [sys.executable, "-m", "yt_dlp"]
        return _YTDLP_CMD
    except Exception:
        pass

    # 2) PATH 里的可执行文件
    exe = shutil.which("yt-dlp")
    if exe:
        _YTDLP_CMD = [exe]
        return _YTDLP_CMD

    _YTDLP_CMD = []
    return None


def ytdlp_available() -> bool:
    return ytdlp_command() is not None


# --------------------------------------------------------------------------
# 探测
# --------------------------------------------------------------------------

async def probe(
    url: str,
    *,
    cookies_from_browser: str = "",
    cookie_file: str = "",
    proxy: str = "",
    timeout: float = 60.0,
) -> Optional[Dict[str, Any]]:
    """用 yt-dlp 探测页面信息，返回 info dict；失败返回 None。"""
    cmd = ytdlp_command()
    if not cmd:
        return None

    args = cmd + [
        "--dump-single-json",
        "--no-warnings",
        "--no-playlist",
        "--skip-download",
        "--no-check-certificates",
    ]
    if cookies_from_browser:
        args += ["--cookies-from-browser", cookies_from_browser]
    if cookie_file:
        args += ["--cookies", cookie_file]
    if proxy:
        args += ["--proxy", proxy]
    args.append(url)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        return None

    if proc.returncode != 0 or not stdout:
        return None
    try:
        return json.loads(stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None


def formats_to_items(info: Dict[str, Any], page_url: str) -> List[MediaItem]:
    """把 yt-dlp 的 formats 转成媒体条目（视频取分档，音频取最高码率）。"""
    items: List[MediaItem] = []
    title = (info.get("title") or "").strip()
    duration = info.get("duration")
    thumbnail = info.get("thumbnail") or ""
    webpage = info.get("webpage_url") or page_url

    # 视频格式：优先带音频的完整流
    formats = info.get("formats") or []
    video_candidates: List[Tuple[int, Dict[str, Any]]] = []
    audio_candidates: List[Tuple[int, Dict[str, Any]]] = []

    for fmt in formats:
        if not isinstance(fmt, dict):
            continue
        furl = fmt.get("url")
        if not furl:
            continue
        # 注意：通用提取器给出的 vcodec/acodec 常常是 None（未知），
        # 不能当成 "none"（无此轨），否则会把整个格式丢掉。
        raw_v = fmt.get("vcodec")
        raw_a = fmt.get("acodec")
        v_unknown = raw_v is None
        a_unknown = raw_a is None
        vcodec = (raw_v or "none").lower()
        acodec = (raw_a or "none").lower()
        ext = (fmt.get("ext") or "mp4").lower()
        height = fmt.get("height") or 0
        tbr = fmt.get("tbr") or fmt.get("abr") or 0
        has_video = vcodec not in ("none", "") or v_unknown
        has_audio = acodec not in ("none", "") or a_unknown

        # 音频专用扩展名（m4a/mp3 等）且未标明视频轨 → 归为音频
        if ext in ("m4a", "mp3", "aac", "opus", "ogg", "wav", "flac") and v_unknown:
            has_video = False
            has_audio = True

        if has_video and has_audio:
            video_candidates.append((int(height or 0) * 1000 + int(tbr or 0), fmt))
        elif has_video:
            video_candidates.append((int(height or 0) * 1000 + int(tbr or 0), fmt))
        elif has_audio:
            audio_candidates.append((int(tbr or 0), fmt))

    seen_urls: set = set()

    def add(fmt: Dict[str, Any], mtype: MediaType, label: str) -> None:
        furl = fmt["url"]
        if furl in seen_urls:
            return
        seen_urls.add(furl)
        ext = (fmt.get("ext") or "").lower()
        items.append(
            MediaItem(
                url=furl,
                type=mtype,
                source=Source.NETWORK,
                page_url=webpage,
                ext=ext,
                filename=fmt.get("filename") or "",
                width=fmt.get("width"),
                height=fmt.get("height"),
                duration=duration,
                title=title,
                referer=webpage,
                group=f"ytdlp:{label}",
                primary=(label == "best"),
                meta={
                    "backend": "yt-dlp",
                    "format_id": fmt.get("format_id", ""),
                    "format_note": fmt.get("format_note", ""),
                    "vcodec": fmt.get("vcodec", ""),
                    "acodec": fmt.get("acodec", ""),
                    "extractor": info.get("extractor_key") or info.get("extractor", ""),
                    "is_stream": bool(fmt.get("protocol", "").startswith(("m3u8", "http_dash"))),
                },
            )
        )

    # 取最高画质作为主选，其余作为备选
    video_candidates.sort(key=lambda t: t[0])
    if video_candidates:
        best = video_candidates[-1][1]
        add(best, MediaType.VIDEO, "best")
        for score, fmt in reversed(video_candidates[:-1]):
            if len(items) >= 4:
                break
            add(fmt, MediaType.VIDEO, f"alt{score}")
    elif info.get("url"):
        # 兜底：没有可用 formats 时，用顶层 url/format 字段（通用提取器常见）
        fallback = {
            "url": info["url"],
            "ext": info.get("ext") or info.get("video_ext") or "mp4",
            "width": info.get("width"),
            "height": info.get("height"),
            "format_id": info.get("format_id", ""),
            "vcodec": info.get("vcodec"),
            "acodec": info.get("acodec"),
            "protocol": info.get("protocol", ""),
        }
        is_audio_only = (
            (info.get("ext") in ("m4a", "mp3", "aac", "opus", "ogg", "wav", "flac")
             and info.get("video_ext") in (None, "none"))
            or info.get("vcodec") == "none"
        )
        add(fallback, MediaType.AUDIO if is_audio_only else MediaType.VIDEO, "best")

    audio_candidates.sort(key=lambda t: t[0])
    if audio_candidates:
        add(audio_candidates[-1][1], MediaType.AUDIO, "audio")

    # 缩略图作为图片候选
    thumbs = info.get("thumbnails") or []
    if thumbnail:
        thumbs = list(thumbs) + [{"url": thumbnail}]
    for th in thumbs[:5]:
        if not isinstance(th, dict):
            continue
        turl = th.get("url")
        if not turl or turl in seen_urls:
            continue
        seen_urls.add(turl)
        items.append(
            MediaItem(
                url=turl,
                type=MediaType.IMAGE,
                source=Source.META,
                page_url=webpage,
                ext=url_ext(turl) or "jpg",
                title=title,
                referer=webpage,
                width=th.get("width"),
                height=th.get("height"),
                group="ytdlp:thumb",
                primary=(turl == thumbnail),
                meta={"backend": "yt-dlp", "role": "thumbnail"},
            )
        )

    return items


async def probe_items(
    url: str,
    *,
    page_url: str = "",
    cookies_from_browser: str = "",
    cookie_file: str = "",
    proxy: str = "",
) -> List[MediaItem]:
    """探测并转换为媒体条目列表（失败返回空列表）。"""
    info = await probe(
        url,
        cookies_from_browser=cookies_from_browser,
        cookie_file=cookie_file,
        proxy=proxy,
    )
    if not info:
        return []
    if info.get("_type") == "playlist":
        out: List[MediaItem] = []
        for entry in (info.get("entries") or [])[:50]:
            if isinstance(entry, dict):
                out.extend(formats_to_items(entry, page_url or url))
        return out
    return formats_to_items(info, page_url or url)


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------

async def download(
    url: str,
    out_dir: str,
    *,
    filename: str = "",
    cookies_from_browser: str = "",
    cookie_file: str = "",
    proxy: str = "",
    referer: str = "",
    timeout: float = 1800.0,
    progress_cb: Optional[Any] = None,
) -> Tuple[bool, str, str]:
    """用 yt-dlp 下载，返回 ``(是否成功, 落盘路径, 错误信息)``。"""
    cmd = ytdlp_command()
    if not cmd:
        return False, "", "未安装 yt-dlp"

    os.makedirs(out_dir, exist_ok=True)
    template = os.path.join(out_dir, "%(title).80B [%(id)s].%(ext)s")
    if filename:
        template = os.path.join(out_dir, filename)

    args = cmd + [
        "--no-warnings",
        "--no-playlist",
        "--no-check-certificates",
        "--restrict-filenames" if False else "--no-restrict-filenames",
        "--merge-output-format", "mp4",
        "-o", template,
        "--print", "after_move:filepath",
    ]
    if referer:
        args += ["--referer", referer]
    if cookies_from_browser:
        args += ["--cookies-from-browser", cookies_from_browser]
    if cookie_file:
        args += ["--cookies", cookie_file]
    if proxy:
        args += ["--proxy", proxy]
    args.append(url)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return False, "", "yt-dlp 下载超时"
    except (FileNotFoundError, OSError) as exc:
        return False, "", f"yt-dlp 无法执行: {exc}"

    out_text = stdout.decode("utf-8", "replace").strip()
    err_text = stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        return False, "", f"yt-dlp 失败: {err_text[-400:] or '未知错误'}"

    path = ""
    for line in reversed(out_text.splitlines()):
        line = line.strip()
        if line and os.path.exists(line):
            path = line
            break
    if not path and out_text:
        path = out_text.splitlines()[-1].strip()
    return True, path, ""


__all__ = [
    "download",
    "formats_to_items",
    "probe",
    "probe_items",
    "ytdlp_available",
    "ytdlp_command",
]
