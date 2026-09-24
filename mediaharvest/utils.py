"""通用工具：URL 清洗、类型判定、文件名生成等。"""
from __future__ import annotations

import hashlib
import html as _html
import mimetypes
import posixpath
import re
from typing import Dict, Optional
from urllib.parse import (
    unquote,
    urljoin,
    urlsplit,
    urlunsplit,
    parse_qsl,
    urlencode,
)

from .models import MediaType

# --------------------------------------------------------------------------
# 扩展名 / MIME 表
# --------------------------------------------------------------------------

IMAGE_EXTS = {
    "jpg", "jpeg", "jfif", "pjpeg", "png", "apng", "gif", "webp", "bmp",
    "svg", "ico", "avif", "heic", "heif", "tif", "tiff", "jxl",
}
VIDEO_EXTS = {
    "mp4", "m4v", "mov", "webm", "mkv", "avi", "flv", "wmv", "mpeg", "mpg",
    "3gp", "3g2", "ogv", "ts", "m2ts", "mts", "vob", "rmvb", "f4v", "asf",
}
AUDIO_EXTS = {
    "mp3", "m4a", "aac", "flac", "wav", "ogg", "oga", "opus", "wma", "aiff", "ape",
}
STREAM_EXTS = {"m3u8", "mpd"}
SEGMENT_EXTS = {"ts", "m4s", "cmfv", "cmfa", "aac", "fmp4"}

#: 这些扩展名即使命中也不当作可下载媒体（避免把播放器脚本当视频）
DENY_EXTS = {
    "js", "css", "json", "xml", "html", "htm", "php", "txt", "woff", "woff2",
    "ttf", "eot", "map", "wasm",
}

#: CDN 常用这些「伪装」扩展名提供图片/视频，实际类型以 Content-Type 为准
_DECEPTIVE_EXTS = {
    "php", "ashx", "aspx", "jsp", "do", "cgi", "bin", "asp", "action",
    "ashx", "ashx", "handler", "img", "image", "media", "download",
}

MIME_TO_TYPE: Dict[str, MediaType] = {}
for _ext in IMAGE_EXTS:
    MIME_TO_TYPE["image/" + _ext] = MediaType.IMAGE
MIME_TO_TYPE.update({
    "image/jpeg": MediaType.IMAGE,
    "image/jpg": MediaType.IMAGE,
    "image/png": MediaType.IMAGE,
    "image/gif": MediaType.IMAGE,
    "image/webp": MediaType.IMAGE,
    "image/svg+xml": MediaType.IMAGE,
    "image/avif": MediaType.IMAGE,
    "image/bmp": MediaType.IMAGE,
    "image/x-icon": MediaType.IMAGE,
    "image/vnd.microsoft.icon": MediaType.IMAGE,
    "image/heic": MediaType.IMAGE,
    "video/mp4": MediaType.VIDEO,
    "video/webm": MediaType.VIDEO,
    "video/quicktime": MediaType.VIDEO,
    "video/x-msvideo": MediaType.VIDEO,
    "video/x-flv": MediaType.VIDEO,
    "video/x-matroska": MediaType.VIDEO,
    "video/mpeg": MediaType.VIDEO,
    "video/3gpp": MediaType.VIDEO,
    "video/ogg": MediaType.VIDEO,
    "video/mp2t": MediaType.SEGMENT,
    "application/vnd.apple.mpegurl": MediaType.HLS,
    "application/x-mpegurl": MediaType.HLS,
    "audio/mpegurl": MediaType.HLS,
    "application/dash+xml": MediaType.DASH,
    "audio/mpeg": MediaType.AUDIO,
    "audio/mp4": MediaType.AUDIO,
    "audio/aac": MediaType.AUDIO,
    "audio/flac": MediaType.AUDIO,
    "audio/wav": MediaType.AUDIO,
    "audio/x-wav": MediaType.AUDIO,
    "audio/ogg": MediaType.AUDIO,
    "audio/opus": MediaType.AUDIO,
    "audio/webm": MediaType.AUDIO,
})

#: 形如 https://host/path 的宽松匹配，用于在脚本/JSON/文本里捞 URL
URL_IN_TEXT_RE = re.compile(
    r"""(?:https?:)?//[^\s"'`<>\\)\[\]{},;|]+""",
    re.IGNORECASE,
)

#: 需要从 URL 里剔除的跟踪参数（保留其它 query，签名类参数不能删）
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "share_token", "wxshare", "scene", "srcid", "share_from",
    "timestamp", "_t", "t",  # 注意：部分站点 t 是签名，默认不删，见 strip_tracking
}


# --------------------------------------------------------------------------
# URL 处理
# --------------------------------------------------------------------------

def unescape_url(raw: str) -> str:
    """还原 JS / JSON / HTML 转义后的 URL。"""
    if not raw:
        return ""
    url = raw.strip()
    if "\\" in url:
        url = url.replace("\\/", "/")
        url = re.sub(r"\\u002[fF]", "/", url)
        url = re.sub(r"\\u0026", "&", url)
        url = url.replace("\\", "")
    if "&amp;" in url or "&#" in url:
        url = _html.unescape(url)
    return url.strip()


def clean_url(raw: str) -> str:
    """去掉 URL 尾部常见的标点与包裹字符噪声。"""
    url = unescape_url(raw)
    # 成对包裹的引号/括号先剥掉
    if len(url) >= 2 and url[0] in "\"'(" and url[-1] in "\"')":
        url = url[1:-1]
    # 反复剥离尾部噪声直到稳定：形如 "x.jpg')," 需要多轮才能清干净
    while url:
        last = url[-1]
        if last in ".,;:!?\u3002\uff0c'\"":
            url = url[:-1]
            continue
        # 右括号仅在不成对时删除，避免破坏合法 URL
        if last == ")" and url.count("(") < url.count(")"):
            url = url[:-1]
            continue
        break
    return url


def resolve_url(base: str, raw: str) -> Optional[str]:
    """把相对/协议相对 URL 解析为绝对 URL，不可用则返回 None。"""
    if not raw:
        return None
    url = clean_url(raw)
    if not url:
        return None
    low = url.lower()
    if low.startswith(("data:", "blob:", "javascript:", "mailto:", "tel:", "about:", "#")):
        return None
    if url.startswith("//"):
        scheme = urlsplit(base).scheme or "https"
        url = scheme + ":" + url
    elif not low.startswith(("http://", "https://")):
        if not base:
            return None
        url = urljoin(base, url)
    if not url.lower().startswith(("http://", "https://")):
        return None
    # 去掉 fragment
    parts = urlsplit(url)
    if parts.fragment:
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    return url


def strip_tracking(url: str) -> str:
    """移除明显的统计参数，保留可能参与签名的参数。"""
    parts = urlsplit(url)
    if not parts.query:
        return url
    keep = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS or k.lower() in {"t", "timestamp"}
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(keep), ""))


def url_ext(url: str) -> str:
    """取 URL 路径上的扩展名（小写，不含点）。"""
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    base = posixpath.basename(unquote(path))
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[-1].lower()


def url_query_hint(url: str) -> str:
    """从 query 里猜扩展名，例如 ?format=jpg / ?type=png。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    for key, value in parse_qsl(parts.query, keep_blank_values=False):
        if key.lower() in {"format", "type", "ext", "fm", "f", "img", "suffix"}:
            value = value.lower().lstrip(".")
            if value in IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS:
                return value
    return ""


def classify(url: str, content_type: str = "") -> MediaType:
    """根据 MIME 与 URL 判定媒体类型。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct and ct in MIME_TO_TYPE:
        return MIME_TO_TYPE[ct]
    if ct.startswith("image/"):
        return MediaType.IMAGE
    if ct.startswith("video/"):
        return MediaType.VIDEO
    if ct.startswith("audio/"):
        return MediaType.AUDIO
    if ct in {"application/octet-stream", "binary/octet-stream"}:
        ct = ""  # 无信息，交给 URL 判断

    ext = url_ext(url) or url_query_hint(url)
    low = url.lower()
    if ext in DENY_EXTS:
        return MediaType.OTHER
    if ext in STREAM_EXTS:
        return MediaType.HLS if ext == "m3u8" else MediaType.DASH
    if ext in IMAGE_EXTS:
        return MediaType.IMAGE
    if ext in VIDEO_EXTS:
        return MediaType.SEGMENT if ext in SEGMENT_EXTS else MediaType.VIDEO
    if ext in AUDIO_EXTS:
        return MediaType.AUDIO
    # 无扩展名时的路径/参数特征
    if any(k in low for k in ("/m3u8", "playlist.m3u8", "hls/index", "master.m3u8")):
        return MediaType.HLS
    if ".mpd" in low or "/dash/" in low:
        return MediaType.DASH
    return MediaType.OTHER


def ext_from_content_type(content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct:
        return ""
    explicit = {
        "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
        "image/gif": "gif", "image/webp": "webp", "image/avif": "avif",
        "image/svg+xml": "svg", "image/bmp": "bmp", "image/x-icon": "ico",
        "image/vnd.microsoft.icon": "ico", "image/heic": "heic", "image/tiff": "tiff",
        "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
        "video/x-matroska": "mkv", "video/mpeg": "mpg", "video/x-flv": "flv",
        "video/mp2t": "ts", "audio/mpeg": "mp3", "audio/mp4": "m4a",
        "audio/aac": "aac", "audio/flac": "flac", "audio/wav": "wav",
        "audio/x-wav": "wav", "audio/ogg": "ogg", "audio/opus": "opus",
        "application/vnd.apple.mpegurl": "m3u8", "application/x-mpegurl": "m3u8",
    }
    if ct in explicit:
        return explicit[ct]
    guessed = mimetypes.guess_extension(ct)
    return (guessed or "").lstrip(".").replace("jpe", "jpg")


# --------------------------------------------------------------------------
# 文件名
# --------------------------------------------------------------------------

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, max_len: int = 100) -> str:
    """清洗文件名：去非法字符、限长、避免系统保留名。"""
    name = unquote(name or "").strip()
    # 把被替换掉的非法字符周围的空白一并压缩，避免出现 "标题 _ 副标题"
    name = _UNSAFE_CHARS.sub(" ", name)
    name = name.replace("\n", " ").replace("\r", " ")
    name = re.sub(r"\s+", " ", name)

    if not name:
        return ""
    stem, dot, ext = name.rpartition(".")
    if not dot:  # 没有扩展名
        stem, ext = name, ""
    # 分别清洗主名与扩展名，避免把 "a .png" 里的空格留在点前面
    stem = stem.strip(" .")
    ext = ext.strip(" .")
    # 折叠分隔符周围的空白：'a - b' -> 'a-b'，'a _ b' -> 'a_b'
    stem = re.sub(r"\s*([_\-–—]+)\s*", r"\1", stem)
    # 其它位置的多余空格压成单个下划线，保持可读性
    stem = re.sub(r"\s+", "_", stem).strip("_.-")
    if len(stem) > max_len:
        stem = stem[:max_len].rstrip("_.- ")
    if not stem:
        return f"file.{ext}" if ext else ""
    if stem.lower() in _RESERVED_NAMES:
        stem = "_" + stem
    return f"{stem}.{ext}" if ext else stem


def filename_from_url(url: str, fallback_stem: str = "media", default_ext: str = "") -> str:
    """由 URL 生成保存用的文件名。"""
    try:
        path = urlsplit(url).path
    except ValueError:
        path = ""
    base = posixpath.basename(unquote(path))
    name = sanitize_filename(base)
    if name:
        stem, dot, ext = name.rpartition(".")
        if dot and ext:
            return name
        # 有名字但没扩展名
        return f"{name}.{default_ext}" if default_ext else name
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    ext = default_ext or url_ext(url) or url_query_hint(url) or "bin"
    return f"{fallback_stem}_{digest}.{ext}"


def ensure_ext(filename: str, ext: str) -> str:
    """确保文件名带指定扩展名（用于下载后按 Content-Type 纠正）。"""
    if not ext:
        return filename
    stem, dot, cur = filename.rpartition(".")
    if not dot:
        return f"{filename}.{ext}"
    if cur.lower() == ext.lower():
        return filename
    # 扩展名与真实类型不符时替换掉（CDN 常见的 .php/.ashx 伪装）
    if cur.lower() in _DECEPTIVE_EXTS:
        return f"{stem}.{ext}"
    # 已知媒体扩展名但与真实类型不符：仍以真实类型为准
    if cur.lower() in IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS:
        return f"{stem}.{ext}"
    return f"{filename}.{ext}"


# --------------------------------------------------------------------------
# 杂项
# --------------------------------------------------------------------------

_SIZE_UNITS = {
    "b": 1, "k": 1024, "kb": 1024, "kib": 1024,
    "m": 1024 ** 2, "mb": 1024 ** 2, "mib": 1024 ** 2,
    "g": 1024 ** 3, "gb": 1024 ** 3, "gib": 1024 ** 3,
}


def parse_size(text: str) -> Optional[int]:
    """把 ``10M`` / ``1.5GB`` / ``1024`` 解析成字节数。"""
    if not text:
        return None
    m = re.fullmatch(r"\s*([\d.]+)\s*([a-zA-Z]*)\s*", str(text))
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or "b").lower()
    if unit not in _SIZE_UNITS:
        return None
    return int(value * _SIZE_UNITS[unit])


def human_size(num: Optional[int]) -> str:
    """人类可读的体积。"""
    if num is None:
        return "-"
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def human_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def looks_like_media_url(url: str) -> bool:
    """快速判断一个 URL 是否值得当作媒体候选。"""
    return classify(url) != MediaType.OTHER
