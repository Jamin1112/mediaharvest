"""资源下载器：并发下载、断点续传、重试、去重与命名。

视频类目标（m3u8 / mpd / 需要站点适配的页面）交给 :mod:`mediaharvest.hls`
与 yt-dlp 处理，普通图片/视频走这里的高并发 HTTP 通道。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import httpx

from .control import (
    CancelToken,
    CancelledError,
    ProgressReporter,
    _interruptible_sleep,
)
from .fetcher import Fetcher
from .hls import HlsDownloader, find_ffmpeg, remux_to_mp4
from .models import DownloadResult, MediaItem, MediaType
from .utils import (
    ext_from_content_type,
    filename_from_url,
    ensure_ext,
    human_size,
    parse_size,
    sanitize_filename,
)

ProgressFn = Callable[[DownloadResult], None]

#: 小于此体积且是图片时，视为占位/追踪像素
MIN_IMAGE_BYTES = 512

#: 常见图片 CDN 的「缩略图」参数，去掉可拿原图
THUMB_PARAM_PATTERNS = (
    (re.compile(r"([?&])(w|h|width|height|size|scale|resize)=\d+", re.I), r"\1"),
    (re.compile(r"@\d+w_\d+h[^/]*$", re.I), ""),
    (re.compile(r"\.(jpe?g|png|webp)\.(thumb|small|medium|mini)\.", re.I), r".\1."),
)


def upgrade_image_quality(url: str) -> str:
    """尽力把缩略图 URL 还原成原图 URL（保守策略，只处理明确的模式）。"""
    out = url
    for pattern, repl in THUMB_PARAM_PATTERNS:
        new = pattern.sub(repl, out)
        if new != out:
            out = new
    return out.rstrip("?&")


class Downloader:
    """把 :class:`MediaItem` 落盘到目标目录。"""

    def __init__(
        self,
        fetcher: Fetcher,
        *,
        out_dir: str = "downloads",
        concurrency: int = 8,
        overwrite: bool = False,
        max_size: Optional[int] = None,
        min_size: Optional[int] = None,
        hls_concurrency: int = 8,
        max_segments: int = 20000,
        remux: bool = True,
        keep_ts: bool = False,
        flat: bool = False,
        music_tags: bool = True,
        write_lyrics_file: bool = False,
        token: Optional[CancelToken] = None,
        reporter: Optional[ProgressReporter] = None,
    ) -> None:
        self.fetcher = fetcher
        self.out_dir = out_dir
        self.concurrency = max(1, concurrency)
        self.overwrite = overwrite
        self.max_size = max_size
        self.min_size = min_size
        self.flat = flat
        self.keep_ts = keep_ts
        #: 是否给音乐文件写标签 / 歌词 / 封面
        self.music_tags = music_tags
        #: 是否在音频文件旁额外写一份 .lrc 歌词文件
        self.write_lyrics_file = write_lyrics_file
        #: 取消令牌：可从其它线程触发，下载循环会实时感知
        self.token = token or CancelToken()
        #: 进度上报器：节流后回调，避免刷爆前端
        self.reporter = reporter or ProgressReporter()
        self.ffmpeg = find_ffmpeg()
        self.has_remux = _remux_available()
        self.hls = HlsDownloader(
            fetcher,
            concurrency=hls_concurrency,
            max_segments=max_segments,
            remux=remux,
            ffmpeg=self.ffmpeg,
            token=self.token,
        )
        self._used_paths: Set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def progress(self) -> ProgressReporter:
        """当前进度上报器。"""
        return self.reporter

    # ---- 路径分配 ----------------------------------------------------

    def _subdir_for(self, item: MediaItem) -> str:
        """按类型分目录（flat 模式下全部平铺）。

        音乐条目额外按「专辑」再分一层：音乐库的目录结构本就是
        ``歌手/专辑/曲目``，只按类型堆在 ``audio/`` 里会让整张专辑的歌
        和零散单曲混作一团，导入播放器后无法按专辑浏览。
        """
        if self.flat:
            return self.out_dir
        name = {
            MediaType.IMAGE: "images",
            MediaType.VIDEO: "videos",
            MediaType.AUDIO: "audio",
            MediaType.HLS: "videos",
            MediaType.DASH: "videos",
        }.get(item.type, "others")
        base = os.path.join(self.out_dir, name)
        if item.type == MediaType.AUDIO and item.music is not None:
            album = sanitize_filename(item.music.album, max_len=60)
            if album:
                artist = sanitize_filename(item.music.album_artist or item.music.artist,
                                           max_len=40)
                # 专辑目录带上歌手，避免不同歌手的同名专辑（如各种「精选」）撞在一起
                leaf = f"{artist} - {album}" if artist else album
                return os.path.join(base, leaf)
        return base

    def _music_filename(self, item: MediaItem) -> str:
        """音乐条目用「歌手 - 歌名」命名。

        不能沿用 URL 文件名：音乐 CDN 的地址通常是
        ``.../f6344203897a00ec561d6e1abab44c.mp3`` 这种哈希，
        存下来满屏乱码名，音乐库根本无法识别。
        """
        meta = item.music
        if meta is None:
            return ""
        stem = ""
        if meta.artist and meta.title:
            stem = f"{meta.artist} - {meta.title}"
        elif meta.title:
            stem = meta.title
        elif meta.display:
            stem = meta.display
        if not stem:
            return ""
        name = sanitize_filename(stem, max_len=120)
        return name or ""

    async def _allocate_path(self, item: MediaItem, ext_override: str = "") -> str:
        """分配不冲突的落盘路径。"""
        directory = self._subdir_for(item)
        os.makedirs(directory, exist_ok=True)

        # 音乐条目优先用「歌手 - 歌名」，其余沿用原逻辑
        base = self._music_filename(item)
        if base:
            audio_ext = ext_override or item.ext or ""
            base = ensure_ext(base, audio_ext) if audio_ext else base
        else:
            stem_hint = sanitize_filename(item.title) if item.title else ""
            base = item.filename or filename_from_url(
                item.url, fallback_stem=stem_hint or "media", default_ext=item.ext
            )
            base = sanitize_filename(base) or "media"
            if ext_override:
                base = ensure_ext(base, ext_override)
            elif item.ext and not base.lower().endswith("." + item.ext.lower()):
                base = ensure_ext(base, item.ext)

        async with self._lock:
            path = os.path.join(directory, base)
            if path in self._used_paths or (os.path.exists(path) and not self.overwrite):
                stem, dot, ext = base.rpartition(".")
                if not dot:
                    stem, ext = base, ""
                digest = hashlib.sha1(item.url.encode()).hexdigest()[:6]
                candidate = f"{stem}_{digest}{'.' + ext if ext else ''}"
                path = os.path.join(directory, candidate)
                counter = 2
                while path in self._used_paths or (os.path.exists(path) and not self.overwrite):
                    candidate = f"{stem}_{digest}_{counter}{'.' + ext if ext else ''}"
                    path = os.path.join(directory, candidate)
                    counter += 1
            self._used_paths.add(path)
            return path

    # ---- 单条下载 ----------------------------------------------------

    async def download_one(self, item: MediaItem) -> DownloadResult:
        """下载单个资源，永不抛异常（错误记录在结果里）。"""
        result = DownloadResult(item=item)
        try:
            if item.type in (MediaType.HLS, MediaType.DASH):
                await self._download_stream(item, result)
            else:
                await self._download_file(item, result)
            if result.ok:
                await self._postprocess_music(item, result)
        except CancelledError:
            result.ok = False
            result.skipped = True
            result.error = "已取消"
        except Exception as exc:
            result.ok = False
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    # ---- 音乐后处理 ------------------------------------------------

    async def _postprocess_music(self, item: MediaItem, result: DownloadResult) -> None:
        """给下载好的音乐文件写标签、歌词与封面。

        放在下载成功之后而不是之前：标签写入要依赖最终落盘的文件
        （扩展名可能已按 Content-Type 被纠正过，如 ``.mp3`` 实际是 ``.m4a``）。

        写标签失败**不算下载失败**——音频本身已经拿到了，标签只是增强。
        失败原因记进 ``item.meta``，由上层决定要不要提示用户。
        """
        if not self.music_tags or item.music is None or not result.path:
            return
        if item.type != MediaType.AUDIO:
            return

        try:
            from .tags import TrackTags, write_tags
        except Exception as exc:
            item.meta["tag_error"] = f"标签模块不可用: {exc}"
            return

        meta = item.music
        cover = item.meta.get("cover_bytes") or b""
        tags = TrackTags(
            title=meta.title,
            artist=meta.artist,
            album=meta.album,
            album_artist=meta.album_artist,
            track_number=meta.track_number,
            track_total=meta.track_total,
            disc_number=meta.disc_number,
            disc_total=meta.disc_total,
            year=meta.year,
            date=meta.date,
            genre=meta.genre,
            comment=meta.comment,
            isrc=meta.isrc,
            copyright=meta.copyright,
            lyrics=meta.lyrics,
            cover_data=cover if isinstance(cover, (bytes, bytearray)) else b"",
        )
        if tags.is_empty:
            return

        loop = asyncio.get_event_loop()
        outcome = await loop.run_in_executor(
            None,
            lambda: write_tags(
                result.path, tags,
                embed_cover=bool(cover),
                embed_lyrics=bool(meta.lyrics),
                write_lyrics_file=self.write_lyrics_file,
            ),
        )
        item.meta["tags_written"] = outcome.written
        item.meta["tag_backend"] = outcome.backend
        if not outcome.ok and outcome.error:
            item.meta["tag_error"] = outcome.error
        # 标签写入后文件体积会变，重新统计，避免历史记录的体积对不上
        try:
            if os.path.exists(result.path):
                result.size = os.path.getsize(result.path)
        except OSError:
            pass

    async def _download_stream(self, item: MediaItem, result: DownloadResult) -> None:
        """m3u8 / mpd 流媒体下载。"""
        ext = "mp4" if item.type == MediaType.DASH else ("mp4" if self.has_remux else "ts")
        path = await self._allocate_path(item, ext_override=ext)

        if item.type == MediaType.DASH:
            # DASH 需要 ffmpeg 才能合并；无 ffmpeg 时明确报错而不是产出坏文件
            if not self.ffmpeg:
                result.error = "DASH(.mpd) 需要 ffmpeg 才能下载，请安装 ffmpeg 或改用 HLS 源"
                return
            await self._download_with_ffmpeg(item.url, path, item, result)
            return

        ts_path = path
        if path.lower().endswith(".mp4"):
            ts_path = os.path.splitext(path)[0] + ".ts"

        self.progress.set_phase("downloading", "拉取视频分片")

        def progress(_kind: str, done: int, total: int) -> None:
            item.meta["progress"] = (done, total)
            # 分片进度映射到当前文件的百分比，让进度条能动起来
            self.progress.set_phase("downloading", f"分片 {done}/{total}")
            if total > 0:
                self.progress.state.bytes_total = total
                self.progress.state.bytes_done = done
                self.progress.emit()

        await self.hls.download(
            item.url, ts_path, referer=item.referer or item.page_url, progress=progress
        )
        item.meta.pop("progress", None)

        if not os.path.exists(ts_path) or os.path.getsize(ts_path) == 0:
            result.error = "下载完成但文件为空（可能是直播流或分片被拒绝）"
            return

        final = ts_path
        if path.lower().endswith(".mp4"):
            self.progress.set_phase("remuxing", "转封装为 MP4")
            final = await self._remux_ts(ts_path, path)

        result.ok = True
        result.path = final
        result.size = os.path.getsize(final)
        if final != ts_path:
            item.meta["remuxed"] = True

    async def _remux_ts(self, ts_path: str, mp4_path: str) -> str:
        """把 .ts 转封装成 .mp4。

        优先用功能完整的 ffmpeg；没有就退回内置的纯 Python 转封装实现
        （无需任何外部依赖）。两者都不可用时原样保留 .ts（本身即可播放）。
        """
        if self.ffmpeg:
            # remux_to_mp4 内部会删除源 .ts；需要保留时改用副本
            source = ts_path
            if self.keep_ts:
                source = ts_path + ".src.ts"
                try:
                    shutil.copy2(ts_path, source)
                except OSError:
                    source = ts_path
            remuxed = await remux_to_mp4(source, self.ffmpeg)
            if remuxed:
                if source != ts_path:
                    try:
                        os.replace(source, ts_path)
                    except OSError:
                        pass
                return remuxed

        if self.has_remux:
            try:
                from .remux import ts_to_mp4

                loop = asyncio.get_event_loop()
                ok = await loop.run_in_executor(
                    None, lambda: ts_to_mp4(ts_path, mp4_path)
                )
                if ok and os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
                    if not self.keep_ts:
                        _silent_remove(ts_path)
                    return mp4_path
            except Exception:
                pass
            if os.path.exists(mp4_path) and os.path.getsize(mp4_path) == 0:
                _silent_remove(mp4_path)

        return ts_path

    async def _download_with_ffmpeg(
        self, url: str, path: str, item: MediaItem, result: DownloadResult
    ) -> None:
        """用 ffmpeg 直接抓流（处理 DASH 等复杂情况）。"""
        headers = ""
        if item.referer or item.page_url:
            headers = f"Referer: {item.referer or item.page_url}\r\n"
        proc = await asyncio.create_subprocess_exec(
            self.ffmpeg, "-y", "-loglevel", "error",
            "-headers", headers or "\r\n",
            "-user_agent", self.fetcher.user_agent,
            "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc", path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
            result.error = f"ffmpeg 下载失败: {(stderr or b'').decode('utf-8', 'ignore')[:300]}"
            if os.path.exists(path) and os.path.getsize(path) == 0:
                try:
                    os.remove(path)
                except OSError:
                    pass
            return
        result.ok = True
        result.path = path
        result.size = os.path.getsize(path)

    async def _download_file(self, item: MediaItem, result: DownloadResult) -> None:
        """普通文件下载，支持流式写入与大小限制。

        自带指数退避重试：429/5xx 与网络抖动都会重试，这对图片 CDN 很关键
        （不少站点会对并发请求返回 429）。取消后立即停止，不再重试。
        """
        path = await self._allocate_path(item)
        url = item.url
        referer = item.referer or item.page_url

        attempts = max(0, self.fetcher.retries)
        last_error = ""
        for attempt in range(attempts + 1):
            self.token.check()
            outcome = await self._attempt_download(url, path, referer, item, result)
            if outcome is None:
                return  # 成功，或无需重试的终态
            last_error = outcome
            if attempt < attempts:
                # 429 需要等更久，避免越试越被限流；等待期间也要能被取消
                delay = 1.5 * (attempt + 1) if "429" in outcome else 0.6 * (attempt + 1)
                await _interruptible_sleep(delay, self.token)

        result.ok = False
        result.error = last_error or "下载失败"

    async def _attempt_download(
        self, url: str, path: str, referer: str, item: MediaItem, result: DownloadResult
    ) -> Optional[str]:
        """单次下载尝试。

        返回 ``None`` 表示已达终态（成功或不可重试的失败）；
        返回字符串表示可重试的错误原因。
        """
        tmp_path = path + ".part"
        size = 0
        ctype = ""
        try:
            async with self.fetcher.client.stream(
                "GET", url, headers={"Referer": referer} if referer else None
            ) as resp:
                result.attempts += 1
                if resp.status_code in (429, 500, 502, 503, 504):
                    await resp.aclose()
                    _silent_remove(tmp_path)
                    return f"HTTP {resp.status_code}"
                if resp.status_code >= 400:
                    result.error = f"HTTP {resp.status_code}"
                    return None
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
                clen = resp.headers.get("content-length") or ""
                if clen.isdigit():
                    item.size = int(clen)
                    self.progress.set_total(int(clen))

                # 大文件提前拒绝，避免浪费时间
                if self.max_size and item.size and item.size > self.max_size:
                    result.skipped = True
                    result.error = f"超过体积上限 ({human_size(item.size)})"
                    return None

                with open(tmp_path, "wb") as fh:
                    async for chunk in resp.aiter_bytes(65536):
                        # 每个分块都检查取消 —— 这是「大文件能立即取消」的关键
                        if self.token.cancelled:
                            fh.close()
                            _silent_remove(tmp_path)
                            raise CancelledError(self.token.reason or "用户取消")
                        if not chunk:
                            continue
                        fh.write(chunk)
                        size += len(chunk)
                        self.progress.add_bytes(len(chunk))
                        if self.max_size and size > self.max_size:
                            result.skipped = True
                            result.error = f"超过体积上限 ({human_size(self.max_size)})"
                            break

            if result.skipped:
                _silent_remove(tmp_path)
                return None

            # 内容类型校验：避免把 HTML 错误页存成图片
            if ctype in {"text/html", "application/json"} or (
                ctype.startswith("text/") and item.type != MediaType.OTHER
            ):
                _silent_remove(tmp_path)
                result.error = f"返回的不是媒体 ({ctype or 'unknown'})"
                return None

            if self.min_size and size < self.min_size:
                _silent_remove(tmp_path)
                result.skipped = True
                result.error = f"体积过小 ({human_size(size)})，可能是占位图"
                return None

            if item.type == MediaType.IMAGE and size < MIN_IMAGE_BYTES:
                _silent_remove(tmp_path)
                result.skipped = True
                result.error = f"体积过小 ({size} B)，疑似追踪像素"
                return None

            # 按真实 Content-Type 修正扩展名
            real_ext = ext_from_content_type(ctype)
            final_path = path
            if real_ext and item.type in (MediaType.IMAGE, MediaType.VIDEO, MediaType.AUDIO):
                corrected = ensure_ext(path, real_ext)
                if corrected != path and not os.path.exists(corrected):
                    final_path = corrected

            os.replace(tmp_path, final_path)
            result.ok = True
            result.path = final_path
            result.size = size
            item.content_type = ctype
            return None
        except (httpx.TransportError, httpx.HTTPError) as exc:
            _silent_remove(tmp_path)
            return f"{type(exc).__name__}: {exc}"
        except Exception:
            _silent_remove(tmp_path)
            raise

    # ---- 批量下载 ----------------------------------------------------

    async def download_many(
        self,
        items: Sequence[MediaItem],
        *,
        progress: Optional[ProgressFn] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        on_start: Optional[Callable[[int, MediaItem], None]] = None,
    ) -> List[DownloadResult]:
        """并发下载多条资源，保持输入顺序返回结果。

        ``should_stop`` 是历史遗留的轻量钩子（在文件开始前检查一次）；
        真正能打断大文件的是 ``self.token``。这里把两者桥接起来：
        ``should_stop()`` 返回真时也会触发 ``token.cancel()``，
        保证调用方无论用哪种方式都能取消。
        """
        # 桥接外部停止钩子到取消令牌
        if should_stop is not None and not self.token.cancelled and should_stop():
            self.token.cancel()

        sem = asyncio.Semaphore(self.concurrency)
        results: List[Optional[DownloadResult]] = [None] * len(items)
        self.progress.state.total_files = len(items)

        # 去重：同一 URL 只下一次
        seen: Dict[str, int] = {}
        unique: List[Tuple[int, MediaItem]] = []
        for idx, item in enumerate(items):
            if item.url in seen:
                dup = DownloadResult(item=item, skipped=True, error="重复地址，已跳过")
                results[idx] = dup
                self.progress.finish_file(ok=False, skipped=True)
                if progress:
                    progress(dup)
                continue
            seen[item.url] = idx
            unique.append((idx, item))

        async def worker(idx: int, item: MediaItem) -> None:
            async with sem:
                # 开始前检查：已取消就不再发请求
                if self.token.cancelled or (should_stop and should_stop()):
                    self.token.cancel()
                    results[idx] = DownloadResult(
                        item=item, skipped=True, error="已取消"
                    )
                    self.progress.finish_file(ok=False, skipped=True)
                    return

                self.progress.start_file(idx + 1, item.display_name, item.size)
                if on_start:
                    try:
                        on_start(idx, item)
                    except Exception:
                        pass

                res = await self.download_one(item)
                results[idx] = res
                self.progress.finish_file(
                    ok=res.ok, size=res.size, skipped=res.skipped
                )
                if progress:
                    progress(res)

        await asyncio.gather(*(worker(i, it) for i, it in unique))
        self.progress.flush()
        return [r for r in results if r is not None]


def _silent_remove(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _remux_available() -> bool:
    """检测内置的纯 Python TS→MP4 转封装是否可用。"""
    try:
        from .remux import ts_to_mp4  # noqa: F401

        return True
    except Exception:
        return False


__all__ = ["Downloader", "upgrade_image_quality"]
