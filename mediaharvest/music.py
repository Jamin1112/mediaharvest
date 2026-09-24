"""音乐抓取支持：平台识别、目标分类、音质选择与曲目元信息。

这个模块解决「音乐」与「视频」在抓取上的三处本质差异：

1. **目标粒度不同**——一个音乐 URL 可能是单曲，也可能是专辑/歌单/歌手，
   后者要先展开成几十首曲目，而视频页基本就是一坨。
2. **选择依据不同**——视频按分辨率选流，音乐按**编码与码率**选流，
   还要区分无损与有损（flac vs mp3），否则「最高音质」会给出体积巨大
   但站点其实有无损源的有损文件。
3. **落地方式不同**——音乐文件需要 ID3/Vorbis/MP4 标签、歌词与封面才能
   进音乐库，纯文件名是不够的。

设计上把「平台差异」压缩成一张
:data:`MUSIC_PLATFORMS` 注册表 + 少量按平台扩展的钩子（歌词接口等），
抓取链路本身保持通用：任何平台只要能拿到 yt-dlp 的 info dict，
就能自动获得音质选择、标签与命名能力。新增平台通常只需往注册表加一行。

.. note::
   本模块**不做**任何 DRM 绕过。平台是否可抓取决于其是否公开了音频流地址；
   QQ音乐/酷狗等站点加密或需签名时，会明确失败而不是产出坏文件。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

# --------------------------------------------------------------------------
# 音频编码 / 容器知识
# --------------------------------------------------------------------------

#: 无损编码与容器。用于判定「这条流是不是无损」。
LOSSLESS_CODECS = {"flac", "alac", "ape", "wav", "aiff", "pcm_s16le", "pcm_s24le",
                   "pcm_s32le", "tta", "wv", "dsd"}
LOSSLESS_EXTS = {"flac", "alac", "ape", "wav", "aiff", "aif", "tta", "wv", "dsf", "dff"}

#: 有损编码。
LOSSY_CODECS = {"mp3", "aac", "opus", "vorbis", "mp4a", "ac3", "eac3", "wmav2"}

#: 音频容器的「默认扩展名」，用于文件名与标签写入分流。
AUDIO_EXTS = LOSSLESS_EXTS | {"mp3", "m4a", "aac", "ogg", "oga", "opus", "wma", "weba"}

#: 编码 → 常见容器扩展名，用于把 ``acodec`` 归一到扩展名。
_CODEC_EXT = {
    "flac": "flac", "alac": "m4a", "aac": "m4a", "mp4a": "m4a",
    "mp3": "mp3", "opus": "opus", "vorbis": "ogg", "wav": "wav",
    "pcm_s16le": "wav", "pcm_s24le": "wav",
}


def is_lossless(fmt: Dict[str, Any]) -> bool:
    """判断一个 yt-dlp 格式是否为无损。

    先看 ``ext`` 再看 ``acodec``：部分站点把 flac 装进 ``.m4a`` 容器
    （如 ALAC），只看扩展名会误判为有损。
    """
    ext = str(fmt.get("ext") or "").lower()
    acodec = str(fmt.get("acodec") or "").lower()
    if acodec in LOSSLESS_CODECS:
        return True
    # acodec 未知时退回扩展名判断，避免丢掉真正的无损流
    if not acodec or acodec == "none":
        return ext in LOSSLESS_EXTS
    return ext in LOSSLESS_EXTS and acodec.startswith("pcm")


def format_ext(fmt: Dict[str, Any]) -> str:
    """取格式的音频扩展名，acodec 比 ext 更可信时以 acodec 为准。"""
    ext = str(fmt.get("ext") or "").lower()
    acodec = str(fmt.get("acodec") or "").lower().split(".")[0]
    if ext in ("", "m4a") and acodec in _CODEC_EXT:
        return _CODEC_EXT[acodec]
    if ext in AUDIO_EXTS:
        return ext
    return _CODEC_EXT.get(acodec, ext or "m4a")


def format_bitrate(fmt: Dict[str, Any]) -> int:
    """取格式码率（kbps）。

    依次尝试 ``abr`` / ``tbr`` / ``audio_bitrate``：不同提取器填的字段不一致，
    只认一个会漏掉大量格式。
    """
    for key in ("abr", "tbr", "audio_bitrate", "vbr"):
        value = fmt.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return 0


def format_label(fmt: Dict[str, Any]) -> str:
    """生成人类可读的格式说明，如 ``FLAC 1004kbps``。"""
    codec = str(fmt.get("acodec") or "").split(".")[0].upper()
    ext = format_ext(fmt).upper()
    name = codec or ext or "AUDIO"
    br = format_bitrate(fmt)
    if br:
        return f"{name} {br}kbps"
    if is_lossless(fmt):
        return f"{name} 无损"
    return name


# --------------------------------------------------------------------------
# 音质预设
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class QualityPreset:
    """一档音质偏好。

    ``lossless_first`` 控制无损与有损的优先级；``target_abr`` 只在有损档位
    生效，用于「省流」这类需要贴近某个码率而非一味取最高的场景；
    ``codec_first`` 让编码偏好压过码率，用于「高音质」这类明确点名编码的档位。
    """

    key: str
    label: str
    codecs: Tuple[str, ...]      # 编码偏好，从高到低
    lossless_first: bool = False
    lossless_only: bool = False
    target_abr: int = 0          # 0 表示不设目标，直接取最高
    codec_first: bool = False    # 编码偏好优先于码率

    def describe(self) -> str:
        if self.lossless_only:
            return f"{self.label}（仅无损）"
        if self.target_abr:
            return f"{self.label}（目标 {self.target_abr}kbps）"
        if self.lossless_first:
            return f"{self.label}（无损优先）"
        return self.label


#: 音质档位。``best`` 与 ``lossless`` 的区别：前者「无损优先、没有就取最好的
#: 有损（按客观码率排）」；后者「只要无损，没有就明确失败」——后者对想建
#: 无损库的用户更诚实。``high`` 则按编码偏好选，追求「够好且更省空间」。
QUALITY_PRESETS: Dict[str, QualityPreset] = {
    "best": QualityPreset(
        key="best", label="最高音质", lossless_first=True,
        codecs=("flac", "alac", "wav", "aiff", "m4a", "opus", "mp3", "ogg", "aac"),
    ),
    "lossless": QualityPreset(
        key="lossless", label="仅无损", lossless_first=True, lossless_only=True,
        codecs=("flac", "alac", "wav", "aiff", "ape", "tta", "wv"),
    ),
    "high": QualityPreset(
        key="high", label="高音质", lossless_first=False, codec_first=True,
        codecs=("m4a", "mp3", "opus", "ogg", "aac"),
    ),
    "medium": QualityPreset(
        key="medium", label="中等音质", lossless_first=False, codec_first=True,
        codecs=("m4a", "mp3", "opus", "aac"), target_abr=192,
    ),
    "low": QualityPreset(
        key="low", label="省流", lossless_first=False, codec_first=True,
        codecs=("m4a", "mp3", "aac"), target_abr=128,
    ),
}

DEFAULT_QUALITY = "best"


def get_quality(key: str) -> QualityPreset:
    """按 key 取音质预设，未知 key 回退到最高音质。"""
    return QUALITY_PRESETS.get((key or "").strip().lower(),
                               QUALITY_PRESETS[DEFAULT_QUALITY])


def quality_keys() -> List[str]:
    return list(QUALITY_PRESETS)


def _codec_rank(fmt: Dict[str, Any], preset: QualityPreset) -> int:
    """编码在偏好表里的位次，越小越优先；未列出排在最后。"""
    codec = str(fmt.get("acodec") or "").lower().split(".")[0]
    ext = format_ext(fmt)
    for idx, want in enumerate(preset.codecs):
        if want == codec or want == ext:
            return idx
    # mp4a.40.2 这类 acodec 归一后是 mp4a，与用户认知的 m4a 等价
    if codec in ("mp4a", "aac") and "m4a" in preset.codecs:
        return preset.codecs.index("m4a")
    return len(preset.codecs)


def sort_audio_formats(
    formats: Sequence[Dict[str, Any]], preset: QualityPreset
) -> List[Dict[str, Any]]:
    """按音质偏好把音频格式排序，**最优先的排在最前**。

    排序规则（依次比较）：

    1. 无损优先时，无损排在前面（``lossless_only`` 会直接剔除全部有损）；
    2. 设了 ``target_abr``（省流档）时**先比码率贴近度**——这类档位的目的
       就是控制体积，若让编码偏好优先，会出现「省流档却选了 256k 的 m4a，
       而放着 128k 的 mp3 不选」这种自相矛盾的结果；
    3. 没设 ``target_abr`` 时先比编码偏好——这样 ``codecs`` 表才真正有意义，
       「高音质」会选 256k 的 m4a 而不是 320k 的 mp3（同感知质量下更省空间）；
    4. 再比码率（有目标时贴近目标，无目标时取最高）；
    5. URL 兜底，保证结果稳定可复现。
    """
    audio: List[Dict[str, Any]] = []
    for fmt in formats:
        if not isinstance(fmt, dict) or not fmt.get("url"):
            continue
        acodec = str(fmt.get("acodec") or "").lower()
        vcodec = str(fmt.get("vcodec") or "").lower()
        # 只要音频轨：排除带视频的合成流，否则「音乐」会下成 MV
        if vcodec and vcodec != "none":
            continue
        if acodec == "none":
            continue
        audio.append(fmt)

    if preset.lossless_only:
        audio = [f for f in audio if is_lossless(f)]

    def key(fmt: Dict[str, Any]) -> Tuple:
        """统一返回 5 元组，避免不同档位元组长度不一致导致的错位比较。"""
        ll = 1 if is_lossless(fmt) else 0
        lossless_rank = -ll if preset.lossless_first else ll
        abr = format_bitrate(fmt)
        codec_rank = _codec_rank(fmt, preset)

        if preset.target_abr and not is_lossless(fmt):
            # 省流档：贴近目标码率优先，避免 320k 混进「省流」
            distance = abs((abr or preset.target_abr) - preset.target_abr)
            return (lossless_rank, 0, distance, 0, str(fmt.get("url") or ""))
        if preset.codec_first:
            # 高音质档：编码偏好压在码率之前，否则 320k 的 mp3 会盖过
            # 256k 的 m4a，「偏好 m4a」这个设定就形同虚设
            return (lossless_rank, 0, codec_rank, -abr, str(fmt.get("url") or ""))
        # 最高音质档：以客观质量为先，先比码率再比编码
        return (lossless_rank, 1, -abr, codec_rank, str(fmt.get("url") or ""))

    return sorted(audio, key=key)


def pick_audio_format(
    formats: Sequence[Dict[str, Any]], quality: str = DEFAULT_QUALITY
) -> Optional[Dict[str, Any]]:
    """按音质档位挑出最合适的一条音频流，没有可用流时返回 ``None``。

    返回 ``None`` 的典型场景：用户要 ``lossless`` 但该曲目只有有损源。
    调用方应据此给出「该曲目无无损源」的明确提示，而不是悄悄降级。
    """
    ranked = sort_audio_formats(formats, get_quality(quality))
    return ranked[0] if ranked else None


def build_format_selector(quality: str = DEFAULT_QUALITY) -> str:
    """生成 yt-dlp 的 ``-f`` 表达式，用于无法预先探测的直下场景。"""
    preset = get_quality(quality)
    if preset.lossless_only:
        return "bestaudio[ext=flac]/bestaudio[acodec=alac]/bestaudio[ext=wav]/bestaudio"
    if preset.lossless_first:
        return "bestaudio/best"
    return "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best"


# --------------------------------------------------------------------------
# 平台注册表
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Platform:
    """一个音乐平台。

    ``supported`` 表示**当前环境是否具备可靠的解析后端**（即 yt-dlp 有对应
    extractor）。注册表刻意把不可靠的平台也列出来并标成 ``False``：
    这样用户丢一个 QQ 音乐链接进来时，能得到「此平台需要签名校验，
    当前无法解析」这类明确答复，而不是含糊的「未发现资源」。

    ``path_re`` 用于「同一域名下只有部分路径是音乐」的情况：B 站主站是视频站，
    只有 ``/audio/auXXXX`` 才是音频。没有这个约束的话，B 站视频会被误当成
    音乐曲目走音乐通道。
    """

    key: str
    name: str
    domains: Tuple[str, ...]
    extractor: str = ""
    supported: bool = True
    note: str = ""
    #: 是否提供歌词接口（见 :data:`LYRICS_PARSERS`）
    has_lyrics: bool = False
    #: 额外的路径约束；为空表示该域名下全部路径都算音乐
    path_re: Optional[re.Pattern] = None

    def matches(self, host: str, path: str = "") -> bool:
        host = (host or "").lower().split(":")[0]
        if not any(host == d or host.endswith("." + d) for d in self.domains):
            return False
        if self.path_re is not None and not self.path_re.search(path or ""):
            return False
        return True


#: 平台注册表。``supported=False`` 的是「已知但当前抓不动」的平台，
#: 保留它们的意义在于给出准确的错误信息与后续接入位置。
MUSIC_PLATFORMS: Tuple[Platform, ...] = (
    Platform("netease", "网易云音乐", ("music.163.com", "163cn.tv"),
             extractor="NetEaseMusic", has_lyrics=True),
    Platform("qqmusic", "QQ音乐", ("y.qq.com", "i.y.qq.com", "c.y.qq.com"),
             extractor="QQMusic", supported=False,
             note="接口需要签名校验，yt-dlp 解析常失败"),
    Platform("kugou", "酷狗音乐", ("kugou.com", "m.kugou.com"),
             supported=False, note="无可用解析后端"),
    Platform("kuwo", "酷我音乐", ("kuwo.cn",),
             extractor="Kuwo", supported=False,
             note="extractor 存在但实测常返回空，且需 Cookie"),
    Platform("migu", "咪咕音乐", ("music.migu.cn",),
             supported=False, note="无可用解析后端"),
    Platform("bilibili_audio", "B站音频", ("bilibili.com",),
             extractor="BilibiliAudio",
             note="仅 /audio/auXXXX 路径为音频，其余按视频处理",
             path_re=re.compile(r"/audio/au\d+", re.I)),
    Platform("soundcloud", "SoundCloud", ("soundcloud.com", "snd.sc"),
             extractor="SoundCloud"),
    Platform("bandcamp", "Bandcamp", ("bandcamp.com",), extractor="Bandcamp"),
    Platform("mixcloud", "Mixcloud", ("mixcloud.com",), extractor="Mixcloud"),
    Platform("audiomack", "Audiomack", ("audiomack.com",), extractor="Audiomack"),
    Platform("jamendo", "Jamendo", ("jamendo.com",), extractor="Jamendo"),
    Platform("archive_audio", "互联网档案馆", ("archive.org",),
             extractor="ArchiveOrg"),
    Platform("qingting", "蜻蜓FM", ("qingting.fm",), extractor="QingTing",
             has_lyrics=False),
    Platform("lastfm", "Last.fm", ("last.fm",), extractor="LastFM",
             supported=False, note="仅提供试听片段"),
    Platform("spotify", "Spotify", ("spotify.com", "open.spotify.com"),
             supported=False, note="DRM 保护，无法下载"),
    Platform("applemusic", "Apple Music", ("music.apple.com",),
             supported=False, note="DRM 保护，无法下载"),
    Platform("deezer", "Deezer", ("deezer.com",),
             supported=False, note="无可用解析后端"),
    Platform("tidal", "Tidal", ("tidal.com", "listen.tidal.com"),
             supported=False, note="DRM 保护，无法下载"),
    Platform("youtube_music", "YouTube Music", ("music.youtube.com",),
             extractor="Youtube", note="按普通 YouTube 视频解析"),
)

#: 平台 key → Platform，便于按名字查。
PLATFORM_BY_KEY: Dict[str, Platform] = {p.key: p for p in MUSIC_PLATFORMS}


def platform_for(url: str) -> Optional[Platform]:
    """识别 URL 属于哪个音乐平台；不是已知平台返回 ``None``。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    # 路径约束要看完整路径 + fragment（网易云把路由放在 fragment 里）
    path = parts.path + parts.fragment
    for platform in MUSIC_PLATFORMS:
        if platform.matches(parts.netloc, path):
            return platform
    return None


# --------------------------------------------------------------------------
# 音乐 URL 识别与目标分类
# --------------------------------------------------------------------------

#: 路径特征 → 目标粒度。顺序有意义：先匹配到的先用（单曲特征比歌手特征更具体）。
#: 每项为 ``(正则, TrackKind)``，正则在 URL 的 path(+query) 上匹配。
_KIND_PATTERNS: Tuple[Tuple[re.Pattern, "TrackKindRef"], ...] = ()


def _kind_patterns() -> Tuple[Tuple[re.Pattern, Any], ...]:
    """延迟构造路径特征表（TrackKind 需从 models 导入，避免循环依赖）。"""
    global _KIND_PATTERNS
    if _KIND_PATTERNS:
        return _KIND_PATTERNS
    from .models import TrackKind as K

    _KIND_PATTERNS = (
        # ---- 单曲 ----
        (re.compile(r"/song\b|/song/|/songDetail|/track\b|/play_detail/|"
                    r"/audio/au\d+|/tracks?/", re.I), K.SONG),
        (re.compile(r"music\.163\.com.*[?&]id=\d+", re.I), K.SONG),
        # ---- MV / 音乐视频 ----
        (re.compile(r"/mv\b|/video/|/watch\?v=|/film/", re.I), K.MV),
        # ---- 专辑 ----
        (re.compile(r"/album\b|/album/|/albumDetail|/release/|/records/", re.I), K.ALBUM),
        # ---- 歌单 / 合集 / 电台 ----
        (re.compile(r"/playlist\b|/playlist/|/playlistDetail|/sets/|/djradio|"
                    r"/program/|/radio", re.I), K.PLAYLIST),
        (re.compile(r"/discover/toplist|/chart", re.I), K.PLAYLIST),
        # ---- 歌手 ----
        (re.compile(r"/artist\b|/artist/|/singer|/user/|/profile", re.I), K.ARTIST),
    )
    return _KIND_PATTERNS


@dataclass
class MusicTarget:
    """一个音乐 URL 的解析结果。"""

    url: str
    platform: Optional[Platform]
    kind: Any                      # TrackKind
    track_id: str = ""
    album_id: str = ""
    playlist_id: str = ""
    artist_id: str = ""
    reason: str = ""               # 判定依据，便于排查

    @property
    def platform_name(self) -> str:
        return self.platform.name if self.platform else "未知平台"

    @property
    def is_collection(self) -> bool:
        return bool(self.kind and self.kind.is_collection)

    @property
    def is_music(self) -> bool:
        return self.platform is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "platform": self.platform.key if self.platform else "",
            "platform_name": self.platform_name,
            "kind": self.kind.value if self.kind else "",
            "kind_label": self.kind.label if self.kind else "",
            "track_id": self.track_id,
            "album_id": self.album_id,
            "playlist_id": self.playlist_id,
            "artist_id": self.artist_id,
            "supported": self.platform.supported if self.platform else False,
            "reason": self.reason,
        }


#: 常见的曲目/专辑/歌单 id 查询参数名。
_ID_KEYS = {
    "track": ("id", "songid", "song_id", "trackid", "track_id", "sid"),
    "album": ("albumid", "album_id", "aid"),
    "playlist": ("playlistid", "playlist_id", "listid", "pid"),
    "artist": ("artistid", "artist_id", "singerid", "uid"),
}


def _ids_from_query(query: str) -> Dict[str, str]:
    """从 query 里提取各类 id。"""
    try:
        params = parse_qs(query, keep_blank_values=False)
    except ValueError:
        return {}
    flat = {k.lower(): (v[0] if v else "") for k, v in params.items()}
    out: Dict[str, str] = {}
    for slot, keys in _ID_KEYS.items():
        for key in keys:
            if flat.get(key):
                out[slot] = flat[key]
                break
    return out


def _ids_from_path(path: str) -> Dict[str, str]:
    """从路径里提取 id，兼容 ``/song/123`` 与 ``/songDetail/0039MnYb`` 两种风格。

    QQ 音乐用的是 ``songDetail``/``albumDetail`` 这种驼峰段，必须单独覆盖，
    否则它的 id 一个都取不到（歌词、去重、日志都会缺上下文）。
    """
    out: Dict[str, str] = {}
    #: id 的形态：纯数字（网易云/B站）或含字母的 8 位以上串（QQ音乐/Bandcamp）。
    #: 分支顺序很关键——``\d+`` 放前面会把 ``0039MnYb0qxYhV`` 截成 ``0039``，
    #: 必须先试更长的字母数字形式。
    _ID = r"([A-Za-z0-9]{8,}|\d+)"
    patterns = (
        (rf"/(?:song|track|play_detail)/{_ID}", "track"),
        (rf"/songDetail/{_ID}", "track"),
        (rf"/(?:album|release)/{_ID}", "album"),
        (rf"/albumDetail/{_ID}", "album"),
        (rf"/(?:playlist|toplist|sets)/{_ID}", "playlist"),
        (rf"/playlistDetail/{_ID}", "playlist"),
    )
    for pattern, slot in patterns:
        m = re.search(pattern, path)
        if m and slot not in out:
            out[slot] = m.group(1)
    m = re.search(r"/audio/au(\d+)", path)
    if m:
        out["track"] = "au" + m.group(1)
    return out


def classify_music_url(url: str) -> MusicTarget:
    """把音乐 URL 分类成「单曲 / 专辑 / 歌单 / 歌手 / MV」。

    判定顺序是「先看平台的显式路径特征，再看 id 参数」：
    路径特征（如 ``/album/``）比 ``?id=`` 更明确，因为网易云的
    ``music.163.com/#/song?id=`` 与 ``/album?id=`` 的 query 长得一样。
    """
    from .models import TrackKind

    platform = platform_for(url)
    try:
        parts = urlsplit(url)
    except ValueError:
        return MusicTarget(url=url, platform=platform, kind=TrackKind.SONG,
                           reason="URL 无法解析")

    # 网易云等站点把真实路由放在 fragment 里（#/song?id=123），要一起看
    haystack = parts.path + ("?" + parts.query if parts.query else "") + parts.fragment

    kind = TrackKind.SONG
    reason = "默认按单曲处理"
    for pattern, candidate in _kind_patterns():
        if pattern.search(haystack):
            kind = candidate
            reason = f"路径特征匹配 {pattern.pattern}"
            break

    ids = _ids_from_query(parts.query)
    # fragment 里也可能带 query（#/song?id=123）
    if parts.fragment and "?" in parts.fragment:
        for slot, value in _ids_from_query(parts.fragment.split("?", 1)[1]).items():
            ids.setdefault(slot, value)
    for slot, value in _ids_from_path(parts.path).items():
        ids.setdefault(slot, value)

    # id 参数可以纠正路径判定：/song?id= 与 /album?id= 的路径可能都是根路径
    if "album" in ids and kind == TrackKind.SONG and not ids.get("track"):
        kind = TrackKind.ALBUM
        reason = "query 含专辑 id"
    elif "playlist" in ids and kind == TrackKind.SONG and not ids.get("track"):
        kind = TrackKind.PLAYLIST
        reason = "query 含歌单 id"

    # 通用参数名 ``id`` 会被塞进 track 槽，但配合 /album、/playlist 路径时
    # 它其实是专辑/歌单 id。放到正确的槽位，后面的歌词接口才能取对 id。
    generic_id = ids.get("track", "")
    if generic_id:
        if kind == TrackKind.ALBUM and not ids.get("album"):
            ids["album"] = generic_id
            ids.pop("track", None)
        elif kind == TrackKind.PLAYLIST and not ids.get("playlist"):
            ids["playlist"] = generic_id
            ids.pop("track", None)
        elif kind == TrackKind.ARTIST and not ids.get("artist"):
            ids["artist"] = generic_id
            ids.pop("track", None)

    return MusicTarget(
        url=url, platform=platform, kind=kind,
        track_id=ids.get("track", ""),
        album_id=ids.get("album", ""),
        playlist_id=ids.get("playlist", ""),
        artist_id=ids.get("artist", ""),
        reason=reason,
    )


def is_music_url(url: str) -> bool:
    """URL 是否指向已知音乐平台。"""
    return platform_for(url) is not None


def is_supported_music_url(url: str) -> bool:
    """URL 是否指向**当前能抓**的音乐平台。"""
    platform = platform_for(url)
    return bool(platform and platform.supported)


# --------------------------------------------------------------------------
# 曲目元信息（由 yt-dlp info dict 构建）
# --------------------------------------------------------------------------

#: yt-dlp 的 ``artist``/``creator`` 等字段可能是列表，统一成字符串。
_ARTIST_FIELDS = ("artist", "artists", "creator", "uploader", "channel", "album_artist")


def _as_text(value: Any) -> str:
    """把可能为列表/None 的元字段压成字符串。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        parts = [_as_text(v) for v in value]
        return " / ".join(p for p in parts if p)
    if isinstance(value, dict):
        return _as_text(value.get("name") or value.get("title") or "")
    return str(value).strip()


def _first_text(info: Dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        text = _as_text(info.get(key))
        if text:
            return text
    return ""


def _parse_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    m = re.search(r"\d+", str(value or ""))
    return int(m.group(0)) if m else 0


#: 形如 "01. 歌名" / "1 - 歌名" / "Track 03 歌名" 的曲目号前缀
_TRACK_PREFIX_RE = re.compile(r"^\s*(?:track\s*)?(\d{1,3})\s*[.\-–—)）:：]\s*(\S.*)$", re.I)
#: 形如 "歌名 (Live)" 之外的 "歌手 - 歌名" 拆分
_ARTIST_TITLE_RE = re.compile(r"^\s*(.+?)\s+[-–—]\s+(.+)$")


def split_track_prefix(title: str) -> Tuple[int, str]:
    """从标题里剥出曲目号，返回 ``(号, 干净标题)``。

    音乐站的标题常带 ``01.`` 前缀，直接当标题写进标签会很难看；
    但也不能无脑剥离——``1979`` 这种纯数字歌名要保留。
    因此要求分隔符存在且剩余部分非空。
    """
    title = (title or "").strip()
    m = _TRACK_PREFIX_RE.match(title)
    if not m:
        return 0, title
    number = int(m.group(1))
    rest = m.group(2).strip()
    if not rest or number > 999:
        return 0, title
    return number, rest


def meta_from_info(
    info: Dict[str, Any],
    *,
    platform: Optional[Platform] = None,
    quality: str = DEFAULT_QUALITY,
) -> "MusicMetaRef":
    """由 yt-dlp 的 info dict 构建 :class:`~mediaharvest.models.MusicMeta`。

    这里对字段做了大量兜底，原因是各提取器填的字段差异极大：
    网易云给 ``artist``，SoundCloud 给 ``uploader``，Bandcamp 给 ``artist``+``album``，
    而歌单里的一条 entry 往往只有 ``title`` 和 ``playlist_index``。
    """
    from .models import MusicMeta, TrackKind

    title = _first_text(info, ("track", "title", "song"))
    number, cleaned = split_track_prefix(title)
    if cleaned:
        title = cleaned

    track_number = _parse_int(info.get("track_number")) or number
    if not track_number:
        track_number = _parse_int(info.get("playlist_index"))

    artist = _first_text(info, ("artist", "artists", "creator", "uploader"))
    album_artist = _first_text(info, ("album_artist", "albumartist")) or ""
    uploader = _first_text(info, ("uploader", "channel"))
    # 没有独立的专辑艺人时，歌手即专辑艺人——音乐库靠这个字段聚合专辑
    if not album_artist:
        album_artist = artist

    date = _first_text(info, ("release_date", "upload_date", "date"))
    year = _first_text(info, ("release_year",)) or (date[:4] if len(date) >= 4 else "")
    # upload_date 是 YYYYMMDD，转成 YYYY-MM-DD 更符合标签习惯
    if re.fullmatch(r"\d{8}", date):
        date = f"{date[:4]}-{date[4:6]}-{date[6:]}"

    album = _first_text(info, ("album",)) or _first_text(info, ("playlist_title",))
    if album and album.lower() in ("none", "null"):
        album = ""

    genre = _first_text(info, ("genre", "categories"))
    thumb = _first_text(info, ("thumbnail",))
    if not thumb:
        thumbs = info.get("thumbnails") or []
        if thumbs and isinstance(thumbs[-1], dict):
            thumb = _as_text(thumbs[-1].get("url"))

    kind = TrackKind.SONG
    if info.get("_type") == "playlist" or info.get("playlist_count"):
        kind = TrackKind.ALBUM if album else TrackKind.PLAYLIST

    return MusicMeta(
        title=title,
        artist=artist,
        album=album,
        album_artist=album_artist,
        track_number=track_number,
        disc_number=_parse_int(info.get("disc_number")),
        year=year,
        date=date,
        genre=genre,
        isrc=_first_text(info, ("isrc",)),
        copyright=_first_text(info, ("copyright", "license")),
        comment=_first_text(info, ("description", "comment"))[:1000],
        cover_url=thumb,
        duration=info.get("duration") if isinstance(info.get("duration"), (int, float)) else None,
        platform=platform.key if platform else "",
        platform_name=platform.name if platform else "",
        track_id=_as_text(info.get("id") or info.get("display_id")),
        album_id=_as_text(info.get("album_id")),
        kind=kind,
    )


#: 供类型标注使用的别名（真实类型是 models.MusicMeta，这里避免模块级循环导入）
MusicMetaRef = Any


# --------------------------------------------------------------------------
# 歌词
# --------------------------------------------------------------------------

#: LRC 时间标签，如 ``[01:23.45]``
LRC_TIME_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")


def looks_like_lrc(text: str) -> bool:
    """文本是否是带时间轴的 LRC 歌词。"""
    return bool(text) and bool(LRC_TIME_RE.search(text))


def parse_lrc(text: str) -> List[Tuple[float, str]]:
    """把 LRC 解析成 ``[(秒, 歌词), ...]``，按时间升序。

    一行可以有多个时间标签（``[00:01.00][00:05.00]副歌``），需要展开成多条。
    """
    out: List[Tuple[float, str]] = []
    for raw in (text or "").splitlines():
        stamps = list(LRC_TIME_RE.finditer(raw))
        if not stamps:
            continue
        content = raw[stamps[-1].end():].strip()
        if not content:
            continue
        for m in stamps:
            minute = int(m.group(1))
            second = int(m.group(2))
            frac = m.group(3) or "0"
            # 两位是百分秒，三位是毫秒，统一到秒
            millis = int(frac.ljust(3, "0")[:3]) / 1000.0
            out.append((minute * 60 + second + millis, content))
    out.sort(key=lambda t: t[0])
    return out


def merge_lrc(original: str, translation: str) -> str:
    """把翻译歌词按时间轴合并成双语 LRC。

    按时间戳对齐，译文插在原文下一行。译文有而原文没有的时间点会补进去，
    避免漏掉只在译文里出现的行（对唱段落常见）。
    """
    if not translation:
        return original
    if not original:
        return translation

    base = parse_lrc(original)
    trans = parse_lrc(translation)
    if not base or not trans:
        # 有一边不是 LRC，退化成简单拼接
        return original.rstrip() + "\n" + translation.strip() + "\n"

    # 用 0.01 秒的容差对齐，避免两边时间戳微小差异导致配不上
    trans_map: Dict[int, List[str]] = {}
    for seconds, text in trans:
        trans_map.setdefault(int(round(seconds * 100)), []).append(text)

    lines: List[str] = []
    used: set = set()
    for seconds, text in base:
        key = int(round(seconds * 100))
        lines.append(_format_lrc_line(seconds, text))
        for extra in trans_map.get(key, []):
            lines.append(_format_lrc_line(seconds, extra))
        used.add(key)
    for seconds, text in trans:
        key = int(round(seconds * 100))
        if key not in used:
            lines.append(_format_lrc_line(seconds, text))
    lines.sort(key=lambda line: _lrc_line_seconds(line))
    return "\n".join(lines) + "\n"


def _format_lrc_line(seconds: float, text: str) -> str:
    minutes, rest = divmod(seconds, 60)
    return f"[{int(minutes):02d}:{rest:05.2f}]{text}"


def _lrc_line_seconds(line: str) -> float:
    m = LRC_TIME_RE.match(line)
    if not m:
        return 0.0
    return int(m.group(1)) * 60 + int(m.group(2)) + int((m.group(3) or "0").ljust(3, "0")[:3]) / 1000.0


# --------------------------------------------------------------------------
# 文件名模板
# --------------------------------------------------------------------------

#: 文件名模板。统一不带曲目号的原因：yt-dlp 的数字格式化在字段缺失时会
#: 产出 ``NA`` 前缀，而曲目顺序本来就应该由 ID3 标签承载，不该塞进文件名。
FILENAME_TEMPLATES: Dict[str, str] = {
    "artist-title": "%(artist,creator,uploader,title)s - %(track,title)s",
    "title": "%(track,title)s",
    "title-artist": "%(track,title)s - %(artist,creator,uploader)s",
    "album-artist-title": "%(album,playlist_title,title)s - %(artist,creator,uploader,title)s - %(track,title)s",
}

DEFAULT_FILENAME_TEMPLATE = "artist-title"

#: 专辑/歌单作为**目录**，而不是文件名的一部分——这样音乐库的目录结构才正确。
ALBUM_DIR_TEMPLATE = "%(album,playlist_title,title)s"


def get_filename_template(key: str) -> str:
    """取文件名模板，未知 key 回退到默认。"""
    return FILENAME_TEMPLATES.get((key or "").strip(),
                                  FILENAME_TEMPLATES[DEFAULT_FILENAME_TEMPLATE])


def filename_template_keys() -> List[str]:
    return list(FILENAME_TEMPLATES)


def build_outtmpl(
    template_key: str = DEFAULT_FILENAME_TEMPLATE,
    *,
    album_dir: bool = False,
    ext: str = "",
) -> str:
    """拼出 yt-dlp 的 ``-o`` 模板（含可选的专辑子目录）。"""
    name = get_filename_template(template_key)
    ext_part = f".{ext}" if ext else ".%(ext)s"
    if album_dir:
        return f"{ALBUM_DIR_TEMPLATE}/{name}{ext_part}"
    return f"{name}{ext_part}"


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------

def describe(url: str) -> str:
    """给出一条音乐 URL 的人类可读诊断信息（供 --list 与排查用）。"""
    target = classify_music_url(url)
    if not target.platform:
        return f"{url} —— 不是已知音乐平台地址"
    status = "可抓取" if target.platform.supported else "当前不可抓取"
    line = f"{target.platform_name} · {target.kind.label} · {status}"
    if target.reason:
        line += f"（{target.reason}）"
    if not target.platform.supported and target.platform.note:
        line += f"\n  原因: {target.platform.note}"
    return line


__all__ = [
    "ALBUM_DIR_TEMPLATE",
    "AUDIO_EXTS",
    "DEFAULT_FILENAME_TEMPLATE",
    "DEFAULT_QUALITY",
    "FILENAME_TEMPLATES",
    "LOSSLESS_CODECS",
    "LOSSLESS_EXTS",
    "MUSIC_PLATFORMS",
    "MusicTarget",
    "PLATFORM_BY_KEY",
    "Platform",
    "QUALITY_PRESETS",
    "QualityPreset",
    "build_format_selector",
    "build_outtmpl",
    "classify_music_url",
    "describe",
    "filename_template_keys",
    "format_bitrate",
    "format_ext",
    "format_label",
    "get_filename_template",
    "get_quality",
    "is_lossless",
    "is_music_url",
    "is_supported_music_url",
    "looks_like_lrc",
    "merge_lrc",
    "meta_from_info",
    "parse_lrc",
    "pick_audio_format",
    "platform_for",
    "quality_keys",
    "sort_audio_formats",
    "split_track_prefix",
]
