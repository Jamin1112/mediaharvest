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


class TrackKind(str, Enum):
    """音乐资源的粒度：单曲 / 专辑 / 歌单 / 歌手 / 电台。"""

    SONG = "song"            # 单曲
    ALBUM = "album"          # 专辑（整张）
    PLAYLIST = "playlist"    # 歌单 / 合集
    ARTIST = "artist"        # 歌手全部作品
    RADIO = "radio"          # 电台 / 播客
    MV = "mv"                # 音乐视频

    @property
    def label(self) -> str:
        return {
            "song": "单曲",
            "album": "专辑",
            "playlist": "歌单",
            "artist": "歌手",
            "radio": "电台",
            "mv": "MV",
        }.get(self.value, self.value)

    @property
    def is_collection(self) -> bool:
        """是否为「多首」的集合型目标——需要展开成多条曲目。"""
        return self in (TrackKind.ALBUM, TrackKind.PLAYLIST,
                        TrackKind.ARTIST, TrackKind.RADIO)


@dataclass
class MusicMeta:
    """一首曲目的音乐元信息。

    与 :class:`MediaItem` 分开存放的原因：媒体条目只关心「怎么下」，
    音乐元信息关心「下下来之后这首歌叫什么」，两者来源不同
    （前者来自格式信息，后者来自站点的曲目信息），合并会互相污染。

    这些字段最终由 :mod:`mediaharvest.tags` 写成 ID3 / MP4 / Vorbis 标签。
    """

    title: str = ""
    artist: str = ""             # 多歌手用 " / " 连接
    album: str = ""
    album_artist: str = ""
    track_number: int = 0
    track_total: int = 0
    disc_number: int = 0
    disc_total: int = 0
    year: str = ""
    date: str = ""               # 完整日期，如 2024-05-01
    genre: str = ""
    isrc: str = ""
    copyright: str = ""
    comment: str = ""
    lyrics: str = ""             # LRC 或纯文本
    cover_url: str = ""          # 封面图地址，下载后嵌入标签
    duration: Optional[float] = None
    platform: str = ""           # 平台 key，如 netease
    platform_name: str = ""      # 平台中文名，如 网易云音乐
    track_id: str = ""           # 平台内的曲目 id
    album_id: str = ""
    kind: TrackKind = TrackKind.SONG

    @property
    def has_tags(self) -> bool:
        """是否有值得写进文件的标签内容。"""
        return bool(self.title or self.artist or self.album or self.lyrics)

    @property
    def display(self) -> str:
        """``歌手 - 标题`` 形式的可读名称。"""
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.title or self.artist or ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        data["kind_label"] = self.kind.label
        data["display"] = self.display
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MusicMeta":
        data = dict(data or {})
        for drop in ("kind_label", "display"):
            data.pop(drop, None)
        try:
            data["kind"] = TrackKind(data.get("kind", "song"))
        except ValueError:
            data["kind"] = TrackKind.SONG
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


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
    #: 音乐元信息（仅音乐类资源有值），下载后用于写 ID3/MP4/Vorbis 标签
    music: Optional[MusicMeta] = None
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
        """可读名称：音乐条目优先用「歌手 - 歌名」。

        音乐 CDN 的文件名通常是哈希（``f6344203897a...mp3``），拿它当显示名
        在 CLI 列表和 Web 界面上毫无意义。
        """
        if self.music is not None and self.music.display:
            return self.music.display
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
        # 嵌套的音乐元信息要单独序列化（asdict 已展开，这里补上枚举与派生字段）
        data["music"] = self.music.to_dict() if self.music else None
        # meta 里可能存着二进制（如封面字节），JSON 序列化会直接抛异常。
        # 在这里剔除而不是让调用方各自处理：to_dict 的契约就是「可序列化」。
        data["meta"] = {
            k: v for k, v in (data.get("meta") or {}).items()
            if not isinstance(v, (bytes, bytearray, memoryview))
        }
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MediaItem":
        data = dict(data)
        for drop in ("id", "host", "display_name", "type_label", "source_label"):
            data.pop(drop, None)
        raw_music = data.pop("music", None)
        data["type"] = MediaType(data.get("type", "other"))
        data["source"] = Source(data.get("source", "tag"))
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        item = cls(**{k: v for k, v in data.items() if k in allowed})
        if raw_music:
            item.music = MusicMeta.from_dict(raw_music)
        return item


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
