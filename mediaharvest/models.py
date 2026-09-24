"""核心数据模型：媒体条目、抓取结果。

整个工具内部一律用 :class:`MediaItem` 表示一个「待下载的媒体资源」，
不管是静态 HTML 解析出来的、JS 渲染后嗅探到的，还是 m3u8 清单里拆出来的。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, unquote


class MediaType(str, Enum):
    """媒体类型。"""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    HLS = "hls"      # .m3u8 流媒体清单
    DASH = "dash"    # .mpd 清单
    SEGMENT = "segment"  # .ts / .m4s 分片，一般不单独下载
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            "image": "图片",
            "video": "视频",
            "audio": "音频",
            "hls": "HLS 流",
            "dash": "DASH 流",
            "segment": "流分片",
            "other": "其他",
        }.get(self.value, self.value)


class Source(str, Enum):
    """媒体 URL 的发现来源，用于排查「为什么抓到了/没抓到」。"""

    TAG = "tag"            # <img src> / <video src> 等直接属性
    LAZY = "lazy"          # data-src / data-original 等懒加载属性
    SRCSET = "srcset"      # srcset / data-srcset 多分辨率
    STYLE = "style"        # CSS background-image
    META = "meta"          # og:image / og:video
    SCRIPT = "script"      # 内联 JS / JSON 里的 URL
    ANCHOR = "anchor"      # <a href> 指向媒体文件
    NETWORK = "network"    # 无头浏览器嗅探到的真实请求
    MANIFEST = "manifest"  # 由 m3u8 / mpd 清单解析而来

    @property
    def label(self) -> str:
        return {
            "tag": "标签属性",
            "lazy": "懒加载属性",
            "srcset": "多分辨率",
            "style": "CSS 背景",
            "meta": "分享元信息",
            "script": "内联脚本",
            "anchor": "超链接",
            "network": "网络嗅探",
            "manifest": "流清单",
        }.get(self.value, self.value)


@dataclass
class MediaItem:
    """一个待下载的媒体资源。"""

    url: str
    type: MediaType
    source: Source
    page_url: str = ""
    ext: str = ""
    filename: str = ""
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None
    title: str = ""
    poster: str = ""
    content_type: str = ""
    size: Optional[int] = None
    group: str = ""          # srcset 同组标记，同组内只有 primary 默认勾选
    primary: bool = True
    referer: str = ""        # 下载该资源时应带的 Referer
    meta: Dict[str, Any] = field(default_factory=dict)

    # ---- 派生属性 ----------------------------------------------------

    @property
    def id(self) -> str:
        """稳定短 id，供 Web UI 前端引用。"""
        return hashlib.sha1(self.url.encode("utf-8")).hexdigest()[:12]

    @property
    def host(self) -> str:
        try:
            return urlparse(self.url).netloc
        except ValueError:
            return ""

    @property
    def display_name(self) -> str:
        if self.filename:
            return self.filename
        name = unquote(urlparse(self.url).path.rsplit("/", 1)[-1]) or self.host
        return name or self.url

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["type"] = self.type.value
        data["source"] = self.source.value
        data["type_label"] = self.type.label
        data["source_label"] = self.source.label
        data["id"] = self.id
        data["host"] = self.host
        data["display_name"] = self.display_name
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MediaItem":
        data = dict(data)
        data.pop("id", None)
        data.pop("host", None)
        data.pop("display_name", None)
        data.pop("type_label", None)
        data.pop("source_label", None)
        data["type"] = MediaType(data.get("type", "other"))
        data["source"] = Source(data.get("source", "tag"))
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class PageCapture:
    """一次页面抓取的完整产物。"""

    url: str
    final_url: str = ""
    title: str = ""
    html: str = ""
    status: int = 0
    rendered: bool = False          # 是否经过无头浏览器渲染
    redirects: List[str] = field(default_factory=list)
    items: List[MediaItem] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def media_items(self) -> List[MediaItem]:
        return [i for i in self.items if i.type != MediaType.SEGMENT]


@dataclass
class DownloadResult:
    """单个资源的下载结果。"""

    item: MediaItem
    ok: bool = False
    path: str = ""
    size: int = 0
    error: str = ""
    skipped: bool = False
    attempts: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.item.url,
            "path": self.path,
            "ok": self.ok,
            "size": self.size,
            "error": self.error,
            "skipped": self.skipped,
            "type": self.item.type.value,
            "name": self.item.display_name,
        }
