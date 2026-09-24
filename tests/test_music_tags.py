"""音乐标签写入的单元测试：全部用代码现场造夹具，不联网、不下载。

运行::

    ./.venv/bin/python -m pytest tests/test_music_tags.py -v
    # 或
    ./.venv/bin/python tests/test_music_tags.py

覆盖三件事：

1. 三条容器分支（MP3 / FLAC / MP4）在 **mutagen 后端** 和 **纯 Python
   回退后端** 下都能写进去、读得回来；
2. 写标签**绝不能碰音频数据**——MP3 的帧、FLAC 的音频帧、MP4 的 mdat
   都要逐字节比对；
3. 各种烂输入（不存在、目录、.txt、截断/垃圾文件）只报错不抛异常。
"""
from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mediaharvest import tags as tagmod
from mediaharvest.tags import (
    TrackTags,
    lyrics_extension,
    mutagen_available,
    read_tags,
    supports_format,
    write_tags,
)


# --------------------------------------------------------------------------
# 合成夹具：都是「结构上合法但内容极少」的最小文件
# --------------------------------------------------------------------------

#: 测试用假 JPEG：文件头 + 填充 + EOI，足够验证字节进出即可
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"x" * 32 + b"\xff\xd9"

#: 带时间戳的歌词（应落到 .lrc）
LRC_LYRICS = "[ti:测试]\n[00:01.00]第一行\n[00:12.34]第二行\n"

#: 无时间戳的歌词（应落到 .txt）
PLAIN_LYRICS = "第一行歌词\n第二行歌词\n"


def build_mp3() -> bytes:
    """造一个最小的 MPEG-1 Layer III 文件。

    帧头 ``FF FB 90 00``：MPEG-1、Layer III、无 CRC、128kbps、44100Hz，
    帧长 = 144 * 128000 / 44100 = 417 字节。五帧足够让「音频原样保留」
    的断言有说服力。
    """
    header = b"\xff\xfb\x90\x00"
    frame_size = 144 * 128000 // 44100          # 417
    frames = b""
    for index in range(5):
        payload = bytes(((index * 7 + i) & 0xFF) for i in range(frame_size - 4))
        frames += header + payload
    return frames


def _flac_block(code: int, payload: bytes, last: bool) -> bytes:
    flag = 0x80 if last else 0x00
    return bytes((flag | code,)) + struct.pack(">I", len(payload))[1:] + payload


def build_flac() -> bytes:
    """造一个最小的 FLAC：``fLaC`` + 34 字节 STREAMINFO + PADDING + 音频帧。

    STREAMINFO 必须恰好 34 字节，否则 mutagen 直接拒绝加载；采样率
    44100 / 双声道 / 16bit / 44100 个样本，这样 ``info.length == 1.0``。
    """
    sample_rate = 44100
    channels, bits = 2, 16
    total_samples = 44100
    streaminfo = struct.pack(">HH", 4096, 4096)          # min/max blocksize
    streaminfo += b"\x00\x00\x00" + b"\x00\x00\x00"      # min/max framesize
    streaminfo += bytes((
        (sample_rate >> 12) & 0xFF,
        (sample_rate >> 4) & 0xFF,
        ((sample_rate & 0xF) << 4) | ((channels - 1) << 1) | (((bits - 1) >> 4) & 1),
        (((bits - 1) & 0xF) << 4) | ((total_samples >> 32) & 0xF),
    ))
    streaminfo += struct.pack(">I", total_samples & 0xFFFFFFFF)
    streaminfo += b"\x00" * 16                           # MD5 签名
    assert len(streaminfo) == 34, len(streaminfo)

    frames = bytes((i * 13 + 5) & 0xFF for i in range(512))
    return (
        b"fLaC"
        + _flac_block(0, streaminfo, False)
        + _flac_block(1, b"\x00" * 64, True)
        + frames
    )


def _atom(name: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + name + payload


def build_mp4() -> bytes:
    """造一个最小 M4A：``ftyp`` + ``moov(mvhd, trak, udta(meta(hdlr)))`` + ``mdat``。

    刻意把 moov 放在 mdat **前面**（faststart 排布）：这种情况下给 moov
    插入 ilst 会让 moov 变大，``stco`` 里的绝对偏移必须跟着平移，正好
    把这段逻辑也测到。
    """
    ftyp = _atom(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")

    mvhd = _atom(b"mvhd", (
        b"\x00\x00\x00\x00"                     # version + flags
        + struct.pack(">IIII", 0, 0, 1000, 1000)   # 创建/修改时间、时基、时长
        + struct.pack(">I", 0x00010000)         # rate 1.0
        + struct.pack(">H", 0x0100)             # volume 1.0
        + b"\x00" * 10
        + struct.pack(">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
        + b"\x00" * 24
    ))
    mdhd = _atom(b"mdhd", (
        b"\x00\x00\x00\x00"
        + struct.pack(">IIII", 0, 0, 44100, 44100)
        + struct.pack(">HH", 0x55C4, 0)         # language 'und' + quality
    ))
    hdlr_soun = _atom(b"hdlr", (
        b"\x00\x00\x00\x00" + b"\x00" * 4 + b"soun" + b"\x00" * 12 + b"\x00"
    ))
    tkhd = _atom(b"tkhd", (
        b"\x00\x00\x00\x07"                     # version 0 + flags(enabled)
        + struct.pack(">IIIII", 0, 0, 1, 0, 1000)
        + b"\x00" * 8
        + struct.pack(">hhhh", 0, 0, 0, 0)
        + struct.pack(">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
        + struct.pack(">II", 0, 0)              # width / height
    ))
    # mp4a 采样条目：36 字节固定部分 + 一个 esds 子原子。esds 是真实
    # M4A 的标配，且 mutagen 的 AudioSampleEntry 要求固定部分后面**必须**
    # 还能解析出一个原子，否则直接判「truncated data」。
    esds_payload = (
        b"\x00\x00\x00\x00"                     # version + flags
        + b"\x03\x19\x00\x00\x00"               # ES_Descriptor
        + b"\x04\x11\x40\x15\x00\x00\x00"       # DecoderConfigDescriptor
        + b"\x00\x00\x00\x00"                   #   （占位，内容不参与解析）
        + b"\x05\x02\x12\x10"                   # DecoderSpecificInfo
        + b"\x06\x01\x02"                       # SLConfigDescriptor
    )
    esds = _atom(b"esds", esds_payload)
    mp4a = _atom(b"mp4a", (
        b"\x00" * 6 + struct.pack(">H", 1)      # reserved + data_reference_index
        + struct.pack(">HH", 0, 0) + b"\x00" * 4   # version/revision/vendor
        + struct.pack(">HH", 2, 16)             # channels、sample_size
        + struct.pack(">HH", 0, 0)              # pre_defined、reserved
        + struct.pack(">I", 44100 << 16)        # sample_rate 16.16
        + esds
    ))
    mdat_payload = bytes((i * 31 + 7) & 0xFF for i in range(600))

    stsd = _atom(b"stsd", b"\x00\x00\x00\x00" + struct.pack(">I", 1) + mp4a)
    stco = _atom(b"stco", b"\x00\x00\x00\x00" + struct.pack(">II", 1, 0))  # 偏移稍后回填
    stbl = _atom(b"stbl", stsd + stco)
    minf = _atom(b"minf", stbl)
    mdia = _atom(b"mdia", mdhd + hdlr_soun + minf)
    trak = _atom(b"trak", tkhd + mdia)

    hdlr_mdir = _atom(b"hdlr", (
        b"\x00\x00\x00\x00" + b"\x00" * 4 + b"mdir" + b"appl"
        + b"\x00" * 9 + b"\x00"
    ))
    # meta 是 FullBox：子原子前面必须先有 4 字节 version/flags，
    # 少写这 4 字节会让解析器把 hdlr 的长度字段当成原子名
    meta = _atom(b"meta", b"\x00\x00\x00\x00" + hdlr_mdir)
    udta = _atom(b"udta", meta)

    moov = _atom(b"moov", mvhd + trak + udta)
    mdat = _atom(b"mdat", mdat_payload)

    # mdat 内容在文件里的真实偏移 = ftyp + moov + mdat 头
    offset = len(ftyp) + len(moov) + 8
    stco_new = _atom(b"stco", b"\x00\x00\x00\x00" + struct.pack(">II", 1, offset))
    assert len(stco_new) == len(stco)
    moov = moov.replace(stco, stco_new)
    return ftyp + moov + mdat


# --------------------------------------------------------------------------
# 从合成文件里取出「音频数据」用于比对
# --------------------------------------------------------------------------


def audio_part(path: str, ext: str) -> bytes:
    """取出去掉元数据之后的部分，用于「音频没被动过」的断言。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    container = tagmod._container_of(path)
    if container == "mp3":
        return tagmod._strip_id3v2(raw)[0]
    if container == "flac":
        return tagmod._flac_split(raw)[2]
    if container == "mp4":
        for off, size, name, _c in tagmod._mp4_scan(raw, 0, len(raw))[0]:
            if name == b"mdat":
                return raw[off + 8:off + size]
    raise AssertionError("夹具里没找到音频数据: %s" % ext)


def find_atom(path: str, atom_name: bytes):
    """在 MP4 顶层找原子，返回 ``(offset, size)``。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    for off, size, name, _container in tagmod._mp4_scan(raw, 0, len(raw))[0]:
        if name == atom_name:
            return off, size
    raise AssertionError("找不到原子 %r" % atom_name)


def sample_tags() -> TrackTags:
    """一份内容齐全的标签，覆盖所有字段分支。"""
    return TrackTags(
        title="夜曲",
        artist="周杰伦",
        album="十一月的萧邦",
        album_artist="周杰伦",
        track_number=3,
        track_total=12,
        disc_number=1,
        disc_total=2,
        year="2005",
        date="2005-11-01",
        genre="Pop",
        comment="mediaharvest 测试注释",
        lyrics=LRC_LYRICS,
        cover_data=FAKE_JPEG,
        cover_mime="image/jpeg",
        isrc="CNA231234567",
        copyright="(C) 2005 Sony Music",
    )


class TagFixtureCase(unittest.TestCase):
    """公共基类：临时目录 + 三条容器的合成夹具。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mh-tags-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.orig_force = tagmod._BACKEND_FORCE
        self.addCleanup(self._restore_force)
        # 环境变量必须清掉，否则本机设置会串进测试
        self._env_saved = {k: os.environ.pop(k) for k in (tagmod.ENV_BACKEND,)
                           if k in os.environ}
        self.addCleanup(self._restore_env)

    def _restore_force(self):
        tagmod._BACKEND_FORCE = self.orig_force

    def _restore_env(self):
        for key, value in self._env_saved.items():
            os.environ[key] = value

    def make(self, ext: str, name: str = "sample") -> str:
        """按扩展名生成对应的合成夹具文件（``name`` 自带扩展名时不重复拼）。"""
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
        path = os.path.join(self.tmp, name + ext)
        with open(path, "wb") as fh:
            fh.write(build_mp3() if ext == ".mp3" else
                     build_flac() if ext == ".flac" else build_mp4())
        return path


# --------------------------------------------------------------------------
# 基础接口
# --------------------------------------------------------------------------


class TestBasicInterface(TagFixtureCase):
    """纯函数：扩展名判定、歌词后缀、is_empty。"""

    def test_supports_format(self):
        for ext in (".mp3", ".m4a", ".mp4", ".m4b", ".flac", ".ogg", ".oga",
                    ".ogx", ".opus"):
            with self.subTest(ext=ext):
                self.assertTrue(supports_format("x" + ext))
                self.assertTrue(supports_format("x" + ext.upper()))
        for ext in (".txt", ".wav", ".ape", ".json", ""):
            with self.subTest(ext=ext):
                self.assertFalse(supports_format("x" + ext))

    def test_ogx_alias_is_supported(self):
        """``.ogx`` 是 Ogg 的常见别名（archive.org 在用）。

        不认这个扩展名时标签会被静默跳过：元信息明明抓到了却写不进去，
        用户只看到「标签没生效」而查不出原因。
        """
        self.assertTrue(supports_format("song.ogx"))
        self.assertTrue(supports_format("SONG.OGX"))

    def test_weba_is_rejected_not_treated_as_ogg(self):
        """``.weba`` 是 WebM/Matroska，标签结构与 Ogg 不同。

        把它当 Ogg 处理会写坏文件，因此必须明确报「不支持」。
        """
        self.assertFalse(supports_format("song.weba"))

    def test_lyrics_extension(self):
        self.assertEqual(lyrics_extension(LRC_LYRICS), ".lrc")
        self.assertEqual(lyrics_extension("[00:01]一行"), ".lrc")
        self.assertEqual(lyrics_extension("[ar:某人]\n歌词"), ".lrc")
        self.assertEqual(lyrics_extension(PLAIN_LYRICS), ".txt")
        self.assertEqual(lyrics_extension("只有一行"), ".txt")
        self.assertEqual(lyrics_extension(""), ".txt")

    def test_is_empty(self):
        self.assertTrue(TrackTags().is_empty)
        self.assertTrue(TrackTags(cover_mime="image/png").is_empty)
        self.assertTrue(TrackTags(title="   ", comment="\n").is_empty)
        self.assertFalse(TrackTags(title="x").is_empty)
        self.assertFalse(TrackTags(track_number=1).is_empty)
        self.assertFalse(TrackTags(cover_data=b"\x01").is_empty)

    def test_mutagen_available_matches_import(self):
        try:
            import mutagen  # noqa: F401
            expected = True
        except Exception:
            expected = False
        self.assertEqual(mutagen_available(), expected)

    def test_empty_tags_short_circuit(self):
        """空标签不该白写盘：ok=True 但一个字段都没写。"""
        path = self.make(".mp3")
        before = audio_part(path, ".mp3")
        result = write_tags(path, TrackTags())
        self.assertTrue(result.ok)
        self.assertEqual(result.written, [])
        self.assertTrue(result.warnings)
        self.assertEqual(audio_part(path, ".mp3"), before)


# --------------------------------------------------------------------------
# 写 + 读 往返
# --------------------------------------------------------------------------


class TestRoundTripBothBackends(TagFixtureCase):
    """三种容器 × 两条后端：写进去要能原样读回来。"""

    def check_roundtrip(self, ext: str, backend: str):
        path = self.make(ext)
        original_audio = audio_part(path, ext)
        tagmod._BACKEND_FORCE = backend

        tags = sample_tags()
        result = write_tags(path, tags)

        self.assertTrue(result.ok, "写入失败: %s" % result.error)
        self.assertEqual(result.backend, backend)
        self.assertIn("title", result.written)
        self.assertIn("artist", result.written)
        self.assertIn("album", result.written)
        self.assertIn("track_number", result.written)
        self.assertIn("lyrics", result.written)

        back = read_tags(path)
        self.assertIsNotNone(back, "read_tags 返回 None")
        self.assertEqual(back.title, tags.title)
        self.assertEqual(back.artist, tags.artist)
        self.assertEqual(back.album, tags.album)
        self.assertEqual(back.album_artist, tags.album_artist)
        self.assertEqual(back.track_number, 3)
        self.assertEqual(back.track_total, 12)
        self.assertEqual(back.lyrics, tags.lyrics)
        self.assertEqual(back.comment, tags.comment)

        # 音频一个字节都不许变
        self.assertEqual(audio_part(path, ext), original_audio)
        return back

    def test_roundtrip_mutagen(self):
        if not mutagen_available():
            self.skipTest("环境里没有 mutagen")
        for ext in (".mp3", ".flac", ".m4a"):
            with self.subTest(ext=ext, backend="mutagen"):
                self.check_roundtrip(ext, "mutagen")

    def test_roundtrip_builtin(self):
        """回退后端：mp3 / flac / m4a 都要真正写进去（不是 stub）。"""
        for ext in (".mp3", ".flac", ".m4a"):
            with self.subTest(ext=ext, backend="builtin"):
                self.check_roundtrip(ext, "builtin")

    def test_env_var_forces_builtin(self):
        """环境变量方式也必须能强制回退后端。"""
        os.environ[tagmod.ENV_BACKEND] = "builtin"
        path = self.make(".flac")
        result = write_tags(path, TrackTags(title="env", artist="某人"))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.backend, "builtin")
        self.assertEqual(read_tags(path).title, "env")

    def test_env_var_forces_mutagen(self):
        if not mutagen_available():
            self.skipTest("环境里没有 mutagen")
        os.environ[tagmod.ENV_BACKEND] = "mutagen"
        path = self.make(".mp3")
        result = write_tags(path, TrackTags(title="env-mutagen"))
        self.assertEqual(result.backend, "mutagen")
        self.assertTrue(result.ok, result.error)

    def test_forced_mutagen_without_mutagen_reports_error(self):
        """强制 mutagen 但环境里没有：明确报错，而不是悄悄走回退。"""
        with mock.patch.object(tagmod, "_HAS_MUTAGEN", False):
            tagmod._BACKEND_FORCE = "mutagen"
            path = self.make(".mp3")
            result = write_tags(path, TrackTags(title="x"))
        self.assertFalse(result.ok)
        self.assertTrue(result.error)
        self.assertEqual(result.backend, "mutagen")

    def test_builtin_works_with_mutagen_hidden(self):
        """把 mutagen 藏起来，回退路径仍然要能写能读。

        这是「没有三方依赖的精简环境」的模拟：``_HAS_MUTAGEN=False`` 时
        自动选择就应落到 builtin，而且 read_tags 也得跟着降级。
        """
        path = self.make(".mp3")
        flac = self.make(".flac")
        mp4 = self.make(".m4a")
        before = {p: audio_part(p, os.path.splitext(p)[1]) for p in (path, flac, mp4)}

        with mock.patch.object(tagmod, "_HAS_MUTAGEN", False):
            self.assertFalse(tagmod.mutagen_available())
            tagmod._BACKEND_FORCE = None
            for target in (path, flac, mp4):
                with self.subTest(path=os.path.basename(target)):
                    result = write_tags(target, sample_tags())
                    self.assertTrue(result.ok, result.error)
                    self.assertEqual(result.backend, "builtin")
                    back = read_tags(target)
                    self.assertIsNotNone(back)
                    self.assertEqual(back.title, "夜曲")
                    self.assertEqual(back.lyrics, LRC_LYRICS)
                    self.assertEqual(back.track_number, 3)

        for target in (path, flac, mp4):
            self.assertEqual(audio_part(target, os.path.splitext(target)[1]),
                             before[target])

    def test_read_tags_written_by_builtin_using_mutagen(self):
        """回退后端写的文件，必须能被 mutagen 正确解析（互操作性）。"""
        if not mutagen_available():
            self.skipTest("环境里没有 mutagen")
        for ext in (".mp3", ".flac", ".m4a"):
            with self.subTest(ext=ext):
                path = self.make(ext, name="interop" + ext.strip("."))
                tagmod._BACKEND_FORCE = "builtin"
                result = write_tags(path, sample_tags())
                self.assertTrue(result.ok, result.error)
                tagmod._BACKEND_FORCE = None

                # 用 mutagen 自己的解析器复核，确保不是「自写自读的巧合」
                from mutagen import File as MutagenFile

                audio = MutagenFile(path)
                self.assertIsNotNone(audio, "mutagen 打不开回退后端写出的文件")
                if ext == ".mp3":
                    self.assertEqual(str(audio.tags["TIT2"]), "夜曲")
                    self.assertEqual(str(audio.tags["TPE1"]), "周杰伦")
                    self.assertEqual(audio.tags["TRCK"].text[0], "3/12")
                    self.assertEqual(audio.tags["USLT::eng"].text, LRC_LYRICS)
                    self.assertEqual(audio.tags["APIC:"].data, FAKE_JPEG)
                    self.assertEqual(audio.tags["TXXX:ISRC"].text[0], "CNA231234567")
                elif ext == ".flac":
                    self.assertEqual(audio.tags["TITLE"][0], "夜曲")
                    self.assertEqual(audio.tags["TRACKNUMBER"][0], "3")
                    self.assertEqual(audio.tags["LYRICS"][0], LRC_LYRICS)
                    self.assertEqual(audio.pictures[0].data, FAKE_JPEG)
                else:
                    self.assertEqual(audio.tags["\xa9nam"][0], "夜曲")
                    self.assertEqual(audio.tags["trkn"][0], (3, 12))
                    self.assertEqual(audio.tags["\xa9lyr"][0], LRC_LYRICS)
                    self.assertEqual(bytes(audio.tags["covr"][0]), FAKE_JPEG)

    def test_rewrite_is_idempotent(self):
        """连续写两次不该把文件写坏，音频始终不变。"""
        for backend in ("builtin", "mutagen"):
            if backend == "mutagen" and not mutagen_available():
                continue
            for ext in (".mp3", ".flac", ".m4a"):
                with self.subTest(backend=backend, ext=ext):
                    path = self.make(ext, name="twice-%s%s" % (backend, ext))
                    original = audio_part(path, ext)
                    tagmod._BACKEND_FORCE = backend
                    self.assertTrue(write_tags(path, sample_tags()).ok)
                    again = write_tags(path, sample_tags())
                    self.assertTrue(again.ok, again.error)
                    self.assertEqual(audio_part(path, ext), original)
                    self.assertEqual(read_tags(path).title, "夜曲")


# --------------------------------------------------------------------------
# 封面
# --------------------------------------------------------------------------


class TestCoverEmbedding(TagFixtureCase):
    """封面进出：假 JPEG 字节要能原样读回来。"""

    def check_cover(self, ext: str, backend: str):
        path = self.make(ext, name="cover-%s%s" % (backend, ext))
        tagmod._BACKEND_FORCE = backend
        result = write_tags(path, TrackTags(title="封面测试", cover_data=FAKE_JPEG,
                                            cover_mime="image/jpeg"))
        self.assertTrue(result.ok, result.error)
        self.assertIn("cover_data", result.written)
        back = read_tags(path)
        self.assertIsNotNone(back)
        self.assertEqual(back.cover_data, FAKE_JPEG)
        self.assertIn("jpeg", back.cover_mime)

    def test_cover_builtin(self):
        for ext in (".mp3", ".flac", ".m4a"):
            with self.subTest(ext=ext):
                self.check_cover(ext, "builtin")

    def test_cover_mutagen(self):
        if not mutagen_available():
            self.skipTest("环境里没有 mutagen")
        for ext in (".mp3", ".flac", ".m4a"):
            with self.subTest(ext=ext):
                self.check_cover(ext, "mutagen")

    def test_cover_disabled(self):
        """embed_cover=False 时封面不能出现在文件里。"""
        path = self.make(".mp3", name="nocover.mp3")
        tagmod._BACKEND_FORCE = "builtin"
        result = write_tags(path, TrackTags(title="无封面", cover_data=FAKE_JPEG),
                            embed_cover=False)
        self.assertTrue(result.ok, result.error)
        self.assertNotIn("cover_data", result.written)
        back = read_tags(path)
        self.assertEqual(back.cover_data, b"")

    def test_empty_cover_is_skipped(self):
        path = self.make(".flac", name="emptycover.flac")
        tagmod._BACKEND_FORCE = "builtin"
        result = write_tags(path, TrackTags(title="x"))
        self.assertTrue(result.ok, result.error)
        self.assertNotIn("cover_data", result.written)
        self.assertEqual(read_tags(path).cover_data, b"")

    def test_png_cover_type_code(self):
        """PNG 封面在 MP4 里要用 14 号类型码，不能一律按 JPEG 处理。"""
        png = b"\x89PNG\r\n\x1a\n" + b"p" * 32
        path = self.make(".m4a", name="png.m4a")
        tagmod._BACKEND_FORCE = "builtin"
        result = write_tags(path, TrackTags(title="png", cover_data=png,
                                            cover_mime="image/png"))
        self.assertTrue(result.ok, result.error)
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertIn(struct.pack(">I", 14) + b"\x00\x00\x00\x00" + png, raw)
        back = read_tags(path)
        self.assertEqual(back.cover_data, png)
        self.assertEqual(back.cover_mime, "image/png")


# --------------------------------------------------------------------------
# 歌词旁挂文件
# --------------------------------------------------------------------------


class TestLyricsSidecar(TagFixtureCase):
    """write_lyrics_file=True 时落一个同名的 .lrc / .txt。"""

    def test_lrc_sidecar(self):
        path = self.make(".mp3", name="lrc.mp3")
        tagmod._BACKEND_FORCE = "builtin"
        result = write_tags(path, TrackTags(title="lrc", lyrics=LRC_LYRICS),
                            write_lyrics_file=True)
        self.assertTrue(result.ok, result.error)
        self.assertIn("lyrics_file", result.written)
        sidecar = os.path.join(self.tmp, "lrc.lrc")
        self.assertTrue(os.path.isfile(sidecar))
        with open(sidecar, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), LRC_LYRICS)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "lrc.txt")))

    def test_txt_sidecar(self):
        path = self.make(".flac", name="plain.flac")
        tagmod._BACKEND_FORCE = "builtin"
        result = write_tags(path, TrackTags(title="plain", lyrics=PLAIN_LYRICS),
                            write_lyrics_file=True)
        self.assertTrue(result.ok, result.error)
        self.assertIn("lyrics_file", result.written)
        sidecar = os.path.join(self.tmp, "plain.txt")
        self.assertTrue(os.path.isfile(sidecar))
        with open(sidecar, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), PLAIN_LYRICS)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "plain.lrc")))

    def test_no_sidecar_without_flag(self):
        paths = []
        for backend in ("builtin", "mutagen"):
            if backend == "mutagen" and not mutagen_available():
                continue
            path = self.make(".m4a", name="noside-%s.m4a" % backend)
            tagmod._BACKEND_FORCE = backend
            result = write_tags(path, TrackTags(title="x", lyrics=LRC_LYRICS))
            self.assertTrue(result.ok, result.error)
            paths.append(path)
        for path in paths:
            self.assertFalse(os.path.exists(os.path.splitext(path)[0] + ".lrc"))

    def test_empty_lyrics_warns(self):
        path = self.make(".mp3", name="nolyrics.mp3")
        tagmod._BACKEND_FORCE = "builtin"
        result = write_tags(path, TrackTags(title="x"), write_lyrics_file=True)
        self.assertTrue(result.ok, result.error)
        self.assertNotIn("lyrics_file", result.written)
        self.assertTrue(result.warnings)


# --------------------------------------------------------------------------
# 健壮性
# --------------------------------------------------------------------------


class TestRobustness(TagFixtureCase):
    """烂输入只报错，绝不抛异常。"""

    def assert_fails_cleanly(self, path: str, **kwargs):
        try:
            result = write_tags(path, kwargs.pop("tags", None) or sample_tags(),
                                **kwargs)
        except Exception as exc:  # pragma: no cover - 出现即测试失败
            self.fail("write_tags 抛异常了: %r" % exc)
        self.assertFalse(result.ok, "本该失败却成功了: %s" % path)
        self.assertTrue(result.error, "失败但没有 error 信息: %s" % path)
        return result

    def test_missing_file(self):
        self.assert_fails_cleanly(os.path.join(self.tmp, "nope.mp3"))

    def test_directory(self):
        target = os.path.join(self.tmp, "adir.mp3")
        os.makedirs(target)
        result = self.assert_fails_cleanly(target)
        self.assertIn("目录", result.error)

    def test_plain_text_file(self):
        target = os.path.join(self.tmp, "notes.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("hello")
        result = self.assert_fails_cleanly(target)
        self.assertIn("扩展名", result.error)

    def test_truncated_or_garbage_files(self):
        cases = {
            "garbage.mp3": b"\x00\x01\x02\x03 not audio at all",
            "garbage.flac": b"fLaC" + b"\xff\xff\xff\xff" * 4,
            "garbage.m4a": b"\x00\x00\x00\x08mdat" + b"\x11" * 32,
            "empty.flac": b"",
            "short.mp3": b"\xff",
            "flac_bad_streaminfo.flac": b"fLaC" + _flac_block(0, b"\x00" * 10, True),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                target = os.path.join(self.tmp, name)
                with open(target, "wb") as fh:
                    fh.write(payload)
                self.assert_fails_cleanly(target)
                # 失败时文件必须原样不动
                with open(target, "rb") as fh:
                    self.assertEqual(fh.read(), payload)

    def test_builtin_and_mutagen_both_reject_garbage(self):
        for backend in ("builtin", "mutagen"):
            if backend == "mutagen" and not mutagen_available():
                continue
            tagmod._BACKEND_FORCE = backend
            target = os.path.join(self.tmp, "junk-%s.mp3" % backend)
            with open(target, "wb") as fh:
                fh.write(b"totally not an mp3")
            with self.subTest(backend=backend):
                self.assert_fails_cleanly(target)

    def test_truncated_mp4_does_not_corrupt(self):
        """moov 声明的长度超过文件实际长度时必须拒绝，而不是写出坏文件。"""
        path = self.make(".m4a", name="trunc.m4a")
        with open(path, "rb") as fh:
            raw = fh.read()
        broken = raw[:len(raw) // 2]
        target = os.path.join(self.tmp, "trunc-broken.m4a")
        with open(target, "wb") as fh:
            fh.write(broken)
        tagmod._BACKEND_FORCE = "builtin"
        self.assert_fails_cleanly(target)
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), broken)

    def test_builtin_ogg_needs_mutagen(self):
        """Ogg 回退路径必须明确说「需要 mutagen」，而不是写坏文件。"""
        target = os.path.join(self.tmp, "fake.opus")
        payload = b"OggS" + b"\x00" * 128
        with open(target, "wb") as fh:
            fh.write(payload)
        tagmod._BACKEND_FORCE = "builtin"
        result = self.assert_fails_cleanly(target)
        self.assertIn("mutagen", result.error)
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), payload)

    def test_empty_path_and_bad_tags(self):
        self.assert_fails_cleanly("")
        path = self.make(".mp3", name="badtypes.mp3")
        self.assert_fails_cleanly(path, tags=object())

    def test_read_tags_on_rubbish_returns_none(self):
        target = os.path.join(self.tmp, "junk.flac")
        with open(target, "wb") as fh:
            fh.write(b"fLaC" + b"\x00" * 3)
        self.assertIsNone(read_tags(target))
        self.assertIsNone(read_tags(os.path.join(self.tmp, "missing.mp3")))
        self.assertIsNone(read_tags(os.path.join(self.tmp, "notes")))
        self.assertIsNone(read_tags(""))

    def test_fuzz_never_raises_and_never_makes_things_worse(self):
        """截断 / 随机翻转字节：两条后端都不得抛异常，也不得越写越坏。

        「越写越坏」的判据刻意宽松——变异过的文件本来就可能已经坏了
        （比如把 esds 的编码描述符改烂，mutagen 自己都读不了），
        所以只要求：**写之前能读的，写之后也必须能读**。
        """
        import random

        rng = random.Random(20240501)
        cases = []
        for ext, builder in ((".mp3", build_mp3), (".flac", build_flac),
                             (".m4a", build_mp4)):
            raw = builder()
            for frac in (0.03, 0.5, 0.93):
                cases.append(("%s-trunc%.2f" % (ext, frac), ext,
                              raw[:int(len(raw) * frac)]))
            for index in range(3):
                mutated = bytearray(raw)
                for _ in range(4):
                    mutated[rng.randrange(len(mutated))] = rng.randrange(256)
                cases.append(("%s-mut%d" % (ext, index), ext, bytes(mutated)))

        for backend in ("builtin", "mutagen"):
            if backend == "mutagen" and not mutagen_available():
                continue
            tagmod._BACKEND_FORCE = backend
            for label, ext, payload in cases:
                with self.subTest(backend=backend, case=label):
                    path = os.path.join(self.tmp, "fuzz" + label)
                    with open(path, "wb") as fh:
                        fh.write(payload)
                    readable_before = read_tags(path) is not None
                    try:
                        result = write_tags(path, sample_tags())
                    except Exception as exc:      # pragma: no cover
                        self.fail("write_tags 抛异常: %r" % exc)
                    if not result.ok:
                        self.assertTrue(result.error,
                                        "失败但 error 为空: %s" % label)
                        continue
                    if readable_before:
                        self.assertIsNotNone(
                            read_tags(path),
                            "写之前能读、写之后读不了: %s" % label)


# --------------------------------------------------------------------------
# 结构级断言：原子 / 元数据块 / 帧的细节
# --------------------------------------------------------------------------


class TestAllFieldsRoundTrip(TagFixtureCase):
    """全字段往返：16 个字段一个都不能在读写途中丢失或被改写。

    这组用例是回归网：开发过程中真的踩到过 TXXX 的 UTF-16 结束符没按
    2 字节对齐、多个 TXXX 只读了第一个、MP4 自由原子缺 mean/name、
    meta 的 FullBox 头被重建时丢掉等 bug。
    """

    FIELDS = ("title", "artist", "album", "album_artist", "track_number",
              "track_total", "disc_number", "disc_total", "year", "date",
              "genre", "comment", "lyrics", "isrc", "copyright", "cover_data")

    def test_all_fields_both_backends(self):
        for backend in ("mutagen", "builtin"):
            if backend == "mutagen" and not mutagen_available():
                continue
            for ext in (".mp3", ".flac", ".m4a"):
                with self.subTest(backend=backend, ext=ext):
                    path = self.make(ext, name="all-%s%s" % (backend, ext))
                    tagmod._BACKEND_FORCE = backend
                    tags = sample_tags()
                    result = write_tags(path, tags)
                    self.assertTrue(result.ok, result.error)
                    back = read_tags(path)
                    self.assertIsNotNone(back)
                    for name in self.FIELDS:
                        self.assertEqual(getattr(back, name), getattr(tags, name),
                                         "字段 %s 不一致" % name)
                    self.assertNotIn("date", result.warnings)

    def test_isrc_uses_freeform_atom_in_mp4(self):
        """MP4 的 ISRC 必须走 ``----`` 自由原子，不能借用 ©too。"""
        path = self.make(".m4a", name="isrc.m4a")
        tagmod._BACKEND_FORCE = "builtin"
        self.assertTrue(write_tags(path, TrackTags(title="x",
                                                   isrc="CNA231234567")).ok)
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertIn(b"----", raw)
        self.assertIn(b"com.apple.iTunes", raw)
        self.assertIn(b"ISRC", raw)
        self.assertEqual(read_tags(path).isrc, "CNA231234567")
        if mutagen_available():
            from mutagen.mp4 import MP4

            free = MP4(path).tags.get("----:com.apple.iTunes:ISRC")
            self.assertIsNotNone(free, "mutagen 读不到自由原子")
            self.assertEqual(bytes(free[0]).decode("utf-8"), "CNA231234567")

    def test_multiple_txxx_frames_survive(self):
        """DATE 与 ISRC 是两个独立 TXXX，回读时不能只看第一个。"""
        path = self.make(".mp3", name="txxx.mp3")
        tagmod._BACKEND_FORCE = "builtin"
        result = write_tags(path, TrackTags(title="x", year="2024",
                                            date="2024-05-01",
                                            isrc="CNA231234567"))
        self.assertTrue(result.ok, result.error)
        back = read_tags(path)
        self.assertEqual(back.date, "2024-05-01")
        self.assertEqual(back.isrc, "CNA231234567")

    def test_utf16_descriptions_are_not_truncated(self):
        """TXXX 描述以 UTF-16 存储，结束符必须按 2 字节对齐查找。

        对齐错了会解出 ``DAT\\ufffd`` 这种半截描述，ISRC 就永远匹配不上。
        """
        path = self.make(".mp3", name="align.mp3")
        tagmod._BACKEND_FORCE = "builtin"
        self.assertTrue(write_tags(path, TrackTags(title="x",
                                                   isrc="ABC123")).ok)
        with open(path, "rb") as fh:
            raw = fh.read()
        end = 10 + tagmod._syncsafe(raw[6:10])
        pos, descs = 10, []
        while pos + 10 <= end:
            frame_id = raw[pos:pos + 4]
            if not frame_id.strip(b"\x00"):
                break
            size = tagmod._u32be(raw, pos + 4)
            if frame_id == b"TXXX":
                payload = raw[pos + 10:pos + 10 + size]
                codec = tagmod._id3_codec(payload[0])
                desc, _next = tagmod._id3_read_string(payload, 1, codec)
                descs.append(desc)
            pos += 10 + size
        self.assertEqual(descs, ["ISRC"], "TXXX 描述被解坏了: %r" % descs)

    def test_mp4_meta_fullbox_head_preserved(self):
        """重建 meta 时必须保留它开头那 4 字节 FullBox version/flags。"""
        path = self.make(".m4a", name="fullbox.m4a")
        tagmod._BACKEND_FORCE = "builtin"
        self.assertTrue(write_tags(path, TrackTags(title="x")).ok)
        with open(path, "rb") as fh:
            raw = fh.read()
        top, _err = tagmod._mp4_scan(raw, 0, len(raw))
        moov = tagmod._mp4_child(top, b"moov", 0, len(raw))
        children, _e = tagmod._mp4_scan(raw, moov[0] + 8, moov[0] + moov[1])
        udta = tagmod._mp4_child(children, b"udta", moov[0] + 8, moov[0] + moov[1])
        grand, _e2 = tagmod._mp4_scan(raw, udta[0] + 8, udta[0] + udta[1])
        meta = tagmod._mp4_child(grand, b"meta", udta[0] + 8, udta[0] + udta[1])
        self.assertEqual(raw[meta[0] + 8:meta[0] + 12], b"\x00\x00\x00\x00")
        inner, err = tagmod._mp4_scan(raw, meta[0] + 12, meta[0] + meta[1])
        self.assertEqual(err, "", "meta 内部结构解析失败: %s" % err)
        self.assertIn(b"hdlr", {i[2] for i in inner})


class TestStructure(TagFixtureCase):
    """确认没有把容器结构写坏——这些是播放器最在意的部分。"""

    def test_mp3_has_syncsafe_tag_size(self):
        """ID3v2 标签总长度字段必须是 syncsafe，且与帧总长对得上。"""
        path = self.make(".mp3", name="size.mp3")
        tagmod._BACKEND_FORCE = "builtin"
        self.assertTrue(write_tags(path, sample_tags()).ok)
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertEqual(raw[:3], b"ID3")
        self.assertEqual(raw[3], 3)                    # v2.3
        size = tagmod._syncsafe(raw[6:10])
        self.assertEqual(10 + size, len(raw) - len(build_mp3()))

    def test_mp3_replaces_old_tag_instead_of_stacking(self):
        """重复写入不应该堆出多个 ID3 标签。"""
        path = self.make(".mp3", name="stack.mp3")
        tagmod._BACKEND_FORCE = "builtin"
        for index in range(3):
            self.assertTrue(write_tags(path, TrackTags(title="第%d次" % index)).ok)
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertEqual(raw.count(b"ID3\x03\x00"), 1)
        self.assertEqual(read_tags(path).title, "第2次")

    def test_flac_streaminfo_untouched_and_first(self):
        """STREAMINFO 必须逐字节不变且仍是第一个块。"""
        path = self.make(".flac", name="si.flac")
        original = tagmod._flac_split(build_flac())
        tagmod._BACKEND_FORCE = "builtin"
        self.assertTrue(write_tags(path, sample_tags()).ok)
        with open(path, "rb") as fh:
            raw = fh.read()
        after = tagmod._flac_split(raw)
        self.assertEqual(after[0], original[0])
        self.assertEqual(raw[:4], b"fLaC")
        self.assertEqual(raw[4] & 0x7F, 0)             # 第一个块是 STREAMINFO
        self.assertFalse(raw[4] & 0x80)                # 它不可能是最后一块

    def test_flac_last_block_flag(self):
        """元数据链的「最后一块」标志只能有一个，且必须在末尾。"""
        path = self.make(".flac", name="last.flac")
        tagmod._BACKEND_FORCE = "builtin"
        self.assertTrue(write_tags(path, sample_tags()).ok)
        with open(path, "rb") as fh:
            raw = fh.read()
        pos, lasts = 4, []
        while pos < len(raw):
            head = raw[pos]
            lasts.append(bool(head & 0x80))
            size = tagmod._u24be(raw, pos + 1)
            pos += 4 + size
            if lasts[-1]:
                break
        self.assertEqual(lasts, [False] * (len(lasts) - 1) + [True])

    def test_flac_picture_block_replaced_not_stacked(self):
        """旧的 PICTURE 块要被换掉，不能越写越多。"""
        path = self.make(".flac", name="pic.flac")
        tagmod._BACKEND_FORCE = "builtin"
        for _ in range(3):
            self.assertTrue(write_tags(path, TrackTags(title="x",
                                                       cover_data=FAKE_JPEG)).ok)
        with open(path, "rb") as fh:
            raw = fh.read()
        pos, pictures = 4, 0
        while True:
            head = raw[pos]
            last, code, size = bool(head & 0x80), head & 0x7F, tagmod._u24be(raw, pos + 1)
            pictures += 1 if code == 6 else 0
            pos += 4 + size
            if last:
                break
        self.assertEqual(pictures, 1)

    def test_mp4_parent_sizes_are_consistent(self):
        """moov / udta / meta / ilst 的声明长度必须与真实字节数吻合。"""
        path = self.make(".m4a", name="sizes.m4a")
        tagmod._BACKEND_FORCE = "builtin"
        self.assertTrue(write_tags(path, sample_tags()).ok)
        with open(path, "rb") as fh:
            raw = fh.read()

        def descend(start: int, end: int, chain):
            items, err = tagmod._mp4_scan(raw, start, end)
            self.assertEqual(err, "", err)
            if not chain:
                return items
            target = chain[0]
            found = [i for i in items if i[2] == target]
            self.assertEqual(len(found), 1, "找不到原子 %r" % target)
            off, size, _n, _c = found[0]
            return descend(off + 8 + (4 if target == b"meta" else 0), off + size,
                           chain[1:])

        ilst_items = descend(0, len(raw), [b"moov", b"udta", b"meta", b"ilst"])
        self.assertTrue(ilst_items)
        names = {item[2] for item in ilst_items}
        for expected in (b"\xa9nam", b"\xa9ART", b"\xa9alb", b"aART", b"trkn",
                         b"disk", b"\xa9day", b"\xa9gen", b"\xa9cmt", b"\xa9lyr",
                         b"covr"):
            self.assertIn(expected, names)

    def test_mp4_trailing_mdat_offsets_shifted(self):
        """moov 长大之后，stco 里的偏移要加上同样的增量。"""
        path = self.make(".m4a", name="shift.m4a")
        with open(path, "rb") as fh:
            before = fh.read()
        before_mdat = find_atom(path, b"mdat")
        before_offset = _stco_offset(before)
        # stco 存的是样本数据的绝对偏移，即 mdat 正文起点（跳过 8 字节原子头）
        self.assertEqual(before_mdat[0] + 8, before_offset,
                         "夹具自身的 stco 应指向 mdat 正文起点")

        tagmod._BACKEND_FORCE = "builtin"
        self.assertTrue(write_tags(path, sample_tags()).ok)
        with open(path, "rb") as fh:
            after = fh.read()
        after_mdat = find_atom(path, b"mdat")
        self.assertEqual(_stco_offset(after), after_mdat[0] + 8,
                         "stco 偏移没有跟上 mdat 的新位置")
        self.assertGreater(after_mdat[0], before_mdat[0])
        self.assertEqual(after[after_mdat[0] + 8:after_mdat[0] + after_mdat[1]],
                         before[before_mdat[0] + 8:before_mdat[0] + before_mdat[1]])

    def test_mp4_moov_after_mdat_untouched_offsets(self):
        """moov 在文件尾部时，插入标签不该动 stco（偏移指向 moov 之前）。"""
        path = os.path.join(self.tmp, "tail.m4a")
        with open(path, "wb") as fh:
            fh.write(_mp4_with_trailing_moov())
        before = _stco_offset(open(path, "rb").read())
        tagmod._BACKEND_FORCE = "builtin"
        result = write_tags(path, TrackTags(title="尾部 moov"))
        self.assertTrue(result.ok, result.error)
        with open(path, "rb") as fh:
            after_raw = fh.read()
        self.assertEqual(_stco_offset(after_raw), before)
        self.assertEqual(read_tags(path).title, "尾部 moov")


def _stco_offset(raw: bytes) -> int:
    """取出夹具里第一个 stco 表的第一个 chunk 偏移。"""
    top, err = tagmod._mp4_scan(raw, 0, len(raw))
    if err:
        raise AssertionError("顶层解析失败: %s" % err)
    moov = tagmod._mp4_child(top, b"moov", 0, len(raw))
    if moov is None:
        raise AssertionError("找不到 moov")
    found: list = []
    tagmod._mp4_collect(raw, moov[0] + 8, moov[0] + moov[1], (b"stco",), found)
    if not found:
        raise AssertionError("找不到 stco")
    off = found[0][0]
    return tagmod._u32be(raw, off + 16)


def _mp4_with_trailing_moov() -> bytes:
    """moov 放在 mdat 之后的变体，用来验证「偏移不用平移」的分支。"""
    raw = build_mp4()
    ftyp_size = tagmod._u32be(raw, 0)
    moov = tagmod._mp4_child(tagmod._mp4_scan(raw, 0, len(raw))[0], b"moov",
                             0, len(raw))
    moov_bytes = raw[moov[0]:moov[0] + moov[1]]
    mdat = raw[ftyp_size:moov[0]]
    # mdat 挪到前面来，stco 的偏移要改成新的 mdat 内容位置
    new_offset = len(raw[:ftyp_size]) + 8
    fixed_stco = struct.pack(">II", 1, new_offset)
    old_stco = struct.pack(">II", 1, ftyp_size + moov[1] + 8)
    return raw[:ftyp_size] + mdat + moov_bytes.replace(old_stco, fixed_stco)


if __name__ == "__main__":
    unittest.main(verbosity=2)
