"""音乐抓取相关单元测试：平台识别、目标分类、音质选择、元信息与歌词。

全部为纯逻辑测试，不访问网络。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mediaharvest import music
from mediaharvest.models import MediaItem, MediaType, MusicMeta, Source, TrackKind
from mediaharvest.utils import classify


class TestPlatformDetection(unittest.TestCase):
    """平台识别。"""

    def test_known_platforms(self):
        cases = [
            ("https://music.163.com/song?id=1", "netease"),
            ("https://music.163.com/#/song?id=1", "netease"),
            ("https://y.qq.com/n/ryqq/songDetail/abc", "qqmusic"),
            ("https://soundcloud.com/a/b", "soundcloud"),
            ("https://artist.bandcamp.com/album/x", "bandcamp"),
            ("https://www.mixcloud.com/a/b", "mixcloud"),
            ("https://music.youtube.com/watch?v=x", "youtube_music"),
            ("https://archive.org/details/x", "archive_audio"),
        ]
        for url, key in cases:
            with self.subTest(url=url):
                platform = music.platform_for(url)
                self.assertIsNotNone(platform, url)
                self.assertEqual(platform.key, key)

    def test_non_music_sites(self):
        for url in ("https://example.com/gallery", "https://github.com/a/b",
                    "https://www.bilibili.com/video/BV1xx"):
            with self.subTest(url=url):
                self.assertIsNone(music.platform_for(url))
                self.assertFalse(music.is_music_url(url))

    def test_bilibili_audio_path_constraint(self):
        """B 站只有 /audio/auXXXX 算音乐，主站视频不算。"""
        self.assertIsNotNone(music.platform_for("https://www.bilibili.com/audio/au3134075"))
        self.assertIsNone(music.platform_for("https://www.bilibili.com/video/BV1Qk4C62EEn"))

    def test_supported_flag_is_honest(self):
        """DRM 平台必须标成不可用，不能假装能抓。"""
        for url in ("https://open.spotify.com/track/x",
                    "https://music.apple.com/album/x",
                    "https://y.qq.com/n/ryqq/songDetail/x"):
            with self.subTest(url=url):
                platform = music.platform_for(url)
                self.assertIsNotNone(platform)
                self.assertFalse(platform.supported)
                self.assertTrue(platform.note, "不可用平台必须给出原因")
        self.assertTrue(music.is_supported_music_url("https://music.163.com/song?id=1"))


class TestClassifyMusicUrl(unittest.TestCase):
    """目标粒度分类。"""

    def test_kinds(self):
        cases = [
            ("https://music.163.com/song?id=347230", TrackKind.SONG),
            ("https://music.163.com/#/song?id=347230", TrackKind.SONG),
            ("https://music.163.com/album?id=32311", TrackKind.ALBUM),
            ("https://music.163.com/#/playlist?id=3778678", TrackKind.PLAYLIST),
            ("https://music.163.com/artist?id=10557", TrackKind.ARTIST),
            ("https://music.163.com/discover/toplist?id=1", TrackKind.PLAYLIST),
            ("https://www.bilibili.com/audio/au3134075", TrackKind.SONG),
            ("https://artist.bandcamp.com/album/x", TrackKind.ALBUM),
            ("https://soundcloud.com/a/sets/b", TrackKind.PLAYLIST),
        ]
        for url, kind in cases:
            with self.subTest(url=url):
                self.assertEqual(music.classify_music_url(url).kind, kind)

    def test_collection_flag(self):
        self.assertTrue(music.classify_music_url("https://music.163.com/album?id=1").is_collection)
        self.assertTrue(music.classify_music_url("https://music.163.com/artist?id=1").is_collection)
        self.assertFalse(music.classify_music_url("https://music.163.com/song?id=1").is_collection)

    def test_id_extraction(self):
        t = music.classify_music_url("https://music.163.com/song?id=347230")
        self.assertEqual(t.track_id, "347230")
        # 通用 id 参数要落进正确的槽位，否则歌词接口会取错 id
        t = music.classify_music_url("https://music.163.com/album?id=32311")
        self.assertEqual(t.album_id, "32311")
        self.assertEqual(t.track_id, "")
        t = music.classify_music_url("https://music.163.com/#/playlist?id=3778678")
        self.assertEqual(t.playlist_id, "3778678")
        t = music.classify_music_url("https://www.bilibili.com/audio/au3134075")
        self.assertEqual(t.track_id, "au3134075")

    def test_id_from_path(self):
        t = music.classify_music_url("https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV")
        self.assertEqual(t.track_id, "0039MnYb0qxYhV")
        t = music.classify_music_url("https://y.qq.com/n/ryqq/albumDetail/002fRO0N4FftzY")
        self.assertEqual(t.album_id, "002fRO0N4FftzY")


class TestLosslessDetection(unittest.TestCase):
    """无损判定。"""

    def test_lossless(self):
        for fmt in ({"ext": "flac", "acodec": "flac"},
                    {"ext": "m4a", "acodec": "alac"},
                    {"ext": "wav", "acodec": "pcm_s16le"},
                    {"ext": "flac", "acodec": None}):
            with self.subTest(fmt=fmt):
                self.assertTrue(music.is_lossless(fmt))

    def test_lossy(self):
        for fmt in ({"ext": "mp3", "acodec": "mp3"},
                    {"ext": "m4a", "acodec": "mp4a.40.2"},
                    {"ext": "opus", "acodec": "opus"},
                    {"ext": "ogg", "acodec": "vorbis"}):
            with self.subTest(fmt=fmt):
                self.assertFalse(music.is_lossless(fmt))

    def test_ext_fallback_only_when_codec_unknown(self):
        """acodec 已知是 aac 时，即使扩展名是 flac 也不能判成无损。"""
        self.assertFalse(music.is_lossless({"ext": "flac", "acodec": "aac"}))

    def test_format_ext_normalises(self):
        self.assertEqual(music.format_ext({"ext": "m4a", "acodec": "mp4a.40.2"}), "m4a")
        self.assertEqual(music.format_ext({"ext": "", "acodec": "flac"}), "flac")
        self.assertEqual(music.format_ext({"ext": "weird", "acodec": "opus"}), "opus")

    def test_bitrate_fallbacks(self):
        self.assertEqual(music.format_bitrate({"abr": 320}), 320)
        self.assertEqual(music.format_bitrate({"tbr": 192.7}), 192)
        self.assertEqual(music.format_bitrate({"audio_bitrate": 128}), 128)
        self.assertEqual(music.format_bitrate({}), 0)


class TestQualitySelection(unittest.TestCase):
    """音质档位选择。"""

    def setUp(self):
        self.formats = [
            {"url": "mp3-128", "acodec": "mp3", "ext": "mp3", "abr": 128},
            {"url": "mp3-320", "acodec": "mp3", "ext": "mp3", "abr": 320},
            {"url": "flac", "acodec": "flac", "ext": "flac", "abr": 1004},
            {"url": "m4a-256", "acodec": "mp4a.40.2", "ext": "m4a", "abr": 256},
            {"url": "mv", "acodec": "mp3", "ext": "mp4", "abr": 320, "vcodec": "h264"},
        ]

    def test_best_prefers_lossless(self):
        best = music.pick_audio_format(self.formats, "best")
        self.assertEqual(best["url"], "flac")

    def test_lossless_only_rejects_lossy_platform(self):
        """只有有损源时，lossless 档必须给 None 而不是悄悄降级。"""
        lossy_only = [f for f in self.formats if not music.is_lossless(f)]
        self.assertIsNone(music.pick_audio_format(lossy_only, "lossless"))
        # 而 best 档应该仍然能选出最高的有损
        self.assertEqual(music.pick_audio_format(lossy_only, "best")["url"], "mp3-320")

    def test_high_prefers_m4a_over_higher_bitrate_mp3(self):
        """高音质档按编码偏好选：m4a 优先于码率更高的 mp3。

        同等感知质量下 m4a 体积更小，这正是编码偏好表存在的意义。
        """
        self.assertEqual(music.pick_audio_format(self.formats, "high")["url"], "m4a-256")

    def test_mp3_used_when_no_m4a_available(self):
        mp3_only = [f for f in self.formats if f["ext"] == "mp3"]
        picked = music.pick_audio_format(mp3_only, "high")
        self.assertEqual(picked["url"], "mp3-320", "没有 m4a 时应取最高码率 mp3")

    def test_low_targets_bitrate(self):
        """省流档应贴近目标码率，而不是无脑取最高。"""
        self.assertEqual(music.pick_audio_format(self.formats, "low")["url"], "mp3-128")

    def test_medium_targets_bitrate(self):
        picked = music.pick_audio_format(self.formats, "medium")
        self.assertEqual(picked["url"], "m4a-256")

    def test_video_streams_excluded(self):
        """带视频轨的格式不能被当成音频，否则音乐会下成 MV。"""
        ranked = music.sort_audio_formats(self.formats, music.get_quality("best"))
        self.assertNotIn("mv", [f["url"] for f in ranked])

    def test_unknown_quality_falls_back(self):
        self.assertEqual(music.get_quality("nonsense").key, "best")

    def test_selector_strings(self):
        self.assertIn("flac", music.build_format_selector("lossless"))
        self.assertIn("bestaudio", music.build_format_selector("best"))


class TestMetaFromInfo(unittest.TestCase):
    """yt-dlp info → MusicMeta。"""

    def test_basic_mapping(self):
        info = {
            "title": "海阔天空", "artist": "Beyond", "album": "乐与怒",
            "track_number": 3, "release_date": "19930901",
            "genre": "摇滚", "thumbnail": "https://x/c.jpg", "duration": 320,
            "id": "347230",
        }
        meta = music.meta_from_info(info, platform=music.platform_for("https://music.163.com/"))
        self.assertEqual(meta.title, "海阔天空")
        self.assertEqual(meta.artist, "Beyond")
        self.assertEqual(meta.album, "乐与怒")
        self.assertEqual(meta.track_number, 3)
        self.assertEqual(meta.date, "1993-09-01")
        self.assertEqual(meta.year, "1993")
        self.assertEqual(meta.cover_url, "https://x/c.jpg")
        self.assertTrue(meta.has_tags)

    def test_artist_list_is_flattened(self):
        """部分提取器给的是歌手列表。"""
        info = {"title": "x", "artists": ["A", "B"]}
        self.assertEqual(music.meta_from_info(info).artist, "A / B")

    def test_uploader_fallback(self):
        """SoundCloud 这类站点没有 artist，只有 uploader。"""
        info = {"title": "x", "uploader": "SomeUser"}
        meta = music.meta_from_info(info)
        self.assertEqual(meta.artist, "SomeUser")
        # 专辑艺人为空时应回填歌手，否则音乐库聚合不了专辑
        self.assertEqual(meta.album_artist, "SomeUser")

    def test_track_number_from_playlist_index(self):
        info = {"title": "x", "playlist_index": 7}
        self.assertEqual(music.meta_from_info(info).track_number, 7)

    def test_track_prefix_stripped_from_title(self):
        info = {"title": "01. 海阔天空", "artist": "Beyond"}
        meta = music.meta_from_info(info)
        self.assertEqual(meta.title, "海阔天空")
        self.assertEqual(meta.track_number, 1)

    def test_upload_date_converted(self):
        info = {"title": "x", "upload_date": "20240501"}
        self.assertEqual(music.meta_from_info(info).date, "2024-05-01")

    def test_thumbnail_from_list(self):
        info = {"title": "x", "thumbnails": [{"url": "small"}, {"url": "big"}]}
        self.assertEqual(music.meta_from_info(info).cover_url, "big")

    def test_empty_info_is_safe(self):
        meta = music.meta_from_info({})
        self.assertFalse(meta.has_tags)
        self.assertEqual(meta.display, "")


class TestTrackPrefix(unittest.TestCase):
    """曲目号前缀解析。"""

    def test_strips_common_prefixes(self):
        self.assertEqual(music.split_track_prefix("01. 歌名"), (1, "歌名"))
        self.assertEqual(music.split_track_prefix("3 - Yesterday"), (3, "Yesterday"))
        self.assertEqual(music.split_track_prefix("12) 歌名"), (12, "歌名"))

    def test_keeps_numeric_titles(self):
        """纯数字歌名不能被误剥。"""
        self.assertEqual(music.split_track_prefix("1979"), (0, "1979"))
        self.assertEqual(music.split_track_prefix("300"), (0, "300"))

    def test_keeps_plain_titles(self):
        self.assertEqual(music.split_track_prefix("海阔天空"), (0, "海阔天空"))


class TestLyrics(unittest.TestCase):
    """LRC 解析与合并。"""

    LRC = "[00:01.00]第一行\n[00:05.50]第二行\n"

    def test_looks_like_lrc(self):
        self.assertTrue(music.looks_like_lrc(self.LRC))
        self.assertFalse(music.looks_like_lrc("普通歌词\n没有时间轴"))

    def test_parse_lrc(self):
        parsed = music.parse_lrc(self.LRC)
        self.assertEqual(len(parsed), 2)
        self.assertAlmostEqual(parsed[0][0], 1.0)
        self.assertAlmostEqual(parsed[1][0], 5.5)
        self.assertEqual(parsed[0][1], "第一行")

    def test_parse_multiple_stamps_per_line(self):
        """一行多个时间标签要展开成多条。"""
        parsed = music.parse_lrc("[00:01.00][00:10.00]副歌")
        self.assertEqual(len(parsed), 2)
        self.assertEqual([t for t, _ in parsed], [1.0, 10.0])

    def test_parse_sorts_by_time(self):
        parsed = music.parse_lrc("[00:10.00]后\n[00:01.00]前")
        self.assertEqual([t for _, t in parsed], ["前", "后"])

    def test_merge_translation(self):
        original = "[00:01.00]Hello\n[00:05.00]World\n"
        translation = "[00:01.00]你好\n[00:05.00]世界\n"
        merged = music.merge_lrc(original, translation)
        lines = merged.strip().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn("你好", lines[1])
        self.assertIn("Hello", lines[0])

    def test_merge_keeps_translation_only_lines(self):
        original = "[00:01.00]Hello\n"
        translation = "[00:01.00]你好\n[00:09.00]只有译文\n"
        merged = music.merge_lrc(original, translation)
        self.assertIn("只有译文", merged)

    def test_merge_degrades_on_plain_text(self):
        merged = music.merge_lrc("普通歌词", "译文")
        self.assertIn("普通歌词", merged)
        self.assertIn("译文", merged)

    def test_merge_empty_translation_is_noop(self):
        self.assertEqual(music.merge_lrc(self.LRC, ""), self.LRC)
        self.assertEqual(music.merge_lrc("", "x"), "x")


class TestModelsMusicIntegration(unittest.TestCase):
    """MusicMeta 与 MediaItem 的序列化。"""

    def test_item_round_trip_with_music(self):
        meta = MusicMeta(title="歌", artist="手", album="辑", track_number=2,
                         lyrics="[00:01.00]x", platform="netease")
        item = MediaItem(url="https://x/a.mp3", type=MediaType.AUDIO,
                         source=Source.NETWORK, music=meta)
        data = item.to_dict()
        self.assertEqual(data["type"], "audio")
        self.assertEqual(data["music"]["title"], "歌")
        self.assertEqual(data["music"]["kind"], "song")

        restored = MediaItem.from_dict(data)
        self.assertIsNotNone(restored.music)
        self.assertEqual(restored.music.title, "歌")
        self.assertEqual(restored.music.track_number, 2)
        self.assertEqual(restored.music.platform, "netease")

    def test_item_round_trip_without_music(self):
        item = MediaItem(url="https://x/a.jpg", type=MediaType.IMAGE, source=Source.TAG)
        data = item.to_dict()
        self.assertIsNone(data["music"])
        self.assertIsNone(MediaItem.from_dict(data).music)

    def test_music_meta_unknown_kind_is_safe(self):
        meta = MusicMeta.from_dict({"title": "x", "kind": "bogus"})
        self.assertEqual(meta.kind, TrackKind.SONG)

    def test_music_meta_display(self):
        self.assertEqual(MusicMeta(title="歌", artist="手").display, "手 - 歌")
        self.assertEqual(MusicMeta(title="歌").display, "歌")
        self.assertEqual(MusicMeta(artist="手").display, "手")

    def test_track_kind_labels(self):
        self.assertEqual(TrackKind.ALBUM.label, "专辑")
        self.assertTrue(TrackKind.PLAYLIST.is_collection)
        self.assertFalse(TrackKind.SONG.is_collection)


class TestMusicSortIntegration(unittest.TestCase):
    """音乐条目在整体排序中要排到封面之前。"""

    def test_music_track_beats_cover_and_generic_audio(self):
        from mediaharvest.crawler import sort_items

        cover = MediaItem(url="https://x/cover.jpg", type=MediaType.IMAGE,
                          source=Source.META)
        track = MediaItem(url="https://x/song.mp3", type=MediaType.AUDIO,
                          source=Source.NETWORK, music=MusicMeta(title="歌", artist="手"))
        plain_audio = MediaItem(url="https://x/plain.mp3", type=MediaType.AUDIO,
                                source=Source.TAG)
        ordered = sort_items([cover, plain_audio, track])
        self.assertIs(ordered[0], track, "音乐曲目应排在最前，封面与普通音频在其后")

    def test_generic_audio_ordering_unchanged(self):
        """非音乐通道的音频排序不应被改变。"""
        from mediaharvest.crawler import sort_items

        audio = MediaItem(url="https://x/a.mp3", type=MediaType.AUDIO, source=Source.TAG)
        image = MediaItem(url="https://x/a.jpg", type=MediaType.IMAGE, source=Source.TAG)
        ordered = sort_items([audio, image])
        self.assertEqual(ordered[0].type, MediaType.IMAGE)


class TestFilenameTemplates(unittest.TestCase):
    """文件名模板。"""

    def test_default_template_has_no_track_number(self):
        """文件名不带曲目号：缺字段时 yt-dlp 会产出 NA 前缀。"""
        tmpl = music.get_filename_template("artist-title")
        self.assertNotIn("track_number", tmpl)
        self.assertIn("artist", tmpl)

    def test_unknown_template_falls_back(self):
        self.assertEqual(music.get_filename_template("bogus"),
                         music.FILENAME_TEMPLATES[music.DEFAULT_FILENAME_TEMPLATE])

    def test_build_outtmpl(self):
        self.assertTrue(music.build_outtmpl("title").endswith(".%(ext)s"))
        self.assertIn("/", music.build_outtmpl("title", album_dir=True))
        self.assertTrue(music.build_outtmpl("title", ext="flac").endswith(".flac"))


class TestClassifyStillWorksForPlainAudio(unittest.TestCase):
    """音乐功能不应破坏原有的普通音频判定。"""

    def test_plain_audio_urls(self):
        self.assertEqual(classify("https://x/a.mp3"), MediaType.AUDIO)
        self.assertEqual(classify("https://x/a.flac"), MediaType.AUDIO)
        self.assertEqual(classify("https://x/a.m4a"), MediaType.AUDIO)
        self.assertEqual(classify("https://x/a.ogg"), MediaType.AUDIO)

    def test_plain_audio_not_music_platform(self):
        self.assertFalse(music.is_music_url("https://cdn.example.com/a.mp3"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
