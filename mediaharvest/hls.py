"""HLS (m3u8) 原生解析与下载。

自行实现而不依赖 ffmpeg，这样在没装 ffmpeg 的机器上也能抓到视频：

  * 解析 master playlist，按带宽/分辨率自动选最优码流
  * 解析 media playlist，支持 ``EXT-X-BYTERANGE``、``EXT-X-MAP``(fMP4)
  * 支持 ``AES-128`` 加密分片解密（IV 缺省时按 media sequence 推导）
  * 并发下载分片 → 顺序拼接成 ``.ts`` / ``.mp4``
  * 若系统存在 ffmpeg，则额外 remux 成 mp4 容器（可选，非必需）
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import struct
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from .fetcher import Fetcher
from .models import MediaItem, MediaType, Source

try:  # pragma: no cover - 环境相关
    from Crypto.Cipher import AES

    _HAS_AES = True
except Exception:  # pragma: no cover
    _HAS_AES = False


# --------------------------------------------------------------------------
# 清单解析
# --------------------------------------------------------------------------

@dataclass
class Variant:
    """master playlist 中的一路码流。"""

    url: str
    bandwidth: int = 0
    resolution: str = ""
    codecs: str = ""
    name: str = ""

    @property
    def height(self) -> int:
        m = re.search(r"(\d+)\s*[xX]\s*(\d+)", self.resolution or "")
        return int(m.group(2)) if m else 0

    def describe(self) -> str:
        bits = []
        if self.resolution:
            bits.append(self.resolution)
        if self.bandwidth:
            bits.append(f"{self.bandwidth // 1000} kbps")
        return " / ".join(bits) or self.name or self.url.rsplit("/", 1)[-1]


@dataclass
class Segment:
    """一个媒体分片。"""

    url: str
    duration: float = 0.0
    key_url: str = ""
    iv: Optional[bytes] = None
    byte_range: Optional[Tuple[int, int]] = None  # (offset, length)
    seq: int = 0


@dataclass
class MediaPlaylist:
    """media playlist 解析结果。"""

    url: str
    segments: List[Segment] = field(default_factory=list)
    init_segment: Optional[Segment] = None
    is_endlist: bool = False
    total_duration: float = 0.0
    encryption: str = "NONE"

    @property
    def is_fmp4(self) -> bool:
        return self.init_segment is not None or any(
            s.url.lower().endswith((".m4s", ".mp4")) for s in self.segments[:3]
        )


def _attr_map(line: str) -> Dict[str, str]:
    """解析 ``KEY=VALUE,KEY="VALUE"`` 形式的属性串。"""
    out: Dict[str, str] = {}
    for m in re.finditer(r'([A-Za-z0-9\-]+)=("[^"]*"|[^,]*)', line):
        key = m.group(1).strip().upper()
        value = m.group(2).strip().strip('"')
        out[key] = value
    return out


def parse_master(text: str, base_url: str) -> List[Variant]:
    """解析 master playlist，返回按质量升序的码流列表。"""
    variants: List[Variant] = []
    lines = [l.strip() for l in text.splitlines()]
    pending: Optional[Dict[str, str]] = None
    for line in lines:
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = _attr_map(line.split(":", 1)[1])
            continue
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = _attr_map(line.split(":", 1)[1])
            uri = attrs.get("URI")
            if uri and attrs.get("TYPE") == "AUDIO" and not variants:
                variants.append(Variant(url=urljoin(base_url, uri), name=attrs.get("NAME", "audio")))
            continue
        if line.startswith("#"):
            continue
        if pending is not None:
            try:
                bandwidth = int(pending.get("BANDWIDTH") or pending.get("AVERAGE-BANDWIDTH") or 0)
            except ValueError:
                bandwidth = 0
            variants.append(
                Variant(
                    url=urljoin(base_url, line),
                    bandwidth=bandwidth,
                    resolution=pending.get("RESOLUTION", ""),
                    codecs=pending.get("CODECS", ""),
                    name=pending.get("NAME", ""),
                )
            )
            pending = None
    variants.sort(key=lambda v: (v.height, v.bandwidth))
    return variants


def parse_media_playlist(text: str, base_url: str) -> MediaPlaylist:
    """解析 media playlist，含加密与字节范围信息。"""
    playlist = MediaPlaylist(url=base_url)
    lines = text.splitlines()
    duration = 0.0
    seq = 0
    key_url = ""
    iv: Optional[bytes] = None
    encryption = "NONE"
    next_range: Optional[Tuple[int, int]] = None
    offset_cursor = 0
    pending_duration: Optional[float] = None
    media_seq = 0

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_seq = int(line.split(":", 1)[1].strip())
                seq = media_seq
            except ValueError:
                pass
            continue
        if line.startswith("#EXT-X-KEY:"):
            attrs = _attr_map(line.split(":", 1)[1])
            method = (attrs.get("METHOD") or "NONE").upper()
            encryption = method
            if method in {"AES-128", "SAMPLE-AES"}:
                uri = attrs.get("URI", "")
                key_url = urljoin(base_url, uri) if uri else ""
                iv_hex = attrs.get("IV", "")
                if iv_hex:
                    iv = bytes.fromhex(iv_hex.lower().removeprefix("0x").zfill(32))
                else:
                    iv = None
            else:
                key_url, iv = "", None
            continue
        if line.startswith("#EXT-X-MAP:"):
            attrs = _attr_map(line.split(":", 1)[1])
            uri = attrs.get("URI")
            if uri:
                rng = None
                if attrs.get("BYTERANGE"):
                    rng = _parse_byterange(attrs["BYTERANGE"], offset_cursor)
                playlist.init_segment = Segment(
                    url=urljoin(base_url, uri),
                    key_url=key_url,
                    iv=iv,
                    byte_range=rng,
                    seq=seq,
                )
            continue
        if line.startswith("#EXT-X-BYTERANGE:"):
            next_range = _parse_byterange(line.split(":", 1)[1].strip(), offset_cursor)
            continue
        if line.startswith("#EXTINF:"):
            try:
                pending_duration = float(line.split(":", 1)[1].split(",")[0].strip())
            except ValueError:
                pending_duration = 0.0
            continue
        if line.startswith("#EXT-X-ENDLIST"):
            playlist.is_endlist = True
            continue
        if line.startswith("#"):
            continue

        # 媒体分片行
        url = urljoin(base_url, line)
        seg_iv = iv
        if seg_iv is None and key_url:
            # 未显式给出 IV 时，按规范用 media sequence number 作为 IV
            seg_iv = seq.to_bytes(16, "big")
        playlist.segments.append(
            Segment(
                url=url,
                duration=pending_duration or 0.0,
                key_url=key_url,
                iv=seg_iv,
                byte_range=next_range,
                seq=seq,
            )
        )
        if next_range:
            offset_cursor = next_range[0] + next_range[1]
        next_range = None
        seq += 1
        if pending_duration:
            duration += pending_duration
        pending_duration = None

    playlist.total_duration = duration
    playlist.encryption = encryption
    return playlist


def _parse_byterange(value: str, default_offset: int) -> Optional[Tuple[int, int]]:
    """解析 ``length@offset``。"""
    try:
        if "@" in value:
            length_s, offset_s = value.split("@", 1)
            return int(offset_s), int(length_s)
        return default_offset, int(value)
    except (ValueError, AttributeError):
        return None


def is_master_playlist(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text or "#EXT-X-MEDIA:" in text


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------

ProgressFn = Callable[[str, int, int], None]


class HlsDownloader:
    """把一个 m3u8 抓成单个视频文件。"""

    def __init__(
        self,
        fetcher: Fetcher,
        *,
        concurrency: int = 8,
        max_segments: int = 20000,
        remux: bool = True,
        ffmpeg: Optional[str] = None,
    ) -> None:
        self.fetcher = fetcher
        self.concurrency = max(1, concurrency)
        self.max_segments = max_segments
        self.remux = remux
        self.ffmpeg = ffmpeg if ffmpeg is not None else find_ffmpeg()
        self._key_cache: Dict[str, bytes] = {}

    async def download(
        self,
        url: str,
        dest: str,
        *,
        referer: str = "",
        progress: Optional[ProgressFn] = None,
        variant_hint: str = "",
    ) -> str:
        """下载 m3u8 到 ``dest``（.ts 或 .mp4），返回实际落盘路径。"""
        text = await self._fetch_playlist_text(url, referer)

        if is_master_playlist(text):
            variants = parse_master(text, url)
            if not variants:
                raise RuntimeError("master playlist 中没有可用码流")
            chosen = variants[-1]  # 最高质量
            if variant_hint:
                for v in variants:
                    if variant_hint in (v.resolution, v.name, str(v.bandwidth)):
                        chosen = v
                        break
            text = await self._fetch_playlist_text(chosen.url, referer)
            url = chosen.url

        playlist = parse_media_playlist(text, url)
        if not playlist.segments:
            raise RuntimeError("media playlist 中没有分片")
        if len(playlist.segments) > self.max_segments:
            raise RuntimeError(
                f"分片数量过多({len(playlist.segments)})，可能是直播流；"
                f"可用 --max-segments 调整上限"
            )
        if playlist.encryption == "SAMPLE-AES":
            raise RuntimeError("SAMPLE-AES 加密需 ffmpeg 或专用工具处理")
        if playlist.encryption == "AES-128" and not _HAS_AES:
            raise RuntimeError("缺少 pycryptodome，无法解密 AES-128 分片")

        return await self._download_segments(
            playlist, dest, referer=referer, progress=progress
        )

    async def _fetch_playlist_text(self, url: str, referer: str) -> str:
        resp = await self.fetcher.get(url, referer=referer or url)
        if resp.status_code >= 400:
            raise RuntimeError(f"获取播放列表失败 HTTP {resp.status_code}: {url}")
        return resp.text

    async def _get_key(self, key_url: str, referer: str) -> bytes:
        if key_url in self._key_cache:
            return self._key_cache[key_url]
        resp = await self.fetcher.get(key_url, referer=referer)
        resp.raise_for_status()
        key = resp.content
        if len(key) not in (16, 24, 32):
            raise RuntimeError(f"密钥长度异常({len(key)} 字节)")
        self._key_cache[key_url] = key
        return key

    async def _download_segments(
        self,
        playlist: MediaPlaylist,
        dest: str,
        *,
        referer: str,
        progress: Optional[ProgressFn],
    ) -> str:
        segments = playlist.segments
        total = len(segments)
        results: List[Optional[bytes]] = [None] * total
        done = 0
        lock = asyncio.Lock()
        sem = asyncio.Semaphore(self.concurrency)
        errors: List[str] = []

        # 预取密钥，避免并发时重复请求
        for key_url in {s.key_url for s in segments if s.key_url}:
            try:
                await self._get_key(key_url, referer)
            except Exception as exc:
                raise RuntimeError(f"获取解密密钥失败: {exc}") from exc

        async def one(index: int, seg: Segment) -> None:
            nonlocal done
            async with sem:
                if errors:
                    return
                try:
                    data = await self._fetch_segment(seg, referer)
                except Exception as exc:
                    async with lock:
                        errors.append(f"分片 {index} 失败: {exc}")
                    return
                results[index] = data
                async with lock:
                    done += 1
                    if progress:
                        progress("hls", done, total)

        await asyncio.gather(*(one(i, s) for i, s in enumerate(segments)))

        if errors:
            raise RuntimeError(errors[0])

        # 顺序拼接落盘
        os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
        with open(dest, "wb") as fh:
            if playlist.init_segment is not None:
                try:
                    init = await self._fetch_segment(playlist.init_segment, referer)
                    fh.write(init)
                except Exception:
                    pass
            for chunk in results:
                if chunk:
                    fh.write(chunk)

        return dest

    async def _fetch_segment(self, seg: Segment, referer: str) -> bytes:
        headers = {}
        if seg.byte_range:
            offset, length = seg.byte_range
            headers["Range"] = f"bytes={offset}-{offset + length - 1}"
        resp = await self.fetcher.get(seg.url, referer=referer, headers=headers, retries=3)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        data = resp.content
        if seg.url.lower().split("?")[0].endswith((".m3u8",)) or data.lstrip()[:7] == b"#EXTM3U":
            raise RuntimeError("分片返回了播放列表而非媒体数据")
        if seg.key_url and seg.iv is not None:
            key = await self._get_key(seg.key_url, referer)
            data = _aes128_cbc_decrypt(data, key, seg.iv)
        return data


def _aes128_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC 解密并去掉 PKCS7 填充。"""
    if not _HAS_AES:  # pragma: no cover
        raise RuntimeError("缺少 pycryptodome")
    cipher = AES.new(key, AES.MODE_CBC, iv[:16])
    plain = cipher.decrypt(data)
    if plain:
        pad = plain[-1]
        if 1 <= pad <= 16 and plain.endswith(bytes([pad]) * pad):
            plain = plain[:-pad]
    return plain


# --------------------------------------------------------------------------
# ffmpeg（可选）
# --------------------------------------------------------------------------

def _ffmpeg_can_mux_mp4(binary: str) -> bool:
    """检测 ffmpeg 是否具备 mp4 封装与 aac 比特流过滤能力。

    Playwright 附带的 ffmpeg 是精简构建（--disable-everything，只启用了
    webm/vp8），无法完成 TS→MP4 转封装，必须排除，否则会静默产出坏文件。
    """
    if not binary:
        return False
    import subprocess

    try:
        out = subprocess.run(
            [binary, "-hide_banner", "-muxers"],
            capture_output=True, timeout=15,
        ).stdout.decode("utf-8", "replace")
        if " mp4" not in out:
            return False
        bsfs = subprocess.run(
            [binary, "-hide_banner", "-bsfs"],
            capture_output=True, timeout=15,
        ).stdout.decode("utf-8", "replace")
        # aac_adtstoasc 用于把 ADTS AAC 转成 MP4 所需的 ASC 格式
        return "aac_adtstoasc" in bsfs
    except Exception:
        return False


_FFMPEG_CACHE: Dict[str, str] = {}


def find_ffmpeg() -> str:
    """查找一个**功能完整**的 ffmpeg，找不到返回空串。

    查找顺序：环境变量 → 项目 bin/ → PATH → Playwright 自带副本。
    每个候选都会做能力探测，避免选中 Playwright 那种精简构建。
    找不到时上层会改用纯 Python 的 remux 实现，功能不受影响。
    """
    if "path" in _FFMPEG_CACHE:
        return _FFMPEG_CACHE["path"]

    candidates: List[str] = []
    env = os.environ.get("FFMPEG_BINARY")
    if env and os.path.exists(env):
        candidates.append(env)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.path.join(root, "bin", "ffmpeg"))

    system = shutil.which("ffmpeg")
    if system:
        candidates.append(system)

    browsers_dir = os.environ.get(
        "PLAYWRIGHT_BROWSERS_PATH", os.path.join(root, ".playwright-browsers")
    )
    try:
        if os.path.isdir(browsers_dir):
            for entry in sorted(os.listdir(browsers_dir)):
                if not entry.startswith("ffmpeg"):
                    continue
                candidate_dir = os.path.join(browsers_dir, entry)
                for name in ("ffmpeg-mac", "ffmpeg-linux", "ffmpeg-win64.exe", "ffmpeg"):
                    candidates.append(os.path.join(candidate_dir, name))
    except OSError:
        pass

    for candidate in candidates:
        if os.path.exists(candidate) and _ffmpeg_can_mux_mp4(candidate):
            _FFMPEG_CACHE["path"] = candidate
            return candidate

    _FFMPEG_CACHE["path"] = ""
    return ""


def probe_duration(path: str, ffmpeg: str = "") -> Optional[float]:
    """尽力探测媒体时长（TS 里读 PTS，失败则用 ffprobe）。"""
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            out = os.popen(
                f'"{ffprobe}" -v error -show_entries format=duration '
                f'-of default=noprint_wrappers=1:nokey=1 "{path}"'
            ).read().strip()
            return float(out)
        except Exception:
            pass
    return None


async def remux_to_mp4(ts_path: str, ffmpeg: str) -> Optional[str]:
    """用 ffmpeg 把 .ts 转封装成 .mp4（不重新编码）。"""
    if not ffmpeg:
        return None
    mp4_path = os.path.splitext(ts_path)[0] + ".mp4"
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-y", "-loglevel", "error", "-i", ts_path,
        "-c", "copy", "-bsf:a", "aac_adtstoasc", mp4_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode == 0 and os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
        try:
            os.remove(ts_path)
        except OSError:
            pass
        return mp4_path
    if os.path.exists(mp4_path) and os.path.getsize(mp4_path) == 0:
        try:
            os.remove(mp4_path)
        except OSError:
            pass
    return None


__all__ = [
    "HlsDownloader",
    "MediaPlaylist",
    "Segment",
    "Variant",
    "find_ffmpeg",
    "is_master_playlist",
    "parse_master",
    "parse_media_playlist",
    "remux_to_mp4",
]
