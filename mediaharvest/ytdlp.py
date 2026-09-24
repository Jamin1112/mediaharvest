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
import re
import shutil
from typing import Any, Dict, List, Optional, Tuple

from . import music as music_mod
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
    playlist: bool = False,
    playlist_end: int = 0,
) -> Optional[Dict[str, Any]]:
    """用 yt-dlp 探测页面信息，返回 info dict；失败返回 None。

    ``playlist=True`` 时允许展开专辑/歌单/歌手这类集合目标——默认的
    ``--no-playlist`` 会让 yt-dlp 只返回集合里的第一首，整张抓取就废了。
    ``playlist_end`` 限制展开条数，避免一个巨型歌单把内存和接口拖垮。
    """
    cmd = ytdlp_command()
    if not cmd:
        return None

    args = cmd + [
        "--dump-single-json",
        "--no-warnings",
        "--skip-download",
        "--no-check-certificates",
        # 网络抖动时重试一次，音乐站点更容易偶发失败
        "--retries", "2",
    ]
    if not playlist:
        args.append("--no-playlist")
    elif playlist_end > 0:
        args += ["--playlist-end", str(playlist_end)]
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


async def probe_error(
    url: str,
    *,
    cookies_from_browser: str = "",
    cookie_file: str = "",
    proxy: str = "",
    timeout: float = 60.0,
    playlist: bool = False,
) -> str:
    """探测失败时取回 yt-dlp 的错误信息（用于给出可诊断的提示）。

    单独一个函数是因为 :func:`probe` 成功路径不该为错误信息付出代价，
    而失败时用户恰恰最需要知道「为什么抓不到」。
    """
    cmd = ytdlp_command()
    if not cmd:
        return "未安装 yt-dlp"
    args = cmd + ["--dump-single-json", "--no-warnings", "--skip-download",
                  "--no-check-certificates", "--retries", "1"]
    if not playlist:
        args.append("--no-playlist")
    if cookies_from_browser:
        args += ["--cookies-from-browser", cookies_from_browser]
    if cookie_file:
        args += ["--cookies", cookie_file]
    if proxy:
        args += ["--proxy", proxy]
    args.append(url)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, FileNotFoundError, OSError) as exc:
        return f"{type(exc).__name__}: {exc}"
    text = (stderr or b"").decode("utf-8", "replace").strip()
    for line in reversed(text.splitlines()):
        if "ERROR" in line:
            # 去掉 "ERROR: " 前缀与多余空白，只留可读的一句
            return re.sub(r"^ERROR:\s*", "", line).strip()[:300]
    return text[-300:] if text else "未知错误"


def formats_to_items(
    info: Dict[str, Any],
    page_url: str,
    *,
    platform: Optional[Any] = None,
    quality: str = "",
    alternatives: bool = True,
) -> List[MediaItem]:
    """把 yt-dlp 的 formats 转成媒体条目。

    传入 ``quality`` 时走**音乐通道**：只保留纯音频轨、按音质档位排序，
    并附带 :class:`~mediaharvest.models.MusicMeta` 供后续写标签。
    不传则维持原有的视频优先行为。

    ``alternatives=False`` 时只给出最优的一条音频流。**集合（专辑/歌单）
    展开时必须关掉备选**：否则每首歌都会多出一条低音质条目，
    一个 100 首的歌单会下成 200 个文件。

    视频通道与音乐通道分开的原因：视频要的是「分辨率最高的合成流」，
    音乐要的是「码率/编码最合适且**不含视频轨**的流」。用同一套逻辑选，
    音乐会被选成体积巨大的 MV 文件。
    """
    items: List[MediaItem] = []
    title = (info.get("title") or "").strip()
    duration = info.get("duration")
    thumbnail = info.get("thumbnail") or ""
    webpage = info.get("webpage_url") or page_url
    formats = info.get("formats") or []
    seen_urls: set = set()

    # ---- 音乐通道 ----------------------------------------------------
    if quality:
        music_meta = music_mod.meta_from_info(info, platform=platform, quality=quality)
        ranked = music_mod.sort_audio_formats(formats, music_mod.get_quality(quality))

        # 只有封面、没有音频流时，也把封面作为图片给出（部分站点如此）
        def add_audio(fmt: Dict[str, Any], is_primary: bool) -> None:
            furl = fmt.get("url")
            if not furl or furl in seen_urls:
                return
            seen_urls.add(furl)
            ext = music_mod.format_ext(fmt)
            label = music_mod.format_label(fmt)
            meta = music_meta
            if not is_primary:
                # 备选音质不应覆盖主选，但标签内容一致
                meta = music_meta
            items.append(
                MediaItem(
                    url=furl,
                    type=MediaType.AUDIO,
                    source=Source.NETWORK,
                    page_url=webpage,
                    ext=ext,
                    duration=duration,
                    title=music_meta.title or title,
                    poster=thumbnail,
                    referer=webpage,
                    group="ytdlp:audio",
                    primary=is_primary,
                    music=meta,
                    meta={
                        "backend": "yt-dlp",
                        "role": "audio",
                        "format_id": fmt.get("format_id", ""),
                        "format_label": label,
                        "extractor": info.get("extractor_key") or info.get("extractor", ""),
                        "quality": quality,
                        "lossless": music_mod.is_lossless(fmt),
                        "abr": music_mod.format_bitrate(fmt),
                        "acodec": fmt.get("acodec", ""),
                    },
                )
            )

        if ranked:
            add_audio(ranked[0], True)
            # 单曲场景额外给一条备选音质；集合场景不给，避免条目翻倍
            if alternatives and len(ranked) > 1:
                add_audio(ranked[1], False)

        for th in _iter_thumbnails(info, thumbnail):
            turl = th.get("url")
            if not turl or turl in seen_urls:
                continue
            seen_urls.add(turl)
            items.append(
                MediaItem(
                    url=turl, type=MediaType.IMAGE, source=Source.META,
                    page_url=webpage, ext=url_ext(turl) or "jpg",
                    title=music_meta.title or title, referer=webpage,
                    width=th.get("width"), height=th.get("height"),
                    group="ytdlp:cover", primary=(turl == thumbnail),
                    meta={"backend": "yt-dlp", "role": "cover"},
                )
            )
        return items

    # ---- 视频通道（原有行为）----------------------------------------
    # 视频格式：优先带音频的完整流
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
    for th in _iter_thumbnails(info, thumbnail):
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


def _iter_thumbnails(info: Dict[str, Any], thumbnail: str) -> List[Dict[str, Any]]:
    """收集缩略图候选（去重、限量），封面优先。"""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    candidates = list(info.get("thumbnails") or [])
    if thumbnail:
        candidates.append({"url": thumbnail})
    # 倒序：yt-dlp 的 thumbnails 一般按分辨率升序，最大的在后面
    for th in reversed(candidates):
        if not isinstance(th, dict):
            continue
        turl = th.get("url")
        if not turl or turl in seen:
            continue
        seen.add(turl)
        out.append(th)
        if len(out) >= 3:
            break
    return out


async def probe_items(
    url: str,
    *,
    page_url: str = "",
    cookies_from_browser: str = "",
    cookie_file: str = "",
    proxy: str = "",
    quality: str = "",
    playlist: bool = False,
    playlist_end: int = 0,
) -> List[MediaItem]:
    """探测并转换为媒体条目列表（失败返回空列表）。

    ``quality`` 非空即开启音乐通道；``playlist`` 允许把专辑/歌单/歌手
    展开成多条曲目。集合展开后每一条都会带上自己的
    :class:`~mediaharvest.models.MusicMeta`（曲目号、专辑名等）。
    """
    info = await probe(
        url,
        cookies_from_browser=cookies_from_browser,
        cookie_file=cookie_file,
        proxy=proxy,
        playlist=playlist,
        playlist_end=playlist_end,
    )
    if not info:
        return []
    platform = music_mod.platform_for(page_url or url)
    if info.get("_type") == "playlist":
        out: List[MediaItem] = []
        entries = info.get("entries") or []
        playlist_title = (info.get("title") or "").strip()
        total = len(entries)
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            # 集合本身的信息要下沉到每一条：条目的 album 常为空（歌单尤其），
            # 没有这一步，整张抓下来的歌会全部丢失专辑归属。
            merged = dict(entry)
            _inherit_collection(merged, info, playlist_title, index, total)
            out.extend(formats_to_items(
                merged, page_url or url, platform=platform, quality=quality,
                alternatives=False,
            ))
        return out
    return formats_to_items(info, page_url or url, platform=platform, quality=quality)


def _inherit_collection(
    entry: Dict[str, Any],
    playlist: Dict[str, Any],
    playlist_title: str,
    index: int,
    total: int,
) -> None:
    """把歌单/专辑级的字段补进单条 entry（就地修改）。"""
    if entry.get("playlist_index") is None:
        entry["playlist_index"] = index + 1
    if entry.get("playlist_count") is None:
        entry["playlist_count"] = total
    if not entry.get("playlist_title") and playlist_title:
        entry["playlist_title"] = playlist_title
    # 专辑名为空时用集合标题兜底，这样 ID3 的 album 字段不会是空的
    if not entry.get("album") and playlist_title:
        entry["album"] = playlist_title
    for key in ("album_artist", "artist", "uploader", "release_date",
                "release_year", "genre", "thumbnail"):
        if not entry.get(key) and playlist.get(key):
            entry[key] = playlist[key]
    if not entry.get("webpage_url") and playlist.get("webpage_url"):
        entry["webpage_url"] = playlist["webpage_url"]


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
    audio_only: bool = False,
    fmt: str = "",
    outtmpl: str = "",
    extra_args: Optional[List[str]] = None,
) -> Tuple[bool, str, str]:
    """用 yt-dlp 下载，返回 ``(是否成功, 落盘路径, 错误信息)``。

    ``audio_only=True`` 时走音乐通道：

    * **不传** ``--merge-output-format mp4``。原实现写死了这一项，会把
      flac/mp3 硬塞进 MP4 容器，直接破坏「无损原样保存」这个前提；
    * 不传 ``--audio-format``，保持站点原始编码，避免无 ffmpeg 时的转码
      失败，也避免有损转有损的二次损失；
    * 不传 ``-x``（``--extract-audio``）：调用方已经用 ``-f`` 选中了
      **纯音频流**，再让 yt-dlp 抽轨纯属多此一举，而且 ``-x`` 会要求
      ffmpeg —— 本项目刻意支持无 ffmpeg 环境，不能平白引入这个依赖。
    """
    cmd = ytdlp_command()
    if not cmd:
        return False, "", "未安装 yt-dlp"

    os.makedirs(out_dir, exist_ok=True)
    template = os.path.join(out_dir, outtmpl) if outtmpl else os.path.join(
        out_dir, "%(title).80B [%(id)s].%(ext)s"
    )
    if filename:
        template = os.path.join(out_dir, filename)

    args = cmd + [
        "--no-warnings",
        "--no-playlist",
        "--no-check-certificates",
        "--no-restrict-filenames",
    ]
    if not audio_only:
        args += ["--merge-output-format", "mp4"]
    if fmt:
        args += ["-f", fmt]
    args += ["-o", template, "--print", "after_move:filepath"]
    if referer:
        args += ["--referer", referer]
    if cookies_from_browser:
        args += ["--cookies-from-browser", cookies_from_browser]
    if cookie_file:
        args += ["--cookies", cookie_file]
    if proxy:
        args += ["--proxy", proxy]
    if extra_args:
        args += list(extra_args)
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
