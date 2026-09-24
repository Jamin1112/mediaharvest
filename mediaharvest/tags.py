"""音乐标签写入：给下载得到的音频文件补上标题/歌手/专辑/封面/歌词。

同一个接口下面有两条实现路径，对外行为完全一致：

* **mutagen 路径**（首选）：环境里装了 ``mutagen`` 就交给它，
  覆盖 ``.mp3`` / ``.m4a`` / ``.mp4`` / ``.m4b`` / ``.flac`` /
  ``.ogg`` / ``.oga`` / ``.opus``。
* **纯 Python 回退路径**：没有 mutagen 时自己拼二进制，支持 ``.mp3``
  （ID3v2.3）、``.flac``（Vorbis comment + PICTURE 元数据块重写）、
  ``.m4a`` / ``.mp4`` / ``.m4b``（``moov.udta.meta.ilst`` 原子插入）。

之所以两条路都要有，是因为 mediaharvest 可能跑在只有标准库的精简容器
里：抓取和下载不该因为缺一个可选依赖就失败，标签写不上最多算降级。
因此 :func:`write_tags` **对任何输入都不抛异常**，问题全部落在
:class:`TagWriteResult` 的 ``error`` / ``warnings`` 字段里。

已知限制
--------

1. 回退路径**不支持 OGG / Opus**：Ogg 的标签活在页面（page）级的 CRC
   校验里，改标签要重排页并重算 CRC，出错就是整个文件放不出声。
   收益太小，所以直接返回 ``ok=False`` 和「需要 mutagen」的提示。
2. MP4 回退路径只平移 ``stco`` / ``co64`` 里的样本偏移，**不处理
   ``moof``（fMP4 分片）的 ``tfhd`` 基准偏移**；分片 MP4 请装 mutagen。
3. 两条路径都是「先清掉同名旧标签再整体写入」，不做逐帧增量合并。
4. 回退路径写出来的 ID3 是 **v2.3**（兼容面最广）；v2.4 只有 mutagen
   路径才会用到。

排障开关
--------

``MEDIAHARVEST_TAG_BACKEND=builtin|mutagen`` 环境变量，或直接改模块级
变量 :data:`_BACKEND_FORCE`，都能强制指定后端，便于单独验证回退路径。
"""
from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# 可选依赖：mutagen 只在这里探测一次，导入失败不影响本模块其他功能
# --------------------------------------------------------------------------

try:  # pragma: no cover - 取决于运行环境
    import mutagen as _mutagen  # noqa: F401

    _HAS_MUTAGEN = True
except Exception:  # pragma: no cover - 缺依赖 / 依赖本身坏掉
    _HAS_MUTAGEN = False

#: 强制后端的环境变量名
ENV_BACKEND = "MEDIAHARVEST_TAG_BACKEND"

#: 后端强制开关（测试 / 排障用）。``None`` = 自动；``"builtin"`` / ``"mutagen"``
#: 为强制值。环境变量 ``MEDIAHARVEST_TAG_BACKEND`` 的优先级更高。
_BACKEND_FORCE: Optional[str] = None

# --------------------------------------------------------------------------
# 容器 / 字段常量
# --------------------------------------------------------------------------

#: 本模块认识的音频容器
#:
#: ``.ogx`` 是实际会遇到的别名：archive.org 等站点就用它提供 Ogg 音频，
#: 下载后若不认这个扩展名，标签会被静默跳过——元信息明明抓到了却写不进去，
#: 用户只会看到「标签没生效」而不知道为什么。
#:
#: 这里**不含** ``.weba``：它是 WebM/Matroska 容器，标签结构与 Ogg 完全不同，
#: 而 mutagen 1.47 也没有对应的解析器。把它当成 Ogg 处理会写坏文件，
#: 因此宁可明确报「不支持」。
SUPPORTED_EXTS = (
    ".mp3", ".m4a", ".mp4", ".m4b", ".flac",
    ".ogg", ".oga", ".ogx", ".opus",
)

#: MP4 系扩展名（同样的 atom 结构，只是用途不同）
_MP4_EXTS = (".m4a", ".mp4", ".m4b")

#: 回退路径支持的容器类别；Ogg 系故意不在其中，见模块 docstring
_BUILTIN_CONTAINERS = ("mp3", "flac", "mp4")

#: (扩展名, 容器类别)
_CONTAINER_BY_EXT: Dict[str, str] = {
    ".mp3": "mp3",
    ".flac": "flac",
    ".ogg": "ogg",
    ".oga": "ogg",
    ".ogx": "ogg",       # Ogg 的另一种常见扩展名（archive.org 在用）
    ".opus": "ogg",
}
for _ext in _MP4_EXTS:
    _CONTAINER_BY_EXT[_ext] = "mp4"

#: LRC 时间戳行首，例如 ``[00:12.34]`` / ``[01:02]``
_LRC_LINE = re.compile(r"^\s*\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]")

#: 纯文本歌词里常见的中文/英文标签行，命中则也算 LRC
_LRC_META = re.compile(r"^\s*\[(ti|ar|al|by|offset|re|ve|length):", re.IGNORECASE)

#: MIME -> MP4 covr 的类型码
_MP4_IMAGE_TYPE = {
    "image/jpeg": 13,
    "image/jpg": 13,
    "image/png": 14,
}

#: covr 类型码 -> MIME（回读用）
_MP4_IMAGE_MIME = {13: "image/jpeg", 14: "image/png"}

#: MP4 里按扩展名推断图片 MIME 的字节签名
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class TrackTags:
    """一首歌想要写入的全部元数据。

    字段一律给默认值，调用方只填自己有的那几项即可；
    :attr:`is_empty` 用来快速判断「一条都没填」，避免白跑一趟写盘。
    """

    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track_number: int = 0
    track_total: int = 0
    disc_number: int = 0
    disc_total: int = 0
    year: str = ""
    date: str = ""            # 完整日期，例如 "2024-05-01"
    genre: str = ""
    comment: str = ""
    lyrics: str = ""          # 可能是 LRC（带时间戳）也可能是纯文本
    cover_data: bytes = b""   # 封面的原始图片字节
    cover_mime: str = "image/jpeg"
    isrc: str = ""
    copyright: str = ""

    @property
    def is_empty(self) -> bool:
        """没有任何可写字段时为 True（封面只给了 MIME 不算内容）。"""
        if self.cover_data:
            return False
        for name in (
            "title", "artist", "album", "album_artist", "year", "date",
            "genre", "comment", "lyrics", "isrc", "copyright",
        ):
            if (getattr(self, name) or "").strip():
                return False
        for name in ("track_number", "track_total", "disc_number", "disc_total"):
            if getattr(self, name):
                return False
        return True


@dataclass
class TagWriteResult:
    """一次写标签的结果；失败信息只在 ``error`` 里，不抛异常。"""

    ok: bool = False
    path: str = ""
    backend: str = ""          # "mutagen" 或 "builtin"
    written: List[str] = field(default_factory=list)   # 真正写进去的字段名
    warnings: List[str] = field(default_factory=list)
    error: str = ""


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------


def _u16be(data: bytes, off: int = 0) -> int:
    return (data[off] << 8) | data[off + 1]


def _u24be(data: bytes, off: int = 0) -> int:
    return (data[off] << 16) | (data[off + 1] << 8) | data[off + 2]


def _u32be(data: bytes, off: int = 0) -> int:
    return struct.unpack_from(">I", data, off)[0]


def _u64be(data: bytes, off: int = 0) -> int:
    return struct.unpack_from(">Q", data, off)[0]


def _syncsafe(data: bytes) -> int:
    """ID3 的 syncsafe 整数：每字节只用低 7 位，避开 0xFF 同步字。"""
    return (
        ((data[0] & 0x7F) << 21)
        | ((data[1] & 0x7F) << 14)
        | ((data[2] & 0x7F) << 7)
        | (data[3] & 0x7F)
    )


def _to_syncsafe(value: int) -> bytes:
    return bytes(
        (
            (value >> 21) & 0x7F,
            (value >> 14) & 0x7F,
            (value >> 7) & 0x7F,
            value & 0x7F,
        )
    )


def _to_u32be(value: int) -> bytes:
    return struct.pack(">I", value & 0xFFFFFFFF)


def _to_u24be(value: int) -> bytes:
    return struct.pack(">I", value & 0xFFFFFF)[1:]


def _read_file(path: str) -> bytes:
    """一次性读全文件；小音频够用，也保证后面拼字节时偏移简单。"""
    with open(path, "rb") as fh:
        return fh.read()


def _guess_image_mime(data: bytes) -> str:
    """按文件头认图片类型，认不出就当 JPEG（封面绝大多数是 JPEG）。"""
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    return "image/jpeg"


def _norm_mime(mime: str, data: bytes) -> str:
    mime = (mime or "").strip().lower()
    if mime in ("image/jpg", "image/pjpeg"):
        mime = "image/jpeg"
    if mime.startswith("image/"):
        return mime
    return _guess_image_mime(data)


def _looks_like_lrc_text(lyrics: str) -> bool:
    lines = lyrics.splitlines()
    stamp = 0
    for line in lines:
        if _LRC_LINE.match(line) or _LRC_META.match(line):
            stamp += 1
    if stamp:
        return True
    # 没有时间戳时，退一步看整体像不像逐行歌词（多行、短句）
    meaningful = [ln for ln in lines if ln.strip()]
    return len(meaningful) > 2 and lines[:1] and lines[0].strip().startswith("[")


def _looks_like_mpeg_audio(data: bytes) -> bool:
    """MP3 帧同步：11 个 1 后面跟版本/层信息，即 ``0xFF Ex``。

    只在写标签前做一次体检——ID3 只是外壳，里面没有真正的音频帧时
    写进去的标签毫无意义，不如直接报错。
    """
    if len(data) < 2:
        return False
    return data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def _precheck_container(path: str, container: str) -> str:
    """写之前先确认容器结构对得上，返回错误信息（空串 = 通过）。

    这一步两条后端共用：mutagen 对「结构不对的文件」也比较宽容
    （比如给一堆垃圾数据也能挂上 ID3 头），提前拦下来能保证
    调用方拿到的一定是「真的写成功」而不是「看起来写成功」。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError as exc:
        return "读取文件失败: %s" % exc
    if not head:
        return "文件为空"

    if container == "flac":
        if not head.startswith(b"fLaC"):
            return "缺少 fLaC 标记，不是 FLAC 文件"
        return ""
    if container == "mp4":
        if len(head) < 8:
            return "文件过短，不像 MP4"
        size = _u32be(head, 0)
        if head[4:8] != b"ftyp" or size < 8:
            return "第一个原子不是 ftyp，不是 MP4"
        return ""
    if container == "ogg":
        if not head.startswith(b"OggS"):
            return "缺少 OggS 标记，不是 Ogg 文件"
        return ""
    if container == "mp3":
        # 读文件头时要**完整**读出标签长度声明的字节数，不能只看前 64 字节：
        # 大封面会让 ID3 标签远超 64 字节，截断的视图里「音频起点」正好
        # 落在帧数据中间，同步字检查必然误报。
        try:
            with open(path, "rb") as fh:
                head = fh.read(10)
                if head[:3] == b"ID3" and len(head) == 10:
                    declared = _syncsafe(head[6:10])
                    probe = fh.read(min(declared, 1 << 20) + 2)
                    body = probe[min(declared, 1 << 20):]
                else:
                    body = head
        except OSError as exc:
            return "读取文件失败: %s" % exc
        if not body:
            return "文件里只有 ID3 标签，没有音频数据"
        if not _looks_like_mpeg_audio(body):
            return "找不到 MPEG 音频帧同步字，文件可能已损坏"
        return ""
    return ""


# --------------------------------------------------------------------------
# 公共接口
# --------------------------------------------------------------------------


def mutagen_available() -> bool:
    """环境里是否有可用的 mutagen。

    只看导入是否成功——mutagen 1.47 的各个格式模块是惰性导入的，
    这里不额外做探测，免得为一个可选依赖付出启动开销。
    """
    return _HAS_MUTAGEN


def supports_format(path: str) -> bool:
    """按扩展名判断这个文件能不能写标签。

    只认白名单里的容器：不认识的扩展名宁可提前说「不支持」，
    也不要对着一个不知道结构的文件乱写。
    """
    if not path:
        return False
    ext = os.path.splitext(path)[1].lower()
    return ext in _CONTAINER_BY_EXT


def lyrics_extension(lyrics: str) -> str:
    """歌词该存成 ``.lrc`` 还是 ``.txt``。

    判据是「有没有时间戳」：播放器只有拿到 ``[mm:ss]`` 才会滚动歌词，
    纯文本硬塞进 ``.lrc`` 反而会被某些播放器显示成乱码。
    """
    if not isinstance(lyrics, str) or not lyrics.strip():
        return ".txt"
    return ".lrc" if _looks_like_lrc_text(lyrics) else ".txt"


def _container_of(path: str) -> str:
    return _CONTAINER_BY_EXT.get(os.path.splitext(path)[1].lower(), "")


def _resolve_backend() -> str:
    """决定这次用哪条路径：强制值 > 环境变量 > 自动探测。"""
    forced = (_BACKEND_FORCE or os.environ.get(ENV_BACKEND) or "").strip().lower()
    if forced in ("builtin", "mutagen"):
        return forced
    return "mutagen" if _HAS_MUTAGEN else "builtin"


def _sidecar_path(path: str, lyrics: str) -> str:
    """歌词文件与音频同目录同名，只换扩展名。"""
    stem = os.path.splitext(path)[0]
    return stem + lyrics_extension(lyrics)


def write_tags(
    path: str,
    tags: TrackTags,
    *,
    embed_cover: bool = True,
    embed_lyrics: bool = True,
    write_lyrics_file: bool = False,
) -> TagWriteResult:
    """把 ``tags`` 写进 ``path`` 指向的音频文件。

    :param embed_cover: 是否把封面塞进容器内部
    :param embed_lyrics: 是否把歌词塞进容器内部
    :param write_lyrics_file: 是否额外落一个同名 ``.lrc`` / ``.txt`` 歌词文件
    :returns: :class:`TagWriteResult`；**任何情况下都不抛异常**

    先做输入体检（存在性、是不是普通文件、扩展名、是否空标签），
    再按后端分派。回退路径遇到自己搞不定的结构宁可返回精确的 ``error``，
    也不写出一个「看起来成功、实际损坏」的文件。
    """
    result = TagWriteResult(path=path or "", backend=_resolve_backend())

    # ---- 输入体检：这些错误必须在碰磁盘之前拦下来 ----
    if not path or not isinstance(path, str):
        result.error = "路径为空"
        return result
    if not os.path.exists(path):
        result.error = "文件不存在: %s" % path
        return result
    if os.path.isdir(path):
        result.error = "路径是目录，不是音频文件: %s" % path
        return result
    if not os.path.isfile(path):
        result.error = "路径不是普通文件: %s" % path
        return result

    container = _container_of(path)
    if not container:
        result.error = "不支持的音频扩展名: %s" % (os.path.splitext(path)[1] or "(无)")
        return result

    if not isinstance(tags, TrackTags):
        result.error = "tags 参数类型错误: %r" % type(tags).__name__
        return result

    # 结构体检放在「空标签」之前：垃圾文件必须报错，不能因为没内容就放过
    structure_error = _precheck_container(path, container)
    if structure_error:
        result.error = structure_error
        return result

    if tags.is_empty:
        result.warnings.append("没有任何标签内容，跳过写入")
        result.ok = True
        return result

    # ---- 写标签本体：后端内部已经吞掉所有异常，这里再加一道保险 ----
    try:
        if result.backend == "mutagen":
            if not _HAS_MUTAGEN:
                result.error = "强制 mutagen 后端，但 mutagen 不可用"
                return result
            written = _write_mutagen(path, tags, embed_cover=embed_cover,
                                     embed_lyrics=embed_lyrics,
                                     warnings=result.warnings)
        else:
            if container not in _BUILTIN_CONTAINERS:
                result.error = (
                    "纯 Python 回退后端不支持 %s（%s），需要 mutagen"
                    % (os.path.splitext(path)[1].lower(), container)
                )
                return result
            written = _write_builtin(path, container, tags,
                                     embed_cover=embed_cover,
                                     embed_lyrics=embed_lyrics,
                                     warnings=result.warnings)
    except Exception as exc:  # pragma: no cover - 兜底，正常路径不该走到
        result.error = "%s 后端写入失败: %s" % (result.backend, exc)
        return result

    if written is None:
        result.error = result.warnings.pop() if result.warnings else "写入失败"
        return result

    result.written.extend(written)
    result.ok = True

    # ---- 歌词旁挂文件与容器标签相互独立，失败只降级成 warning ----
    if write_lyrics_file:
        if tags.lyrics.strip():
            sidecar = _sidecar_path(path, tags.lyrics)
            try:
                with open(sidecar, "w", encoding="utf-8") as fh:
                    fh.write(tags.lyrics)
                result.written.append("lyrics_file")
            except OSError as exc:
                result.warnings.append("歌词文件写入失败: %s" % exc)
        else:
            result.warnings.append("歌词为空，未生成歌词文件")

    return result


def read_tags(path: str) -> Optional[TrackTags]:
    """回读标签，用于校验/排障；读不出来返回 ``None``。

    与 :func:`write_tags` 不同，这里**不理会强制后端开关**：写的时候
    可以逼着走回退路径，读的时候必须用当前环境里最强的解析器，
    否则「写进去没写进去」的校验就没意义了。
    """
    if not path or not os.path.isfile(path):
        return None
    container = _container_of(path)
    if not container:
        return None

    if _HAS_MUTAGEN:
        tags = _read_mutagen(path, container)
        if tags is not None:
            return tags
        return None
    return _read_builtin(path, container)


# --------------------------------------------------------------------------
# mutagen 后端
# --------------------------------------------------------------------------


def _mutagen_mp3(path: str, tags: TrackTags, embed_cover: bool,
                 embed_lyrics: bool, warnings: List[str]) -> List[str]:
    """ID3：v2.4 优先，文件里已是 v2.3 就跟着用 v2.3。"""
    from mutagen.id3 import (
        APIC, COMM, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX,
        USLT, ID3,
    )

    try:
        audio = ID3(path)
        v2_version = 4 if audio.version[1] >= 4 else 3
    except Exception:
        # 没有标签头（或头部坏了）时从零开始，按 v2.4 写
        audio = ID3()
        v2_version = 4

    if not _HAS_MUTAGEN:
        return []

    written: List[str] = []

    # 清掉要覆盖的帧：TRCK/TPOS 这类是整帧语义，合并反而更容易写错
    for key in ("TIT2", "TPE1", "TALB", "TPE2", "TRCK", "TPOS", "TCON",
                "TYER", "TDRC", "TDAT", "COMM", "USLT", "APIC", "TXXX:ISRC"):
        try:
            if key in audio:
                del audio[key]
        except Exception:
            pass

    if tags.title.strip():
        audio.add(TIT2(encoding=3, text=[tags.title]))
        written.append("title")
    if tags.artist.strip():
        audio.add(TPE1(encoding=3, text=[tags.artist]))
        written.append("artist")
    if tags.album.strip():
        audio.add(TALB(encoding=3, text=[tags.album]))
        written.append("album")
    if tags.album_artist.strip():
        audio.add(TPE2(encoding=3, text=[tags.album_artist]))
        written.append("album_artist")

    if tags.track_number:
        audio.add(TRCK(encoding=3, text=[_pair_text(tags.track_number, tags.track_total)]))
        written.append("track_number")
    if tags.disc_number:
        audio.add(TPOS(encoding=3, text=[_pair_text(tags.disc_number, tags.disc_total)]))
        written.append("disc_number")

    if tags.date.strip():
        audio.add(TDRC(encoding=3, text=[tags.date]))
        written.append("date")
    elif tags.year.strip():
        audio.add(TDRC(encoding=3, text=[tags.year]))
        written.append("year")
    if tags.genre.strip():
        audio.add(TCON(encoding=3, text=[tags.genre]))
        written.append("genre")
    if tags.comment.strip():
        audio.add(COMM(encoding=3, lang="eng", desc="", text=[tags.comment]))
        written.append("comment")

    if embed_lyrics:
        if tags.lyrics.strip():
            audio.add(USLT(encoding=3, lang="eng", desc="", text=tags.lyrics))
            written.append("lyrics")
        elif tags.lyrics:
            warnings.append("歌词只有空白字符，已跳过 USLT")

    if embed_cover:
        if tags.cover_data:
            mime = _norm_mime(tags.cover_mime, tags.cover_data)
            audio.add(APIC(encoding=3, mime=mime, type=3, desc="",
                           data=tags.cover_data))
            written.append("cover_data")
        elif tags.cover_data is not None and tags.cover_mime:
            warnings.append("封面数据为空，未写入 APIC")

    if tags.isrc.strip():
        # ISRC 没有标准 ID3v2 帧，行业惯例是 TXXX:ISRC
        audio.add(TXXX(encoding=3, desc="ISRC", text=[tags.isrc]))
        written.append("isrc")

    if tags.copyright.strip():
        try:
            from mutagen.id3 import TCOP

            if "TCOP" in audio:
                del audio["TCOP"]
            audio.add(TCOP(encoding=3, text=[tags.copyright]))
            written.append("copyright")
        except Exception as exc:
            warnings.append("TCOP 写入失败: %s" % exc)

    try:
        audio.save(path, v2_version=v2_version)
    except Exception as exc:
        # v2.4 写不下去（极少见）时退回 v2.3 再试一次
        try:
            audio.save(path, v2_version=3)
            warnings.append("ID3v2.4 保存失败，已回退到 v2.3: %s" % exc)
        except Exception as exc2:
            raise RuntimeError("ID3 保存失败: %s" % exc2)
    return written


def _mutagen_mp4(path: str, tags: TrackTags, embed_cover: bool,
                 embed_lyrics: bool, warnings: List[str]) -> List[str]:
    """MP4：所有值都是 list，键名按 mutagen 的 iTunes 约定。"""
    from mutagen.mp4 import AtomDataType, MP4, MP4Cover

    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    mp4 = audio.tags
    written: List[str] = []

    def put(key: str, value: Any, name: str) -> None:
        mp4[key] = value
        written.append(name)

    if tags.title.strip():
        put("\xa9nam", [tags.title], "title")
    if tags.artist.strip():
        put("\xa9ART", [tags.artist], "artist")
    if tags.album.strip():
        put("\xa9alb", [tags.album], "album")
    if tags.album_artist.strip():
        put("aART", [tags.album_artist], "album_artist")
    if tags.track_number:
        put("trkn", [(int(tags.track_number), int(tags.track_total))], "track_number")
    if tags.disc_number:
        put("disk", [(int(tags.disc_number), int(tags.disc_total))], "disc_number")
    if tags.date.strip():
        put("\xa9day", [tags.date], "date")
    elif tags.year.strip():
        put("\xa9day", [tags.year], "year")
    if tags.genre.strip():
        put("\xa9gen", [tags.genre], "genre")
    if tags.comment.strip():
        put("\xa9cmt", [tags.comment], "comment")
    if tags.copyright.strip():
        put("cprt", [tags.copyright], "copyright")

    if tags.isrc.strip():
        # ISRC 放在 iTunes 的自由标签 ``----`` 里。mean/name 由**键名**
        # 提供（``----:mean:name``），MP4FreeForm 自己只装正文，
        # 所以这里不能也不需要给构造函数传 mean/name。
        try:
            from mutagen.mp4 import MP4FreeForm

            mp4["----:com.apple.iTunes:ISRC"] = [
                MP4FreeForm(tags.isrc.encode("utf-8"), dataformat=AtomDataType.UTF8)
            ]
            written.append("isrc")
        except Exception as exc:
            warnings.append("MP4 ISRC 写入失败: %s" % exc)

    if embed_lyrics:
        if tags.lyrics.strip():
            put("\xa9lyr", [tags.lyrics], "lyrics")
        elif tags.lyrics:
            warnings.append("歌词只有空白字符，已跳过 ©lyr")

    if embed_cover:
        if tags.cover_data:
            mime = _norm_mime(tags.cover_mime, tags.cover_data)
            fmt = (MP4Cover.FORMAT_PNG if mime == "image/png"
                   else MP4Cover.FORMAT_JPEG)
            put("covr", [MP4Cover(tags.cover_data, imageformat=fmt)], "cover_data")
        else:
            warnings.append("封面数据为空，未写入 covr")

    audio.save()
    return written


def _mutagen_vorbis(path: str, container: str, tags: TrackTags,
                    embed_cover: bool, embed_lyrics: bool,
                    warnings: List[str]) -> List[str]:
    """FLAC / Ogg 系共用一套 Vorbis comment，只是容器类不同。"""
    if container == "flac":
        from mutagen.flac import FLAC as _Audio
    elif container == "ogg":
        ext = os.path.splitext(path)[1].lower()
        if ext == ".opus":
            from mutagen.oggopus import OggOpus as _Audio
        else:
            from mutagen.oggvorbis import OggVorbis as _Audio
    else:
        raise RuntimeError("未知容器: %s" % container)

    audio = _Audio(path)
    if audio.tags is None:
        audio.add_tags()
    vc = audio.tags
    written: List[str] = []

    def put(field_name: str, value: str, name: str) -> None:
        vc[field_name] = [value]
        written.append(name)

    if tags.title.strip():
        put("TITLE", tags.title, "title")
    if tags.artist.strip():
        put("ARTIST", tags.artist, "artist")
    if tags.album.strip():
        put("ALBUM", tags.album, "album")
    if tags.album_artist.strip():
        put("ALBUMARTIST", tags.album_artist, "album_artist")
    if tags.track_number:
        put("TRACKNUMBER", str(tags.track_number), "track_number")
    if tags.track_total:
        put("TRACKTOTAL", str(tags.track_total), "track_total")
    if tags.disc_number:
        put("DISCNUMBER", str(tags.disc_number), "disc_number")
    if tags.disc_total:
        put("DISCTOTAL", str(tags.disc_total), "disc_total")
    if tags.date.strip():
        put("DATE", tags.date, "date")
    if tags.year.strip():
        put("YEAR", tags.year, "year")
    if tags.genre.strip():
        put("GENRE", tags.genre, "genre")
    if tags.comment.strip():
        put("COMMENT", tags.comment, "comment")
    if tags.isrc.strip():
        put("ISRC", tags.isrc, "isrc")
    if tags.copyright.strip():
        put("COPYRIGHT", tags.copyright, "copyright")

    if embed_lyrics:
        if tags.lyrics.strip():
            # 两个字段都写：LYRICS 是 Vorbis 事实标准，UNSYNCEDLYRICS 兼容面更广
            put("LYRICS", tags.lyrics, "lyrics")
            vc["UNSYNCEDLYRICS"] = [tags.lyrics]
        elif tags.lyrics:
            warnings.append("歌词只有空白字符，已跳过 LYRICS")

    if container == "flac":
        if embed_cover:
            if not tags.cover_data:
                warnings.append("封面数据为空，未写入 PICTURE")
            else:
                try:
                    from mutagen.flac import Picture

                    audio.clear_pictures()
                    pic = Picture()
                    pic.type = 3               # front cover
                    pic.mime = _norm_mime(tags.cover_mime, tags.cover_data)
                    pic.desc = ""
                    pic.data = tags.cover_data
                    audio.add_picture(pic)
                    written.append("cover_data")
                except Exception as exc:
                    warnings.append("FLAC 封面写入失败: %s" % exc)
        audio.save()
    else:
        if embed_cover and tags.cover_data:
            warnings.append("Ogg 容器不支持内嵌封面，已跳过（可用 write_lyrics_file 旁挂）")
        try:
            audio.save()
        except Exception as exc:
            raise RuntimeError("Ogg 标签保存失败: %s" % exc)
    return written


def _write_mutagen(path: str, tags: TrackTags, embed_cover: bool,
                   embed_lyrics: bool, warnings: List[str]) -> Optional[List[str]]:
    """mutagen 分派；失败时把原因塞进 ``warnings`` 末尾并返回 ``None``。"""
    container = _container_of(path)
    try:
        if container == "mp3":
            return _mutagen_mp3(path, tags, embed_cover, embed_lyrics, warnings)
        if container == "mp4":
            return _mutagen_mp4(path, tags, embed_cover, embed_lyrics, warnings)
        if container in ("flac", "ogg"):
            return _mutagen_vorbis(path, container, tags, embed_cover,
                                   embed_lyrics, warnings)
    except Exception as exc:
        warnings.append("mutagen 写入失败: %s" % exc)
        return None
    warnings.append("mutagen 不支持该容器: %s" % container)
    return None


def _read_mutagen(path: str, container: str) -> Optional[TrackTags]:
    """用 mutagen 回读；任何异常都当成「读不出来」。"""
    try:
        if container == "mp3":
            from mutagen.id3 import ID3

            audio = ID3(path)
            audio.update_to_v24()
            tags = TrackTags()
            # 文本帧一律经 _frame_text 抽出字符串：直接把 mutagen 的 Frame
            # 对象塞进 dataclass，字段类型就悄悄变成 Frame，调用方一比较即错
            for field_name, frame_id in (("title", "TIT2"), ("artist", "TPE1"),
                                         ("album", "TALB"), ("album_artist", "TPE2"),
                                         ("genre", "TCON"), ("copyright", "TCOP")):
                setattr(tags, field_name,
                        _frame_text(_first(audio.getall(frame_id))))
            num, total = _split_pair(_frame_text(_first(audio.getall("TRCK"))))
            tags.track_number, tags.track_total = num, total
            num, total = _split_pair(_frame_text(_first(audio.getall("TPOS"))))
            tags.disc_number, tags.disc_total = num, total
            value = _frame_text(
                _first(audio.getall("TDRC") or audio.getall("TYER")))
            if value:
                tags.date = value
                tags.year = value[:4]
            # v2.3 的 TYER 只有四位年份；精确日期被回退后端放在 TXXX:DATE
            # 里，这里要优先采用它，否则「写了 2024-05-01 读回来 2024」
            precise = _frame_text(_first(audio.getall("TXXX:DATE")))
            if precise:
                tags.date = precise
                if not tags.year:
                    tags.year = precise[:4]
            comm = _first(audio.getall("COMM"))
            tags.comment = (comm.text[0] if comm and getattr(comm, "text", None) else "")
            uslt = _first(audio.getall("USLT"))
            tags.lyrics = (uslt.text if uslt and getattr(uslt, "text", None) else "")
            isrc = _first(audio.getall("TXXX:ISRC"))
            if isrc is not None:
                tags.isrc = (isrc.text[0] if getattr(isrc, "text", None) else "")
            for apic in audio.getall("APIC"):
                if apic.data:
                    tags.cover_data = bytes(apic.data)
                    tags.cover_mime = apic.mime or "image/jpeg"
                    break
            return tags

        if container == "mp4":
            from mutagen.mp4 import MP4

            audio = MP4(path)
            if audio.tags is None:
                return None
            m4 = audio.tags
            tags = TrackTags()
            tags.title = _first(m4.get("\xa9nam")) or ""
            tags.artist = _first(m4.get("\xa9ART")) or ""
            tags.album = _first(m4.get("\xa9alb")) or ""
            tags.album_artist = _first(m4.get("aART")) or ""
            pair = m4.get("trkn") or [(0, 0)]
            tags.track_number, tags.track_total = int(pair[0][0]), int(pair[0][1])
            pair = m4.get("disk") or [(0, 0)]
            tags.disc_number, tags.disc_total = int(pair[0][0]), int(pair[0][1])
            day = _first(m4.get("\xa9day")) or ""
            tags.date = day
            tags.year = day[:4]
            tags.genre = _first(m4.get("\xa9gen")) or ""
            tags.comment = _first(m4.get("\xa9cmt")) or ""
            tags.lyrics = _first(m4.get("\xa9lyr")) or ""
            tags.copyright = _first(m4.get("cprt")) or ""
            free = m4.get("----:com.apple.iTunes:ISRC")
            if free:
                tags.isrc = bytes(free[0]).decode("utf-8", "replace")
            covers = m4.get("covr") or []
            if covers:
                tags.cover_data = bytes(covers[0])
                tags.cover_mime = _MP4_IMAGE_MIME.get(
                    getattr(covers[0], "imageformat", 13), "image/jpeg")
            return tags

        if container == "flac":
            from mutagen.flac import FLAC

            audio = FLAC(path)
            tags = _vorbis_to_tags(audio.tags or {}, path)
            pics = audio.pictures
            if pics:
                tags.cover_data = bytes(pics[0].data)
                tags.cover_mime = pics[0].mime or "image/jpeg"
            return tags

        if container == "ogg":
            ext = os.path.splitext(path)[1].lower()
            if ext == ".opus":
                from mutagen.oggopus import OggOpus as _Audio
            else:
                from mutagen.oggvorbis import OggVorbis as _Audio

            audio = _Audio(path)
            return _vorbis_to_tags(audio.tags or {}, path)
    except Exception:
        return None
    return None


def _first(values: Any) -> Any:
    """取标签值列表的第一项；没有就返回 ``None``。"""
    if not values:
        return None
    try:
        return values[0]
    except (IndexError, KeyError, TypeError):
        return None


def _frame_text(frame: Any) -> str:
    """把 mutagen 的文本帧取出成 ``str``（不是 Frame 对象）。

    mutagen 的 ``__str__`` 对多数文本帧返回第一个值，但 TXXX 之类会带上
    描述前缀，所以优先走 ``frame.text`` 字段，拿不到再退回 ``str()``。
    """
    if frame is None:
        return ""
    text = getattr(frame, "text", None)
    if isinstance(text, (list, tuple)) and text:
        return str(text[0])
    if isinstance(text, str):
        return text
    try:
        return str(frame)
    except Exception:
        return ""


def _pair_text(number: int, total: int) -> str:
    """``3/12`` 这种「序号/总数」文本，总数缺失时只写序号。"""
    return "%d/%d" % (number, total) if total else "%d" % number


def _split_pair(text: Optional[str]) -> Tuple[int, int]:
    """解析 ``3/12``；坏数据一律当 0，不抛异常。"""
    if not text:
        return 0, 0
    head, _, tail = str(text).partition("/")
    try:
        number = int(head.strip() or 0)
    except ValueError:
        number = 0
    try:
        total = int(tail.strip() or 0)
    except ValueError:
        total = 0
    return number, total


def _vorbis_to_tags(vc: Any, path: str) -> TrackTags:
    """Vorbis comment -> :class:`TrackTags`，大小写与别名都兼容。"""

    def get(*names: str) -> str:
        for name in names:
            for key in (name, name.lower()):
                try:
                    value = vc.get(key)
                except Exception:
                    value = None
                if value:
                    return value[0] if isinstance(value, (list, tuple)) else str(value)
        return ""

    def get_int(*names: str) -> int:
        try:
            return int(str(get(*names)).strip() or 0)
        except ValueError:
            return 0

    tags = TrackTags()
    tags.title = get("TITLE")
    tags.artist = get("ARTIST")
    tags.album = get("ALBUM")
    tags.album_artist = get("ALBUMARTIST", "ALBUM ARTIST")
    tags.track_number = get_int("TRACKNUMBER")
    tags.track_total = get_int("TRACKTOTAL", "TOTALTRACKS")
    tags.disc_number = get_int("DISCNUMBER")
    tags.disc_total = get_int("DISCTOTAL", "TOTALDISCS")
    tags.date = get("DATE")
    tags.year = get("YEAR") or tags.date[:4]
    tags.genre = get("GENRE")
    tags.comment = get("COMMENT", "DESCRIPTION")
    tags.lyrics = get("LYRICS", "UNSYNCEDLYRICS")
    tags.isrc = get("ISRC")
    tags.copyright = get("COPYRIGHT")
    return tags


# --------------------------------------------------------------------------
# 纯 Python 回退后端 —— 总入口
# --------------------------------------------------------------------------


def _write_builtin(path: str, container: str, tags: TrackTags, embed_cover: bool,
                   embed_lyrics: bool, warnings: List[str]) -> Optional[List[str]]:
    """回退后端分派；结构不认识时返回 ``None`` 并把原因放进 ``warnings``。"""
    try:
        raw = _read_file(path)
    except OSError as exc:
        warnings.append("读取文件失败: %s" % exc)
        return None

    if container == "mp3":
        data, written, err = _builtin_id3(raw, tags, embed_cover, embed_lyrics)
    elif container == "flac":
        data, written, err = _builtin_flac(raw, tags, embed_cover, embed_lyrics)
    elif container == "mp4":
        data, written, err = _builtin_mp4(raw, tags, embed_cover, embed_lyrics)
    else:
        warnings.append("纯 Python 回退不支持容器: %s" % container)
        return None

    if err:
        warnings.append(err)
        return None

    try:
        _atomic_write(path, data)
    except OSError as exc:
        warnings.append("写盘失败: %s" % exc)
        return None
    return written


def _atomic_write(path: str, data: bytes) -> None:
    """先写临时文件再替换：中途崩了也不会留下半截音频。"""
    tmp = path + ".mhtags.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# 回退后端：MP3 / ID3v2.3
# --------------------------------------------------------------------------


def _strip_id3v2(raw: bytes) -> Tuple[bytes, int]:
    """去掉文件头部的 ID3v2 标签，返回 ``(剩余数据, 原标签长度)``。

    只认 ``ID3`` 魔数 + syncsafe 长度；尾部那 10 字节是 footer，
    只有当 header 的 footer 标志（0x10）置位时才存在。
    """
    if len(raw) < 10 or raw[:3] != b"ID3":
        return raw, 0
    size = _syncsafe(raw[6:10])
    total = 10 + size
    if raw[5] & 0x10:
        total += 10
    if total > len(raw):          # 长度字段明显坏了：只当标签吃掉整段
        total = len(raw)
    return raw[total:], total


def _id3_text_frame(frame_id: str, text: str, version: int = 3) -> bytes:
    """ID3 文本帧：编码字节 0x01（UTF-16 带 BOM）+ 内容。

    用 UTF-16 而不是 UTF-8，因为 ID3v2.3 规范里没有 UTF-8 编码值，
    v2.3 的读取器（包括部分老播放器）会按 Latin-1 解出乱码。
    """
    payload = b"\x01" + text.encode("utf-16")   # encode("utf-16") 自带 BOM
    return _id3_frame(frame_id, payload, version)


def _id3_frame(frame_id: str, payload: bytes, version: int = 3) -> bytes:
    """拼一个 ID3v2.3 帧。

    这里刻意用**普通大端**长度而不是 syncsafe：ID3v2.3 规范如此，
    mutagen 在 v2.3 下也只按普通大端解析（见 ``read_frames`` 的
    ``if id3.version < _V24: bpi = int``），跟着规范走才能被正确读回。
    """
    size = _to_u32be(len(payload))
    flags = b"\x00\x00"
    if version >= 4:
        size = _to_syncsafe(len(payload))
    return frame_id.encode("ascii") + size + flags + payload


def _id3_comm(frame_id: str, text: str, lang: str = "eng") -> bytes:
    """COMM / USLT 这类「语言 + 描述 + 正文」帧。"""
    payload = b"\x01" + lang.encode("ascii")[:3].ljust(3, b" ") + b""
    payload += b"\x00\x00"                    # 描述串的 UTF-16 结束符
    payload += text.encode("utf-16")
    return _id3_frame(frame_id, payload)


def _id3_apic(data: bytes, mime: str) -> bytes:
    """APIC：编码 + MIME(ASCII, 0 结尾) + 图片类型 + 描述 + 图片数据。"""
    payload = (
        b"\x00"
        + mime.encode("ascii", "replace") + b"\x00"
        + b"\x03"                             # front cover
        + b"\x00"                             # 空描述（Latin-1 单字节结束符）
        + data
    )
    return _id3_frame("APIC", payload)


def _builtin_id3(raw: bytes, tags: TrackTags, embed_cover: bool,
                 embed_lyrics: bool) -> Tuple[bytes, List[str], str]:
    """写 ID3v2.3 标签：剥掉旧标签 -> 拼新标签 -> 原封不动接上音频。"""
    audio, _ = _strip_id3v2(raw)
    if not audio:
        return b"", [], "文件里没有音频数据（只有 ID3 标签？）"

    frames: List[bytes] = []
    written: List[str] = []

    def text(frame_id: str, value: str, name: str) -> None:
        if value and value.strip():
            frames.append(_id3_text_frame(frame_id, value))
            written.append(name)

    text("TIT2", tags.title, "title")
    text("TPE1", tags.artist, "artist")
    text("TALB", tags.album, "album")
    text("TPE2", tags.album_artist, "album_artist")
    if tags.track_number:
        frames.append(_id3_text_frame(
            "TRCK", _pair_text(tags.track_number, tags.track_total)))
        written.append("track_number")
    if tags.disc_number:
        frames.append(_id3_text_frame(
            "TPOS", _pair_text(tags.disc_number, tags.disc_total)))
        written.append("disc_number")
    # v2.3 只有四位年份的 TYER；完整日期放 TXXX:DATE 里，需要精确日期的
    # 工具（比如 Picard）会读它，同时不影响老播放器读 TYER。
    if tags.year.strip():
        text("TYER", tags.year.strip()[:4], "year")
    if tags.date.strip():
        if not tags.year.strip():
            text("TYER", tags.date.strip()[:4], "year")
        frames.append(_id3_txxx("DATE", tags.date))
        written.append("date")
    text("TCON", tags.genre, "genre")
    if tags.comment.strip():
        frames.append(_id3_comm("COMM", tags.comment))
        written.append("comment")
    if embed_lyrics and tags.lyrics.strip():
        frames.append(_id3_comm("USLT", tags.lyrics))
        written.append("lyrics")
    if tags.isrc.strip():
        frames.append(_id3_txxx("ISRC", tags.isrc))
        written.append("isrc")
    text("TCOP", tags.copyright, "copyright")
    if embed_cover and tags.cover_data:
        frames.append(_id3_apic(tags.cover_data,
                                _norm_mime(tags.cover_mime, tags.cover_data)))
        written.append("cover_data")

    if not frames:
        return audio, [], "没有任何可写入的 ID3 帧"

    body = b"".join(frames)
    # ID3v2.3 的标签总长度字段也是 syncsafe（v2.4 才把 syncsafe 用在帧上）
    header = b"ID3\x03\x00\x00" + _to_syncsafe(len(body))
    return header + body + audio, written, ""


def _id3_txxx(desc: str, value: str) -> bytes:
    """TXXX：编码 + 描述(UTF-16+结束符) + 值(UTF-16)。"""
    payload = b"\x01" + desc.encode("utf-16") + b"\x00\x00" + value.encode("utf-16")
    return _id3_frame("TXXX", payload)


# --------------------------------------------------------------------------
# 回退后端：FLAC
# --------------------------------------------------------------------------

_FLAC_STREAMINFO = 0
_FLAC_PADDING = 1
_FLAC_VORBIS_COMMENT = 4
_FLAC_PICTURE = 6


def _flac_split(raw: bytes) -> Tuple[bytes, List[Tuple[int, bytes]], bytes, str]:
    """拆开 FLAC：返回 ``(流信息块, 其他元数据块, 音频帧, 错误)``。

    ``STREAMINFO`` 必须原样保留且排在最前——它是采样率/总样本数的唯一
    来源，改一个字节文件就废了。这里只解析不改写。
    """
    if len(raw) < 4 or raw[:4] != b"fLaC":
        return b"", [], b"", "缺少 fLaC 标记，不是 FLAC 文件"
    pos = 4
    streaminfo = b""
    blocks: List[Tuple[int, bytes]] = []
    last = False
    while not last:
        if pos + 4 > len(raw):
            return b"", [], b"", "元数据块链在声明结束前就断了"
        head = raw[pos]
        last = bool(head & 0x80)
        code = head & 0x7F
        size = _u24be(raw, pos + 1)
        if pos + 4 + size > len(raw):
            return b"", [], b"", "元数据块长度越界（声明 %d 字节）" % size
        payload = raw[pos + 4:pos + 4 + size]
        if code == _FLAC_STREAMINFO:
            if size != 34:
                return b"", [], b"", "STREAMINFO 长度应为 34，实际 %d" % size
            streaminfo = payload
        elif code in (_FLAC_VORBIS_COMMENT, _FLAC_PADDING, _FLAC_PICTURE):
            # 这三块会被重建，只留结构信息，正文丢掉
            blocks.append((code, payload))
        else:
            blocks.append((code, payload))
        pos += 4 + size
    return streaminfo, blocks, raw[pos:], ""


def _vorbis_comment_block(tags: TrackTags, embed_cover: bool,
                          embed_lyrics: bool) -> bytes:
    """组装 Vorbis comment 的正文（小端长度 + ``KEY=VALUE``）。"""
    items: List[Tuple[str, str]] = []

    def add(key: str, value: str) -> None:
        if value and value.strip():
            items.append((key, value))

    add("TITLE", tags.title)
    add("ARTIST", tags.artist)
    add("ALBUM", tags.album)
    add("ALBUMARTIST", tags.album_artist)
    if tags.track_number:
        items.append(("TRACKNUMBER", str(tags.track_number)))
    if tags.track_total:
        items.append(("TRACKTOTAL", str(tags.track_total)))
    if tags.disc_number:
        items.append(("DISCNUMBER", str(tags.disc_number)))
    if tags.disc_total:
        items.append(("DISCTOTAL", str(tags.disc_total)))
    add("DATE", tags.date)
    add("YEAR", tags.year)
    add("GENRE", tags.genre)
    add("COMMENT", tags.comment)
    add("ISRC", tags.isrc)
    add("COPYRIGHT", tags.copyright)
    if embed_lyrics:
        add("LYRICS", tags.lyrics)
        add("UNSYNCEDLYRICS", tags.lyrics)

    vendor = b"mediaharvest-tags"
    out = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(items))
    for key, value in items:
        entry = ("%s=%s" % (key, value)).encode("utf-8")
        out += struct.pack("<I", len(entry)) + entry
    return out


def _flac_picture_block(data: bytes, mime: str) -> bytes:
    """FLAC PICTURE 块（type 6）的正文字节。"""
    mime_b = mime.encode("ascii", "replace")
    out = struct.pack(">I", 3)                    # 图片类型：正面封面
    out += struct.pack(">I", len(mime_b)) + mime_b
    out += struct.pack(">I", 0)                   # 描述长度 0
    out += struct.pack(">5I", 0, 0, 0, 0, len(data))   # 宽高深色数未知填 0
    out += data
    return out


def _flac_block(code: int, payload: bytes, last: bool) -> bytes:
    flag = 0x80 if last else 0x00
    return bytes((flag | (code & 0x7F),)) + _to_u24be(len(payload)) + payload


def _builtin_flac(raw: bytes, tags: TrackTags, embed_cover: bool,
                  embed_lyrics: bool) -> Tuple[bytes, List[str], str]:
    """重建 FLAC 元数据块链，音频帧原样接回。"""
    streaminfo, blocks, audio, err = _flac_split(raw)
    if err:
        return b"", [], err

    written: List[str] = []
    for name, value in (
        ("title", tags.title), ("artist", tags.artist), ("album", tags.album),
        ("album_artist", tags.album_artist), ("year", tags.year),
        ("date", tags.date), ("genre", tags.genre), ("comment", tags.comment),
        ("isrc", tags.isrc), ("copyright", tags.copyright),
    ):
        if value and value.strip():
            written.append(name)
    for name, value in (
        ("track_number", tags.track_number), ("track_total", tags.track_total),
        ("disc_number", tags.disc_number), ("disc_total", tags.disc_total),
    ):
        if value:
            written.append(name)
    if embed_lyrics and tags.lyrics.strip():
        written.append("lyrics")

    # 重新排块：STREAMINFO 永远第一，其余非 comment/picture 块按原顺序跟上
    rest = [(code, payload) for code, payload in blocks
            if code not in (_FLAC_VORBIS_COMMENT, _FLAC_PICTURE)]
    if not streaminfo:
        return b"", [], "找不到 STREAMINFO 块，文件结构异常"

    out_blocks: List[Tuple[int, bytes]] = [
        (_FLAC_STREAMINFO, streaminfo),
        (_FLAC_VORBIS_COMMENT,
         _vorbis_comment_block(tags, embed_cover, embed_lyrics)),
    ]
    out_blocks.extend(rest)
    if embed_cover and tags.cover_data:
        out_blocks.append((_FLAC_PICTURE, _flac_picture_block(
            tags.cover_data, _norm_mime(tags.cover_mime, tags.cover_data))))
        written.append("cover_data")

    # 没有 padding 块的话，下次改标签要整体搬动音频帧，顺手补一个
    if not any(code == _FLAC_PADDING for code, _ in out_blocks):
        out_blocks.append((_FLAC_PADDING, b"\x00" * 2048))

    body = b""
    for index, (code, payload) in enumerate(out_blocks):
        body += _flac_block(code, payload, index == len(out_blocks) - 1)
    return b"fLaC" + body + audio, written, ""


# --------------------------------------------------------------------------
# 回退后端：MP4 / M4A
# --------------------------------------------------------------------------

#: 需要递归下钻的容器原子。``ilst`` 故意不在里面——它的子原子一律当
#: 不透明数据整体替换，免得被某些不规范文件里的怪尺寸卡住。
_MP4_CONTAINERS = {
    b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"meta",
}


def _mp4_scan(buf: bytes, start: int, end: int) -> Tuple[List[Tuple[int, int, bytes, bool]], str]:
    """扫描 ``[start, end)`` 的原子链。

    返回 ``(items, error)``；``items`` 每项是
    ``(offset, size, name, is_container)``。解析不通就返回错误字符串，
    绝不抛异常——回退路径的失败都要能变成一条可读的提示。
    """
    items: List[Tuple[int, int, bytes, bool]] = []
    pos = start
    while pos < end:
        if end - pos < 8:
            return items, "原子链表尾部残留 %d 字节" % (end - pos)
        try:
            size = _u32be(buf, pos)
        except struct.error:
            return items, "原子头读取越界"
        name = buf[pos + 4:pos + 8]
        header = 8
        if size == 1:
            if end - pos < 16:
                return items, "64 位原子头不完整"
            size = _u64be(buf, pos + 8)
            header = 16
        if size == 0:
            # 长度 0 表示「一直到文件尾」，只允许出现在最后
            size = end - pos
        if size < header or pos + size > end:
            return items, "原子 %s 长度 %d 越界" % (name.decode("latin-1"), size)
        if name in _MP4_CONTAINERS:
            inner = pos + header + (4 if name == b"meta" else 0)
            if inner > pos + size:
                return items, "容器原子 %s 过短" % name.decode("latin-1")
            items.append((pos, size, name, True))
        else:
            items.append((pos, size, name, False))
        pos += size
    return items, ""


def _mp4_child(items: Sequence[Tuple[int, int, bytes, bool]], name: bytes,
               start: int, end: int) -> Optional[Tuple[int, int, bool]]:
    """在给定区间里找第一个同名原子。"""
    for off, size, aname, is_container in items:
        if aname == name and start <= off < end:
            return off, size, is_container
    return None


def _mp4_atom(name: bytes, payload: bytes) -> bytes:
    """渲染一个原子；超过 4GB 的情况这里不做支持（音频文件不该有那么大）。"""
    size = len(payload) + 8
    if size > 0xFFFFFFFF:
        raise ValueError("原子 %s 过大" % name.decode("latin-1"))
    return _to_u32be(size) + name + payload


def _mp4_data_atom(payload: bytes, type_code: int) -> bytes:
    """``data`` 子原子：类型码(4) + 区域码(4) + 内容。"""
    return _mp4_atom(b"data", _to_u32be(type_code) + b"\x00\x00\x00\x00" + payload)


def _mp4_ilst_payload(tags: TrackTags, embed_cover: bool,
                      embed_lyrics: bool) -> Tuple[bytes, List[str]]:
    """拼 ``ilst`` 的子原子内容，同时报告真正写进去的字段名。

    注意每条都是「容器原子包一个 data 原子」：``©nam`` 自己不直接放文本，
    这是 iTunes 元数据的硬性结构，少一层播放器就读不到。
    """
    out = b""
    written: List[str] = []

    def text(atom: bytes, value: str, name: str) -> None:
        nonlocal out
        if value and value.strip():
            out += _mp4_atom(atom, _mp4_data_atom(value.encode("utf-8"), 1))
            written.append(name)

    text(b"\xa9nam", tags.title, "title")
    text(b"\xa9ART", tags.artist, "artist")
    text(b"\xa9alb", tags.album, "album")
    text(b"aART", tags.album_artist, "album_artist")
    if tags.track_number:
        payload = struct.pack(">4H", 0, tags.track_number & 0xFFFF,
                              tags.track_total & 0xFFFF, 0)
        out += _mp4_atom(b"trkn", _mp4_data_atom(payload, 0))
        written.append("track_number")
    if tags.disc_number:
        # disk 只有 3 个 16 位字段（没有结尾的保留位），和 trkn 不一样
        payload = struct.pack(">3H", 0, tags.disc_number & 0xFFFF,
                              tags.disc_total & 0xFFFF)
        out += _mp4_atom(b"disk", _mp4_data_atom(payload, 0))
        written.append("disc_number")

    day = tags.date.strip() or tags.year.strip()
    if day:
        out += _mp4_atom(b"\xa9day", _mp4_data_atom(day.encode("utf-8"), 1))
        written.append("date" if tags.date.strip() else "year")

    text(b"\xa9gen", tags.genre, "genre")
    text(b"\xa9cmt", tags.comment, "comment")
    text(b"cprt", tags.copyright, "copyright")
    if tags.isrc.strip():
        # 用 iTunes 的 ``----`` 自由原子而不是 ©too：©too 是「编码工具」的
        # 语义，塞 ISRC 进去会被 mutagen 之类的读取器当成工具名，读不回来
        out += _mp4_freeform(b"com.apple.iTunes", b"ISRC",
                             tags.isrc.encode("utf-8"))
        written.append("isrc")
    if embed_lyrics:
        text(b"\xa9lyr", tags.lyrics, "lyrics")
    if embed_cover and tags.cover_data:
        mime = _norm_mime(tags.cover_mime, tags.cover_data)
        type_code = _MP4_IMAGE_TYPE.get(mime, 13)
        out += _mp4_atom(b"covr", _mp4_data_atom(tags.cover_data, type_code))
        written.append("cover_data")
    return out, written


def _mp4_freeform(mean: bytes, name: bytes, payload: bytes) -> bytes:
    """``----`` 自由原子：``mean`` + ``name`` + ``data`` 三个子原子。

    mean / name 都是「4 字节 version/flags + 字符串」的 FullBox 结构，
    少那 4 个字节读取器就解析不出键名。
    """
    mean_atom = _mp4_atom(b"mean", b"\x00\x00\x00\x00" + mean)
    name_atom = _mp4_atom(b"name", b"\x00\x00\x00\x00" + name)
    data_atom = _mp4_data_atom(payload, 1)     # 类型 1 = UTF-8 文本
    return _mp4_atom(b"----", mean_atom + name_atom + data_atom)


def _mp4_hdlr() -> bytes:
    """``meta`` 里必需的 hdlr 原子（handler type = mdir）。"""
    payload = (b"\x00\x00\x00\x00"      # version + flags
               b"\x00\x00\x00\x00"      # pre_defined
               b"mdir"                  # handler type
               b"appl"                  # reserved[0]
               b"\x00\x00\x00\x00"      # reserved[1]
               b"\x00\x00\x00\x00"      # reserved[2]
               b"\x00")                 # name（空串 + 结束符）
    return _mp4_atom(b"hdlr", payload)


def _mp4_collect(buf: bytes, start: int, end: int, wanted: Sequence[bytes],
                 out: List[Tuple[int, int, bytes]]) -> None:
    """递归收集 ``wanted`` 原子（用于找 stco / co64）。"""
    items, err = _mp4_scan(buf, start, end)
    if err:
        return
    for off, size, name, is_container in items:
        if name in wanted:
            out.append((off, size, name))
        elif is_container:
            inner = off + 8 + (4 if name == b"meta" else 0)
            _mp4_collect(buf, inner, off + size, wanted, out)


def _mp4_shift_offsets(buf: bytes, moov_off: int, moov_end: int,
                       old_moov_end: int, delta: int) -> bytes:
    """moov 体积变了，``stco`` / ``co64`` 里的样本偏移要跟着平移。

    ``buf`` 已经是新布局，所以扫描位置按新布局算；但表里存的是**旧**布局的
    绝对偏移，因此判断阈值必须用旧的 moov 末尾：旧偏移落在 moov 之后的
    说明它指向 moov 后面的 mdat，整体加 ``delta``；落在 moov 之前的
    （moov 放在文件末尾的常见情形）不受影响，一律不动。
    """
    if not delta:
        return buf
    entries: List[Tuple[int, int, bytes]] = []
    _mp4_collect(buf, moov_off + 8, moov_end, (b"stco", b"co64"), entries)
    if not entries:
        return buf

    data = bytearray(buf)
    for off, size, name in entries:
        if size < 16:
            continue
        count = _u32be(buf, off + 12)
        width = 4 if name == b"stco" else 8
        fmt = ">I" if width == 4 else ">Q"
        for index in range(count):
            pos = off + 16 + index * width
            if pos + width > off + size:
                break                      # 表比声明的短：停手，别越界写
            value = _u32be(buf, pos) if width == 4 else _u64be(buf, pos)
            if value == 0 or value <= old_moov_end:
                continue
            struct.pack_into(fmt, data, pos, value + delta)
    return bytes(data)


def _builtin_mp4(raw: bytes, tags: TrackTags, embed_cover: bool,
                 embed_lyrics: bool) -> Tuple[bytes, List[str], str]:
    """插入/替换 ``moov.udta.meta.ilst``，并修正原子尺寸与 sample 偏移。

    MP4 是「父原子写长度、子原子写内容」的树，改任何一层都要把
    从 ``moov`` 到 ``ilst`` 的每一级长度重算，否则播放器直接判定文件损坏；
    同时 moov 变大或变小会把后面的 ``mdat`` 挪位，``stco`` / ``co64``
    里的绝对偏移必须一起平移。
    """
    total = len(raw)
    top, err = _mp4_scan(raw, 0, total)
    if err:
        return b"", [], "MP4 顶层结构解析失败：%s" % err
    if not top:
        return b"", [], "MP4 里没有任何原子"
    if top[0][2] != b"ftyp":
        return b"", [], "第一个原子不是 ftyp，不是标准 MP4"

    moov = _mp4_child(top, b"moov", 0, total)
    if moov is None:
        return b"", [], "缺少 moov 原子"
    moov_off, moov_size, _ = moov
    moov_inner_start = moov_off + 8
    moov_inner_end = moov_off + moov_size

    moov_children, err = _mp4_scan(raw, moov_inner_start, moov_inner_end)
    if err:
        return b"", [], "moov 内解析失败：%s" % err

    # ilst 内容自己拼；结构上先算出新的 meta / udta / moov 字节
    ilst_payload, written = _mp4_ilst_payload(tags, embed_cover, embed_lyrics)
    if not ilst_payload:
        return b"", [], "没有任何可写入的 MP4 字段"
    new_ilst = _mp4_atom(b"ilst", ilst_payload)

    udta = _mp4_child(moov_children, b"udta", moov_inner_start, moov_inner_end)
    meta = None
    ilst = None
    if udta is not None:
        udta_off, udta_size, _ = udta
        udta_children, err = _mp4_scan(raw, udta_off + 8, udta_off + udta_size)
        if err:
            return b"", [], "udta 内解析失败：%s" % err
        meta = _mp4_child(udta_children, b"meta", udta_off + 8, udta_off + udta_size)
        if meta is not None:
            meta_off, meta_size, _ = meta
            if meta_size < 12:
                return b"", [], "meta 原子过短（%d 字节）" % meta_size
            meta_children, err = _mp4_scan(raw, meta_off + 12, meta_off + meta_size)
            if err:
                return b"", [], "meta 内解析失败：%s" % err
            ilst = _mp4_child(meta_children, b"ilst", meta_off + 12, meta_off + meta_size)

    # ---- 自底向上重建每一层 ----
    # meta 是 FullBox，正文最前面那 4 字节 version/flags 必须原样保留，
    # 丢掉它等于把里面的 hdlr 长度字段顶到原子名位置，整个 meta 立刻崩。
    if meta is not None:
        meta_off, meta_size, _ = meta
        meta_head = raw[meta_off + 8:meta_off + 12]
        if ilst is not None:
            ilst_off, ilst_size, _ = ilst
            meta_payload = (meta_head + raw[meta_off + 12:ilst_off] + new_ilst
                            + raw[ilst_off + ilst_size:meta_off + meta_size])
        else:
            meta_payload = meta_head + raw[meta_off + 12:meta_off + meta_size] + new_ilst
    else:
        meta_payload = b"\x00\x00\x00\x00" + _mp4_hdlr() + new_ilst
    new_meta = _mp4_atom(b"meta", meta_payload)

    if udta is not None:
        udta_off, udta_size, _ = udta
        if meta is not None:
            udta_payload = (raw[udta_off + 8:meta[0]] + new_meta
                            + raw[meta[0] + meta[1]:udta_off + udta_size])
        else:
            udta_payload = raw[udta_off + 8:udta_off + udta_size] + new_meta
        new_udta = _mp4_atom(b"udta", udta_payload)
        moov_payload = (raw[moov_inner_start:udta_off] + new_udta
                        + raw[udta_off + udta_size:moov_inner_end])
    else:
        new_udta = _mp4_atom(b"udta", new_meta)
        moov_payload = raw[moov_inner_start:moov_inner_end] + new_udta
    new_moov = _mp4_atom(b"moov", moov_payload)

    delta = len(new_moov) - moov_size
    new_raw = raw[:moov_off] + new_moov + raw[moov_inner_end:]
    new_raw = _mp4_shift_offsets(new_raw, moov_off, moov_off + len(new_moov),
                                 moov_inner_end, delta)
    return new_raw, written, ""


def _mp4_parse_ilst(buf: bytes, ilst_off: int, ilst_size: int) -> Dict[bytes, List[Tuple[int, bytes]]]:
    """解析 ``ilst``：``{原子名: [(类型码, 载荷字节), ...]}``。"""
    found: Dict[bytes, List[Tuple[int, bytes]]] = {}
    items, err = _mp4_scan(buf, ilst_off + 8, ilst_off + ilst_size)
    if err:
        return found
    for off, size, name, _ in items:
        values: List[Tuple[int, bytes]] = []
        data_items, derr = _mp4_scan(buf, off + 8, off + size)
        if derr:
            continue
        for doff, dsize, dname, _ in data_items:
            if dname != b"data" or dsize < 16:
                continue
            type_code = _u32be(buf, doff + 8)
            values.append((type_code, buf[doff + 16:doff + dsize]))
        if values:
            found[name] = values
    return found


def _mp4_read_freeform(buf: bytes, ilst_off: int, ilst_size: int,
                       wanted_name: bytes) -> str:
    """从 ``----`` 自由原子里取指定 name 的 UTF-8 文本。"""
    items, err = _mp4_scan(buf, ilst_off + 8, ilst_off + ilst_size)
    if err:
        return ""
    for off, size, name, _ in items:
        if name != b"----":
            continue
        children, derr = _mp4_scan(buf, off + 8, off + size)
        if derr:
            continue
        found_name = b""
        value = ""
        for coff, csize, cname, _c in children:
            if cname == b"name" and csize > 12:
                found_name = buf[coff + 12:coff + csize]
            elif cname == b"data" and csize > 16:
                value = buf[coff + 16:coff + csize].decode("utf-8", "replace")
        if found_name == wanted_name and value:
            return value
    return ""


def _builtin_read_mp4(raw: bytes) -> Optional[TrackTags]:
    """回退路径的 MP4 回读，只用标准库解析原子。"""
    total = len(raw)
    top, err = _mp4_scan(raw, 0, total)
    if err:
        return None
    moov = _mp4_child(top, b"moov", 0, total)
    if moov is None:
        return None
    moov_off, moov_size, _ = moov
    moov_children, err = _mp4_scan(raw, moov_off + 8, moov_off + moov_size)
    if err:
        return None
    udta = _mp4_child(moov_children, b"udta", moov_off + 8, moov_off + moov_size)
    if udta is None:
        return None
    udta_off, udta_size, _ = udta
    udta_children, err = _mp4_scan(raw, udta_off + 8, udta_off + udta_size)
    if err:
        return None
    meta = _mp4_child(udta_children, b"meta", udta_off + 8, udta_off + udta_size)
    if meta is None or meta[1] < 12:
        return None
    meta_off, meta_size, _ = meta
    meta_children, err = _mp4_scan(raw, meta_off + 12, meta_off + meta_size)
    if err:
        return None
    ilst = _mp4_child(meta_children, b"ilst", meta_off + 12, meta_off + meta_size)
    if ilst is None:
        return None
    fields = _mp4_parse_ilst(raw, ilst[0], ilst[1])

    def text(name: bytes) -> str:
        for _type, payload in fields.get(name, []):
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError:
                continue
        return ""

    def pair(name: bytes) -> Tuple[int, int]:
        for _type, payload in fields.get(name, []):
            if len(payload) >= 6:
                return _u16be(payload, 2), _u16be(payload, 4)
        return 0, 0

    tags = TrackTags()
    tags.title = text(b"\xa9nam")
    tags.artist = text(b"\xa9ART")
    tags.album = text(b"\xa9alb")
    tags.album_artist = text(b"aART")
    tags.track_number, tags.track_total = pair(b"trkn")
    tags.disc_number, tags.disc_total = pair(b"disk")
    tags.date = text(b"\xa9day")
    tags.year = tags.date[:4]
    tags.genre = text(b"\xa9gen")
    tags.comment = text(b"\xa9cmt")
    tags.copyright = text(b"cprt")
    tags.isrc = _mp4_read_freeform(raw, ilst[0], ilst[1], b"ISRC") or text(b"\xa9too")
    tags.lyrics = text(b"\xa9lyr")
    for type_code, payload in fields.get(b"covr", []):
        tags.cover_data = payload
        tags.cover_mime = _MP4_IMAGE_MIME.get(type_code, "image/jpeg")
        break
    return tags


# --------------------------------------------------------------------------
# 回退后端：读取（校验用）
# --------------------------------------------------------------------------


def _parse_flac_vorbis(payload: bytes) -> Dict[str, str]:
    """解析 FLAC 里的 Vorbis comment 块正文。

    注意长度字段是**小端**（Vorbis comment 规范如此，和 FLAC 其他块的大端
    不一样）；写的时候也是小端，两边必须一致。
    """
    out: Dict[str, str] = {}
    try:
        pos = 0
        vend_len = struct.unpack_from("<I", payload, pos)[0]
        pos += 4 + vend_len
        count = struct.unpack_from("<I", payload, pos)[0]
        pos += 4
        for _ in range(count):
            length = struct.unpack_from("<I", payload, pos)[0]
            pos += 4
            entry = payload[pos:pos + length].decode("utf-8", "replace")
            pos += length
            key, _, value = entry.partition("=")
            out.setdefault(key.upper(), value)
    except (struct.error, IndexError):
        return out
    return out


def _parse_flac_picture(payload: bytes) -> Tuple[bytes, str]:
    """从 PICTURE 块正文里取出 ``(图片数据, MIME)``。"""
    try:
        pos = 4                                   # 跳过图片类型
        mime_len = _u32be(payload, pos)
        pos += 4
        mime = payload[pos:pos + mime_len].decode("ascii", "replace")
        pos += mime_len
        desc_len = _u32be(payload, pos)
        pos += 4 + desc_len
        pos += 16                                 # 宽/高/深/色数
        data_len = _u32be(payload, pos)
        pos += 4
        return payload[pos:pos + data_len], (mime or "image/jpeg")
    except (struct.error, IndexError):
        return b"", "image/jpeg"


def _builtin_read_flac(raw: bytes) -> Optional[TrackTags]:
    """回退路径的 FLAC 回读。"""
    _streaminfo, blocks, _audio, err = _flac_split(raw)
    if err:
        return None
    comments: Dict[str, str] = {}
    cover = b""
    cover_mime = "image/jpeg"
    for code, payload in blocks:
        if code == _FLAC_VORBIS_COMMENT:
            comments = _parse_flac_vorbis(payload)
        elif code == _FLAC_PICTURE and not cover:
            cover, cover_mime = _parse_flac_picture(payload)
    tags = _vorbis_to_tags(comments, "")
    tags.cover_data = cover
    if cover:
        tags.cover_mime = cover_mime
    return tags


def _parse_id3_size(raw: bytes, pos: int, version: int) -> int:
    """按版本解析帧长度：v2.4 是 syncsafe，v2.3 是普通大端。"""
    chunk = raw[pos + 4:pos + 8]
    if len(chunk) != 4:
        return 0
    return _syncsafe(chunk) if version >= 4 else _u32be(chunk)


def _id3_codec(encoding: int) -> str:
    """ID3 文本编码字节 -> Python 编码名（v2.3/v2.4 通用）。"""
    if encoding == 1:
        return "utf-16"       # 带 BOM
    if encoding == 2:
        return "utf-16-be"    # 无 BOM
    if encoding == 3:
        return "utf-8"
    return "latin-1"


def _id3_terminator(codec: str) -> bytes:
    return b"\x00\x00" if codec.startswith("utf-16") else b"\x00"


def _id3_decode(body: bytes, codec: str) -> str:
    """解码一段 ID3 文本，并砍掉编码里可能残留的结束符/垃圾。"""
    if not body:
        return ""
    try:
        text = body.decode(codec, "replace")
    except Exception:
        return ""
    if codec.startswith("utf-16"):
        # UTF-16 正文里不该再有 NUL，出现即表示后面是填充
        text = text.split("\x00")[0]
    return text.rstrip("\x00")


def _id3_read_string(payload: bytes, pos: int, codec: str) -> Tuple[str, int]:
    """读一个「以 NUL 结尾的字符串」，返回 ``(文本, 下一个位置)``。

    UTF-16 的结束符必须按 **2 字节对齐**去找：字符本身就以 ``0x00`` 结尾
    （ASCII 字符在 UTF-16LE 里是 ``44 00``），不做对齐会把上一个字符的
    尾字节当成结束符的一半，解出半个字符加一个替换符。
    """
    term = _id3_terminator(codec)
    if len(term) == 2:
        end = -1
        index = pos
        while index + 2 <= len(payload):
            if payload[index:index + 2] == term:
                end = index
                break
            index += 2
    else:
        end = payload.find(term, pos)
    if end < 0:
        return _id3_decode(payload[pos:], codec), len(payload)
    return _id3_decode(payload[pos:end], codec), end + len(term)


def _decode_id3_text(payload: bytes) -> str:
    """解 ID3 文本帧：第一字节是编码，后面才是正文。"""
    if not payload:
        return ""
    return _id3_decode(payload[1:], _id3_codec(payload[0]))


def _builtin_read_id3(raw: bytes) -> Optional[TrackTags]:
    """回退路径的 ID3 回读（v2.3 / v2.4 都认）。"""
    if len(raw) < 10 or raw[:3] != b"ID3":
        return None
    version = raw[3]
    end = min(10 + _syncsafe(raw[6:10]), len(raw))
    pos = 10
    frames: Dict[str, bytes] = {}
    multi: Dict[str, List[bytes]] = {}      # 同 ID 出现多次的帧（如多个 TXXX）
    while pos + 10 <= end:
        frame_id = raw[pos:pos + 4]
        if not frame_id.strip(b"\x00"):
            break
        size = _parse_id3_size(raw, pos, version)
        if size <= 0 or pos + 10 + size > end:
            break
        name = frame_id.decode("latin-1")
        payload = raw[pos + 10:pos + 10 + size]
        multi.setdefault(name, []).append(payload)
        frames.setdefault(name, payload)
        pos += 10 + size

    tags = TrackTags()
    tags.title = _decode_id3_text(frames.get("TIT2", b""))
    tags.artist = _decode_id3_text(frames.get("TPE1", b""))
    tags.album = _decode_id3_text(frames.get("TALB", b""))
    tags.album_artist = _decode_id3_text(frames.get("TPE2", b""))
    tags.track_number, tags.track_total = _split_pair(
        _decode_id3_text(frames.get("TRCK", b"")))
    tags.disc_number, tags.disc_total = _split_pair(
        _decode_id3_text(frames.get("TPOS", b"")))
    tags.genre = _decode_id3_text(frames.get("TCON", b""))
    tags.copyright = _decode_id3_text(frames.get("TCOP", b""))
    tags.year = _decode_id3_text(frames.get("TYER", b""))
    tags.date = _decode_id3_text(frames.get("TDRC", b"")) or tags.year

    # COMM / USLT 结构一致：编码(1) + 语言(3) + 描述(带结束符) + 正文
    for frame_id, field_name in (("COMM", "comment"), ("USLT", "lyrics")):
        payload = frames.get(frame_id)
        if not payload or len(payload) < 4:
            continue
        codec = _id3_codec(payload[0])
        _desc, pos2 = _id3_read_string(payload, 4, codec)
        setattr(tags, field_name, _id3_decode(payload[pos2:], codec))

    # TXXX 是同一套结构，只是描述用来区分用途；一个文件里可能有好几个
    # （DATE 和 ISRC 就是分开写的），所以必须遍历全部而不是只看第一个
    for payload in multi.get("TXXX", []):
        if len(payload) < 1:
            continue
        codec = _id3_codec(payload[0])
        desc, pos2 = _id3_read_string(payload, 1, codec)
        value = _id3_decode(payload[pos2:], codec)
        if desc.upper() == "ISRC":
            tags.isrc = value
        elif desc.upper() == "DATE" and value:
            tags.date = value

    apic = frames.get("APIC")
    if apic:
        try:
            codec = _id3_codec(apic[0])
            mime, pos2 = _id3_read_string(apic, 1, "latin-1")
            pos2 += 1                              # 跳过图片类型字节
            _desc, pos3 = _id3_read_string(apic, pos2, codec)
            tags.cover_mime = mime or "image/jpeg"
            tags.cover_data = apic[pos3:]
        except IndexError:
            pass
    return tags


def _read_builtin(path: str, container: str) -> Optional[TrackTags]:
    """回退路径回读总入口。"""
    try:
        raw = _read_file(path)
        if container == "mp3":
            return _builtin_read_id3(raw)
        if container == "flac":
            return _builtin_read_flac(raw)
        if container == "mp4":
            return _builtin_read_mp4(raw)
    except Exception:
        return None
    return None


__all__ = [
    "TrackTags",
    "TagWriteResult",
    "mutagen_available",
    "supports_format",
    "lyrics_extension",
    "write_tags",
    "read_tags",
]
