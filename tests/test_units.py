"""单元测试：不需要网络，纯逻辑验证。

运行::

    ./.venv/bin/python -m pytest tests/ -v
    # 或
    ./.venv/bin/python tests/test_units.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mediaharvest import config
from mediaharvest.extractor import Extractor, extract_from_html
from mediaharvest.hls import (
    is_master_playlist,
    parse_master,
    parse_media_playlist,
)
from mediaharvest.models import MediaItem, MediaType, Source
from mediaharvest.utils import (
    classify,
    clean_url,
    ensure_ext,
    ext_from_content_type,
    filename_from_url,
    human_duration,
    human_size,
    parse_size,
    resolve_url,
    sanitize_filename,
    strip_tracking,
    unescape_url,
    url_ext,
)


class TestClassify(unittest.TestCase):
    """媒体类型判定。"""

    def test_by_extension(self):
        cases = [
            ("https://a.com/x.jpg", MediaType.IMAGE),
            ("https://a.com/x.JPEG", MediaType.IMAGE),
            ("https://a.com/x.png?v=2", MediaType.IMAGE),
            ("https://a.com/x.webp", MediaType.IMAGE),
            ("https://a.com/x.svg", MediaType.IMAGE),
            ("https://a.com/v.mp4", MediaType.VIDEO),
            ("https://a.com/v.mp4?token=abc", MediaType.VIDEO),
            ("https://a.com/v.webm", MediaType.VIDEO),
            ("https://a.com/s.m3u8", MediaType.HLS),
            ("https://a.com/s.mpd", MediaType.DASH),
            ("https://a.com/a.mp3", MediaType.AUDIO),
            ("https://a.com/seg.ts", MediaType.SEGMENT),
            ("https://a.com/page.html", MediaType.OTHER),
            ("https://a.com/app.js", MediaType.OTHER),
            ("https://a.com/data.json", MediaType.OTHER),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(classify(url), expected)

    def test_by_content_type(self):
        self.assertEqual(classify("https://a.com/noext", "image/jpeg"), MediaType.IMAGE)
        self.assertEqual(classify("https://a.com/noext", "video/mp4"), MediaType.VIDEO)
        self.assertEqual(
            classify("https://a.com/noext", "application/vnd.apple.mpegurl"), MediaType.HLS
        )
        self.assertEqual(classify("https://a.com/noext", "text/html"), MediaType.OTHER)

    def test_content_type_beats_wrong_ext(self):
        """CDN 常见 .php 伪装成图片，应以 Content-Type 为准。"""
        self.assertEqual(classify("https://a.com/img.php?id=1", "image/png"), MediaType.IMAGE)

    def test_query_hint(self):
        self.assertEqual(classify("https://a.com/photo?format=jpg"), MediaType.IMAGE)
        self.assertEqual(classify("https://a.com/get?type=mp4&id=3"), MediaType.VIDEO)

    def test_m3u8_path_without_ext(self):
        self.assertEqual(classify("https://a.com/hls/master"), MediaType.OTHER)
        self.assertEqual(classify("https://a.com/live/playlist.m3u8"), MediaType.HLS)


class TestUrlHelpers(unittest.TestCase):
    def test_resolve_relative(self):
        base = "https://a.com/dir/page.html"
        self.assertEqual(resolve_url(base, "img.png"), "https://a.com/dir/img.png")
        self.assertEqual(resolve_url(base, "/root.png"), "https://a.com/root.png")
        self.assertEqual(resolve_url(base, "../up.png"), "https://a.com/up.png")
        self.assertEqual(resolve_url(base, "//cdn.com/x.png"), "https://cdn.com/x.png")

    def test_rejects_non_http(self):
        for bad in ("data:image/png;base64,AAA", "javascript:void(0)", "blob:https://a.com/x",
                    "mailto:a@b.com", "#anchor", ""):
            with self.subTest(bad=bad):
                self.assertIsNone(resolve_url("https://a.com/", bad))

    def test_strips_fragment(self):
        self.assertEqual(
            resolve_url("https://a.com/", "x.png#frag"), "https://a.com/x.png"
        )

    def test_unescape(self):
        self.assertEqual(unescape_url("https:\\/\\/a.com\\/x.jpg"), "https://a.com/x.jpg")
        self.assertEqual(unescape_url("https://a.com/x.jpg?a=1&amp;b=2"),
                         "https://a.com/x.jpg?a=1&b=2")
        self.assertEqual(unescape_url("https:\\u002F\\u002Fa.com\\u002Fx.jpg"),
                         "https://a.com/x.jpg")

    def test_clean_trailing_punct(self):
        self.assertEqual(clean_url("https://a.com/x.jpg'),"), "https://a.com/x.jpg")

    def test_strip_tracking(self):
        self.assertEqual(
            strip_tracking("https://a.com/x.jpg?utm_source=fb&id=5"),
            "https://a.com/x.jpg?id=5",
        )
        # 签名参数必须保留
        kept = strip_tracking("https://a.com/x.jpg?sign=abc&t=123")
        self.assertIn("sign=abc", kept)

    def test_url_ext(self):
        self.assertEqual(url_ext("https://a.com/x.JPG?a=1"), "jpg")
        self.assertEqual(url_ext("https://a.com/noext"), "")
        self.assertEqual(url_ext("https://a.com/a.b.c.png"), "png")

    def test_ext_from_content_type(self):
        self.assertEqual(ext_from_content_type("image/jpeg"), "jpg")
        self.assertEqual(ext_from_content_type("video/mp4; charset=utf-8"), "mp4")
        self.assertEqual(ext_from_content_type("image/webp"), "webp")
        self.assertEqual(ext_from_content_type(""), "")


class TestFilename(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(sanitize_filename("a/b\\c:d.jpg"), "a_b_c_d.jpg")
        self.assertEqual(sanitize_filename("  spaced  .png"), "spaced.png")
        self.assertEqual(sanitize_filename(""), "")
        self.assertEqual(sanitize_filename("con.txt"), "_con.txt")
        long = sanitize_filename("x" * 300 + ".jpg")
        self.assertLessEqual(len(long), 110)

    def test_from_url(self):
        self.assertEqual(filename_from_url("https://a.com/p/photo.jpg"), "photo.jpg")
        self.assertEqual(
            filename_from_url("https://a.com/p/photo.jpg?w=100"), "photo.jpg"
        )
        self.assertTrue(filename_from_url("https://a.com/img/12345").endswith("12345"))
        # 无路径时给个基于 hash 的兜底名
        name = filename_from_url("https://a.com/", fallback_stem="media", default_ext="jpg")
        self.assertTrue(name.startswith("media_"))
        self.assertTrue(name.endswith(".jpg"))

    def test_ensure_ext(self):
        self.assertEqual(ensure_ext("a.jpg", "jpg"), "a.jpg")
        self.assertEqual(ensure_ext("a", "png"), "a.png")
        self.assertEqual(ensure_ext("a.php", "jpg"), "a.jpg")
        self.assertEqual(ensure_ext("a.unknown", "png"), "a.unknown.png")


class TestSizeHelpers(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(parse_size("1024"), 1024)
        self.assertEqual(parse_size("10K"), 10 * 1024)
        self.assertEqual(parse_size("1.5M"), int(1.5 * 1024 ** 2))
        self.assertEqual(parse_size("2GB"), 2 * 1024 ** 3)
        self.assertIsNone(parse_size("abc"))
        self.assertIsNone(parse_size(""))

    def test_human_size(self):
        self.assertEqual(human_size(500), "500 B")
        self.assertEqual(human_size(1024), "1.0 KB")
        self.assertEqual(human_size(None), "-")

    def test_human_duration(self):
        self.assertEqual(human_duration(65), "1:05")
        self.assertEqual(human_duration(3725), "1:02:05")
        self.assertEqual(human_duration(None), "-")


class TestExtractor(unittest.TestCase):
    """HTML 媒体抽取。"""

    BASE = "https://example.com/page/index.html"

    def test_basic_tags(self):
        html = """
        <html><body>
          <img src="/a.jpg">
          <img src="https://cdn.com/b.png">
          <video src="movie.mp4" poster="poster.jpg"></video>
          <audio src="sound.mp3"></audio>
          <a href="direct.webm">link</a>
        </body></html>
        """
        items = extract_from_html(html, self.BASE)
        urls = {i.url for i in items}
        self.assertIn("https://example.com/a.jpg", urls)
        self.assertIn("https://cdn.com/b.png", urls)
        self.assertIn("https://example.com/page/movie.mp4", urls)
        self.assertIn("https://example.com/page/poster.jpg", urls)
        self.assertIn("https://example.com/page/sound.mp3", urls)
        self.assertIn("https://example.com/page/direct.webm", urls)

    def test_type_of_video_tag(self):
        html = '<video src="https://cdn.com/stream"></video>'
        items = extract_from_html(html, self.BASE)
        video = [i for i in items if i.url.endswith("/stream")]
        self.assertEqual(len(video), 1)
        self.assertEqual(video[0].type, MediaType.VIDEO)

    def test_lazy_attributes(self):
        html = """
        <img src="placeholder.gif" data-src="/real1.jpg" data-original="/real2.jpg">
        <img data-lazy-src="/real3.jpg">
        """
        items = extract_from_html(html, self.BASE)
        urls = {i.url for i in items}
        self.assertIn("https://example.com/real1.jpg", urls)
        self.assertIn("https://example.com/real2.jpg", urls)
        self.assertIn("https://example.com/real3.jpg", urls)
        lazy = [i for i in items if i.source == Source.LAZY]
        self.assertGreaterEqual(len(lazy), 3)

    def test_srcset_picks_largest(self):
        html = """
        <img srcset="small.jpg 320w, medium.jpg 800w, large.jpg 1600w"
             src="fallback.jpg">
        """
        items = extract_from_html(html, self.BASE)
        srcset_items = [i for i in items if i.source == Source.SRCSET]
        self.assertEqual(len(srcset_items), 3)
        primaries = [i for i in srcset_items if i.primary]
        self.assertEqual(len(primaries), 1)
        self.assertTrue(primaries[0].url.endswith("large.jpg"))
        # 三者应共享同一个 group
        groups = {i.group for i in srcset_items}
        self.assertEqual(len(groups), 1)

    def test_css_background(self):
        html = """
        <style>.hero { background-image: url('/bg1.jpg'); }</style>
        <div style="background: url(bg2.png) no-repeat"></div>
        """
        items = extract_from_html(html, self.BASE)
        urls = {i.url for i in items}
        self.assertIn("https://example.com/bg1.jpg", urls)
        self.assertIn("https://example.com/page/bg2.png", urls)
        self.assertTrue(all(i.source == Source.STYLE for i in items))

    def test_meta_tags(self):
        html = """
        <meta property="og:image" content="https://cdn.com/share.jpg">
        <meta property="og:video" content="https://cdn.com/share.mp4">
        <meta name="twitter:image" content="/tw.png">
        """
        items = extract_from_html(html, self.BASE)
        urls = {i.url for i in items}
        self.assertIn("https://cdn.com/share.jpg", urls)
        self.assertIn("https://cdn.com/share.mp4", urls)
        self.assertIn("https://example.com/tw.png", urls)
        self.assertTrue(all(i.source == Source.META for i in items))

    def test_script_urls(self):
        html = """
        <script>
          var videoUrl = "https://cdn.com/hls/master.m3u8";
          var img = 'https:\\/\\/cdn.com\\/escaped.jpg';
        </script>
        """
        items = extract_from_html(html, self.BASE)
        urls = {i.url for i in items}
        self.assertIn("https://cdn.com/hls/master.m3u8", urls)
        self.assertIn("https://cdn.com/escaped.jpg", urls)

    def test_json_ld(self):
        html = """
        <script type="application/ld+json">
        {"@type":"Article","image":{"url":"https://cdn.com/ld.jpg"},
         "video":{"contentUrl":"https://cdn.com/ld.mp4"}}
        </script>
        """
        items = extract_from_html(html, self.BASE)
        urls = {i.url for i in items}
        self.assertIn("https://cdn.com/ld.jpg", urls)
        self.assertIn("https://cdn.com/ld.mp4", urls)

    def test_skips_placeholders_and_data_uris(self):
        html = """
        <img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7">
        <img src="/blank.gif">
        <img src="/spacer.gif">
        <img src="/real.jpg">
        """
        items = extract_from_html(html, self.BASE)
        urls = {i.url for i in items}
        self.assertEqual(len(items), 1)
        self.assertIn("https://example.com/real.jpg", urls)

    def test_deduplicates(self):
        html = """
        <img src="/same.jpg">
        <img src="/same.jpg">
        <a href="/same.jpg">x</a>
        """
        items = extract_from_html(html, self.BASE)
        self.assertEqual(len(items), 1)

    def test_same_host_filter(self):
        html = """
        <img src="/local.jpg">
        <img src="https://other-cdn.com/remote.jpg">
        """
        items = extract_from_html(html, self.BASE, same_host_only=True)
        urls = {i.url for i in items}
        self.assertIn("https://example.com/local.jpg", urls)
        self.assertNotIn("https://other-cdn.com/remote.jpg", urls)

    def test_segments_excluded_by_default(self):
        html = '<video src="seg.ts"></video><a href="v.mp4">v</a>'
        items = extract_from_html(html, self.BASE)
        self.assertFalse(any(i.url.endswith(".ts") for i in items))
        items2 = extract_from_html(html, self.BASE, include_segments=True)
        self.assertTrue(any(i.url.endswith(".ts") for i in items2))

    def test_picture_sources(self):
        html = """
        <picture>
          <source srcset="/hi.webp" type="image/webp">
          <img src="/fallback.jpg">
        </picture>
        """
        items = extract_from_html(html, self.BASE)
        urls = {i.url for i in items}
        self.assertIn("https://example.com/hi.webp", urls)
        self.assertIn("https://example.com/fallback.jpg", urls)

    def test_empty_html(self):
        self.assertEqual(extract_from_html("", self.BASE), [])
        self.assertEqual(extract_from_html("<html></html>", self.BASE), [])


class TestHlsParsing(unittest.TestCase):
    MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1280000,RESOLUTION=640x360,CODECS="avc1.42c01e,mp4a.40.2"
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5120000,RESOLUTION=1920x1080
high/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2560000,RESOLUTION=1280x720
mid/index.m3u8
"""

    MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:9.009,
seg0.ts
#EXTINF:9.009,
seg1.ts
#EXTINF:3.003,
seg2.ts
#EXT-X-ENDLIST
"""

    def test_is_master(self):
        self.assertTrue(is_master_playlist(self.MASTER))
        self.assertFalse(is_master_playlist(self.MEDIA))

    def test_parse_master_sorted(self):
        base = "https://a.com/hls/master.m3u8"
        variants = parse_master(self.MASTER, base)
        self.assertEqual(len(variants), 3)
        # 按分辨率升序，最后一个即最高画质
        self.assertEqual(variants[0].height, 360)
        self.assertEqual(variants[-1].height, 1080)
        self.assertEqual(variants[-1].url, "https://a.com/hls/high/index.m3u8")
        self.assertEqual(variants[0].bandwidth, 1280000)

    def test_parse_media(self):
        base = "https://a.com/hls/index.m3u8"
        pl = parse_media_playlist(self.MEDIA, base)
        self.assertEqual(len(pl.segments), 3)
        self.assertTrue(pl.is_endlist)
        self.assertAlmostEqual(pl.total_duration, 21.021, places=2)
        self.assertEqual(pl.segments[0].url, "https://a.com/hls/seg0.ts")
        self.assertEqual(pl.encryption, "NONE")

    def test_parse_encrypted(self):
        text = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x00000000000000000000000000000001
#EXTINF:10,
s0.ts
#EXTINF:10,
s1.ts
"""
        pl = parse_media_playlist(text, "https://a.com/hls/index.m3u8")
        self.assertEqual(pl.encryption, "AES-128")
        self.assertEqual(pl.segments[0].key_url, "https://a.com/hls/key.bin")
        self.assertEqual(pl.segments[0].iv, bytes.fromhex("00000000000000000000000000000001"))

    def test_iv_derived_from_sequence(self):
        """没给 IV 时应按 media sequence 推导，否则解密会失败。"""
        text = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:5
#EXT-X-KEY:METHOD=AES-128,URI="key.bin"
#EXTINF:10,
s0.ts
#EXTINF:10,
s1.ts
"""
        pl = parse_media_playlist(text, "https://a.com/index.m3u8")
        self.assertEqual(pl.segments[0].seq, 5)
        self.assertEqual(pl.segments[0].iv, (5).to_bytes(16, "big"))
        self.assertEqual(pl.segments[1].iv, (6).to_bytes(16, "big"))

    def test_byterange(self):
        text = """#EXTM3U
#EXTINF:10,
#EXT-X-BYTERANGE:1000@0
s.ts
#EXTINF:10,
#EXT-X-BYTERANGE:2000
s.ts
"""
        pl = parse_media_playlist(text, "https://a.com/index.m3u8")
        self.assertEqual(pl.segments[0].byte_range, (0, 1000))
        # 未指定 offset 时应紧接上一段
        self.assertEqual(pl.segments[1].byte_range, (1000, 2000))

    def test_fmp4_init_segment(self):
        text = """#EXTM3U
#EXT-X-MAP:URI="init.mp4"
#EXTINF:10,
s0.m4s
"""
        pl = parse_media_playlist(text, "https://a.com/index.m3u8")
        self.assertIsNotNone(pl.init_segment)
        self.assertEqual(pl.init_segment.url, "https://a.com/init.mp4")
        self.assertTrue(pl.is_fmp4)


class TestMediaItem(unittest.TestCase):
    def test_roundtrip(self):
        item = MediaItem(
            url="https://a.com/x.jpg", type=MediaType.IMAGE, source=Source.LAZY,
            page_url="https://a.com/", width=100, height=200, title="hello",
        )
        data = item.to_dict()
        self.assertEqual(data["type"], "image")
        self.assertEqual(data["source"], "lazy")
        self.assertEqual(data["id"], item.id)
        self.assertEqual(data["host"], "a.com")

        restored = MediaItem.from_dict(data)
        self.assertEqual(restored.url, item.url)
        self.assertEqual(restored.type, MediaType.IMAGE)
        self.assertEqual(restored.source, Source.LAZY)
        self.assertEqual(restored.width, 100)

    def test_stable_id(self):
        a = MediaItem(url="https://a.com/x.jpg", type=MediaType.IMAGE, source=Source.TAG)
        b = MediaItem(url="https://a.com/x.jpg", type=MediaType.VIDEO, source=Source.LAZY)
        self.assertEqual(a.id, b.id)  # id 只取决于 url

    def test_display_name(self):
        item = MediaItem(
            url="https://a.com/path/%E4%B8%AD%E6%96%87.jpg",
            type=MediaType.IMAGE, source=Source.TAG,
        )
        self.assertEqual(item.display_name, "中文.jpg")

    def test_labels(self):
        self.assertEqual(MediaType.HLS.label, "HLS 流")
        self.assertEqual(Source.NETWORK.label, "网络嗅探")


class TestConfig(unittest.TestCase):
    """配置文件：读写、优先级、类型校验、模板生成。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mh-cfg-")
        self.path = os.path.join(self.tmp, "config.toml")
        # 清掉可能干扰的环境变量（含新增的端口等覆盖）
        self._saved_env = {
            key: os.environ.pop(key, None)
            for key in (config.ENV_CONFIG, config.ENV_OUT_DIR,
                        "MEDIAHARVEST_PORT", "MEDIAHARVEST_HOST", "MEDIAHARVEST_PROXY")
        }
        # 切到临时目录，避免读到/写坏项目里真实的 mediaharvest.toml
        self._cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self._cwd)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text: str) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    # ---- 读取与容错 --------------------------------------------------

    def test_missing_file_uses_defaults(self):
        cfg = config.load_config(self.path)
        self.assertFalse(cfg.exists)
        self.assertEqual(cfg.error, "")
        self.assertEqual(cfg.get("web.port"), 8848)
        self.assertEqual(cfg.get("download.concurrency"), 8)
        self.assertEqual(
            config.effective_out_dir(explicit_config=self.path),
            os.path.abspath(config.DEFAULT_OUT_DIR),
        )

    def test_defaults_cover_every_schema_key(self):
        """每个有说明的配置项都必须有默认值，否则模板会缺项。"""
        self.assertEqual(set(config.SCHEMA), set(config.DEFAULTS))

    def test_reads_multiple_sections(self):
        self.write(
            '[download]\nout_dir = "~/dl"\nconcurrency = 16\n\n'
            '[web]\nport = 9000\nhost = "0.0.0.0"\n\n'
            '[crawl]\nrender = "always"\n'
        )
        cfg = config.load_config(self.path)
        self.assertTrue(cfg.exists)
        self.assertEqual(cfg.get("download.concurrency"), 16)
        self.assertEqual(cfg.get("web.port"), 9000)
        self.assertEqual(cfg.get("web.host"), "0.0.0.0")
        self.assertEqual(cfg.get("crawl.render"), "always")
        # 未写的项回落到默认值
        self.assertEqual(cfg.get("advanced.max_segments"), 20000)

    def test_broken_toml_reports_error_without_crashing(self):
        self.write("[download\nout_dir = ")
        cfg = config.load_config(self.path)
        self.assertTrue(cfg.error)
        # 出错时仍能取到默认值，程序不该崩
        self.assertEqual(cfg.get("web.port"), 8848)

    def test_wrong_type_warns_and_falls_back(self):
        self.write('[web]\nport = "not-a-number"\n')
        cfg = config.load_config(self.path)
        self.assertEqual(cfg.get("web.port"), 8848)
        self.assertTrue(any("web.port" in w for w in cfg.warnings))

    def test_string_bool_is_accepted(self):
        self.write('[download]\noverwrite = "true"\nflat = "no"\n')
        cfg = config.load_config(self.path)
        self.assertTrue(cfg.get("download.overwrite"))
        self.assertFalse(cfg.get("download.flat"))

    def test_types_list_is_joined(self):
        self.write('[crawl]\ntypes = ["image", "video"]\n')
        cfg = config.load_config(self.path)
        self.assertEqual(cfg.get("crawl.types"), "image,video")

    # ---- 优先级 ------------------------------------------------------

    def test_cli_beats_config(self):
        self.write('[download]\nout_dir = "/from/config"\n')
        self.assertEqual(
            config.effective_out_dir("/from/cli", self.path), "/from/cli"
        )

    def test_config_beats_default(self):
        self.write('[download]\nout_dir = "/from/config"\n')
        self.assertEqual(
            config.effective_out_dir("", self.path), "/from/config"
        )

    def test_env_beats_config(self):
        self.write('[download]\nout_dir = "/from/config"\n')
        os.environ[config.ENV_OUT_DIR] = "/from/env"
        self.assertEqual(config.effective_out_dir("", self.path), "/from/env")

    def test_blank_cli_value_is_ignored(self):
        self.write('[download]\nout_dir = "/from/config"\n')
        self.assertEqual(
            config.effective_out_dir("   ", self.path), "/from/config"
        )

    def test_env_overrides_other_keys(self):
        """端口等不方便走命令行的项，支持环境变量覆盖。"""
        self.write("[web]\nport = 9000\n")
        os.environ["MEDIAHARVEST_PORT"] = "9100"
        cfg = config.load_config(self.path)
        self.assertEqual(cfg.get("web.port"), 9100)

    # ---- 配置文件定位 ------------------------------------------------

    def test_project_config_found_regardless_of_cwd(self):
        """配置文件按「项目位置」定位，不随工作目录漂移。

        回归用例：早期实现用相对路径查找，导致在 PyCharm 里直接 Run
        （工作目录不是项目目录）时找不到配置，静默回落到内置默认值，
        下载目录也就不是用户配置的那个了。
        """
        project_config = config.project_config_path()
        self.assertTrue(
            os.path.isfile(project_config),
            f"项目配置文件应当存在: {project_config}",
        )
        # 换到若干个不相关的工作目录，都应解析到同一个项目配置文件
        for cwd in ("/", tempfile.gettempdir(), os.path.expanduser("~")):
            if not os.path.isdir(cwd):
                continue
            os.chdir(cwd)
            self.assertEqual(
                config.resolve_config_path(), project_config,
                f"在 {cwd} 下未能定位到项目配置文件",
            )

    def test_project_config_beats_user_config(self):
        """项目配置优先于用户级配置。"""
        self.assertNotEqual(
            config.project_config_path(), config.user_config_path()
        )
        os.chdir(self.tmp)  # 临时目录里没有 mediaharvest.toml
        self.assertEqual(config.resolve_config_path(), config.project_config_path())

    def test_cwd_config_used_when_no_project_config(self):
        """项目里没有配置时，当前目录放的配置也能生效。"""
        if os.path.isfile(config.project_config_path()):
            self.skipTest("项目里已有配置，本用例不适用")
        local = os.path.join(self.tmp, config.PROJECT_CONFIG_NAME)
        with open(local, "w", encoding="utf-8") as fh:
            fh.write('[download]\nout_dir = "/from/cwd"\n')
        os.chdir(self.tmp)
        self.assertEqual(config.resolve_config_path(), local)

    # ---- 路径处理 ----------------------------------------------------

    def test_parse_out_dir_expands(self):
        os.environ["MH_TEST_VAR"] = "expanded"
        try:
            self.assertEqual(config.parse_out_dir("~/x"), os.path.expanduser("~/x"))
            self.assertTrue(
                config.parse_out_dir("$MH_TEST_VAR/sub").endswith("expanded/sub")
            )
            self.assertTrue(os.path.isabs(config.parse_out_dir("rel/path")))
            self.assertEqual(config.parse_out_dir(""), "")
        finally:
            os.environ.pop("MH_TEST_VAR", None)

    def test_out_dir_abs_expands_config_value(self):
        self.write('[download]\nout_dir = "~/Downloads/media"\n')
        cfg = config.load_config(self.path)
        self.assertEqual(
            cfg.out_dir_abs, os.path.abspath(os.path.expanduser("~/Downloads/media"))
        )

    # ---- 写入 --------------------------------------------------------

    def test_save_and_reload(self):
        config.save_config({"download.out_dir": "~/Downloads/media"}, self.path)
        cfg = config.load_config(self.path)
        self.assertTrue(cfg.exists)
        self.assertEqual(cfg.get("download.out_dir"), "~/Downloads/media")

    def test_save_multiple_sections_at_once(self):
        config.save_config(
            {"download.out_dir": "/dl", "web.port": 9001}, self.path
        )
        cfg = config.load_config(self.path)
        self.assertEqual(cfg.get("download.out_dir"), "/dl")
        self.assertEqual(cfg.get("web.port"), 9001)

    def test_save_preserves_unrelated_keys(self):
        self.write(
            '[download]\nout_dir = "/old"\ncustom = "keepme"\n\n'
            '[unknown_section]\nfoo = 1\n'
        )
        config.save_config({"web.port": 9999}, self.path)
        text = open(self.path, encoding="utf-8").read()
        self.assertIn("keepme", text)
        self.assertIn("unknown_section", text)
        self.assertIn("foo = 1", text)
        self.assertIn("9999", text)
        # 未参与更新的值保持原样
        cfg = config.load_config(self.path)
        self.assertEqual(cfg.get("download.out_dir"), "/old")

    def test_save_creates_parent_dirs(self):
        nested = os.path.join(self.tmp, "a", "b", "config.toml")
        config.save_config({"web.port": 9002}, nested)
        self.assertTrue(os.path.isfile(nested))

    def test_save_special_chars_survive(self):
        weird = '/tmp/has "quotes" and \\backslash'
        config.save_config({"download.out_dir": weird}, self.path)
        cfg = config.load_config(self.path)
        self.assertEqual(cfg.get("download.out_dir"), weird)

    def test_save_non_ascii_survives(self):
        value = "~/下载/我的媒体"
        config.save_config({"download.out_dir": value}, self.path)
        cfg = config.load_config(self.path)
        self.assertEqual(cfg.get("download.out_dir"), value)

    def test_save_empty_clears_value(self):
        config.save_config({"download.out_dir": "/x"}, self.path)
        config.save_config({"download.out_dir": ""}, self.path)
        cfg = config.load_config(self.path)
        self.assertEqual(cfg.get("download.out_dir"), "")

    def test_save_rejects_bad_key(self):
        with self.assertRaises(config.ConfigError):
            config.save_config({"no_section": 1}, self.path)

    def test_saved_file_is_valid_toml(self):
        config.save_config(
            {"download.out_dir": "/dl", "web.port": 9003, "download.overwrite": True},
            self.path,
        )
        cfg = config.load_config(self.path)
        self.assertEqual(cfg.error, "", f"生成的 TOML 无法解析: {cfg.error}")

    # ---- 模板 --------------------------------------------------------

    def test_template_contains_all_keys(self):
        config.write_template(self.path)
        cfg = config.load_config(self.path)
        self.assertEqual(cfg.error, "", "模板本身必须能被解析")
        # 模板写出的默认值应与 DEFAULTS 一致
        for key, expected in config.DEFAULTS.items():
            self.assertEqual(cfg.get(key), expected, f"{key} 在模板里不一致")

    def test_template_is_commented_when_asked(self):
        config.write_template(self.path, commented=True)
        # 全注释掉时，解析结果应回落到默认值
        cfg = config.load_config(self.path)
        self.assertEqual(cfg.error, "")
        self.assertEqual(cfg.get("web.port"), 8848)

    def test_template_refuses_overwrite_without_force(self):
        config.write_template(self.path)
        with self.assertRaises(config.ConfigError):
            config.write_template(self.path)
        # 加了 force 就可以
        config.write_template(self.path, force=True)

    # ---- 展示 --------------------------------------------------------

    def test_describe_config_reports_source(self):
        self.write('[download]\nout_dir = "/from/config"\n')
        text = config.describe_config(self.path)
        self.assertIn("配置文件", text)
        self.assertIn("/from/config", text)
        # 端口等新项也要出现在展示里
        self.assertIn("port", text)

    def test_describe_config_marks_cli_source(self):
        text = config.describe_config(self.path, "/from/cli")
        self.assertIn("命令行", text)

    def test_non_string_out_dir_reports_error(self):
        """out_dir 写成非字符串时，取出应安全降级。"""
        self.write("[download]\nout_dir = 123\n")
        cfg = config.load_config(self.path)
        value = cfg.get("download.out_dir")
        self.assertIsInstance(value, str)
        self.assertTrue(any("out_dir" in w for w in cfg.warnings) or value == "123")


if __name__ == "__main__":
    unittest.main(verbosity=2)
