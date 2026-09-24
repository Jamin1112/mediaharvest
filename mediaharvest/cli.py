"""命令行入口：``mediaharvest`` / ``python -m mediaharvest``。

典型用法::

    # 抓取并下载全部图片和视频
    mh https://example.com/gallery

    # 只列出资源，不下载
    mh https://example.com --list

    # 只要视频，最高并发，存到指定目录
    mh https://example.com -t video -c 16 -o ~/Downloads/media

    # 动态页面强制用无头浏览器渲染
    mh https://example.com --render always --show-browser

    # 整站爬取两层，限定链接包含 /post/
    mh https://example.com --depth 2 --max-pages 30 --link-pattern '/post/'

    # 需要登录的页面：导入浏览器 Cookie
    mh https://example.com --cookies-from-browser chrome

    # 预览确认后再下载（交互式）
    mh https://example.com --interactive
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Dict, List, Optional, Sequence

# 允许直接执行本文件
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "mediaharvest"

from . import music as music_mod
from . import ytdlp as ytdlp_mod
from .browser import ensure_browser_path, playwright_available
from .config import (
    DEFAULT_OUT_DIR,
    ENV_OUT_DIR,
    ConfigError,
    config_candidates,
    describe_config,
    effective_out_dir,
    load_config,
    parse_out_dir,
    save_config,
    toml_available,
    write_template,
)
from .crawler import CrawlOptions, Crawler, CrawlReport
from .downloader import Downloader
from .hls import find_ffmpeg
from .models import DownloadResult, MediaItem, MediaType
from .utils import human_size, parse_size

# 终端着色（无依赖，自动降级）
_TTY = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def dim(t: str) -> str:
    return _c(t, "2")


def bold(t: str) -> str:
    return _c(t, "1")


def cyan(t: str) -> str:
    return _c(t, "36")


def green(t: str) -> str:
    return _c(t, "32")


def yellow(t: str) -> str:
    return _c(t, "33")


def red(t: str) -> str:
    return _c(t, "31")


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mh",
        description="网页图片/视频爬取下载工具（支持静态页、JS 动态页、m3u8 流）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("典型用法::")[-1] if "典型用法::" in __doc__ else "",
    )
    parser.add_argument("url", nargs="?", help="目标网址")
    parser.add_argument("-o", "--output", default="",
                        help="保存目录（临时覆盖配置，默认取配置文件 / downloads）")
    parser.add_argument("--config", default="", metavar="PATH", help="指定配置文件路径")
    parser.add_argument("--init-config", action="store_true",
                        help="生成带注释的配置文件模板后退出")
    parser.add_argument("--force", action="store_true",
                        help="配合 --init-config：覆盖已存在的配置文件")
    parser.add_argument("--set-out-dir", default=None, metavar="DIR",
                        help="把下载目录写入配置文件后退出（即配置下载地址）")
    parser.add_argument("--unset-out-dir", action="store_true",
                        help="清除配置文件里的下载目录后退出")
    parser.add_argument("--show-config", action="store_true", help="显示当前生效的配置")
    parser.add_argument("-t", "--types", default=None,
                        help="要抓取的类型，逗号分隔: image,video,audio,hls,dash")
    parser.add_argument("-c", "--concurrency", type=int, default=None, help="下载并发数")
    parser.add_argument("-j", "--page-concurrency", type=int, default=None, help="页面抓取并发数")

    # 抓取策略
    g = parser.add_argument_group("抓取策略")
    g.add_argument("--render", choices=["auto", "always", "never"], default=None,
                   help="无头浏览器渲染策略（默认 auto：按需启用）")
    g.add_argument("--show-browser", action="store_true", default=None,
                   help="显示浏览器窗口（调试用）")
    g.add_argument("--no-scroll", action="store_true", default=None,
                   help="不自动滚动页面")
    g.add_argument("--wait", type=float, default=None, help="渲染后额外等待秒数")
    g.add_argument("--browser-timeout", type=float, default=None, help="浏览器超时秒数")
    g.add_argument("--ytdlp", choices=["auto", "always", "never"], default=None,
                   help="yt-dlp 使用策略")
    g.add_argument("--segments", action="store_true", help="把 .ts/.m4s 分片也列入")
    g.add_argument("--same-host", action="store_true", default=None,
                   help="只保留同域名资源")

    # 站内爬取
    g2 = parser.add_argument_group("站内爬取")
    g2.add_argument("--depth", type=int, default=None, help="跟随站内链接的深度（0 只抓当前页）")
    g2.add_argument("--max-pages", type=int, default=None, help="最多抓取页面数")
    g2.add_argument("--link-pattern", default="", help="只跟随匹配该正则的链接")

    # 音乐
    gm = parser.add_argument_group("音乐")
    gm.add_argument("--no-music", action="store_true", default=None,
                    help="关闭音乐通道（音乐 URL 按普通网页处理）")
    gm.add_argument("--quality", choices=music_mod.quality_keys(), default=None,
                    help="音质档位：best 最高 / lossless 仅无损 / high / medium / low 省流")
    gm.add_argument("--no-playlist", dest="no_playlist", action="store_true", default=None,
                    help="不展开专辑/歌单/歌手，只抓单个目标")
    gm.add_argument("--playlist-limit", type=int, default=None,
                    help="单个专辑/歌单最多抓多少首（默认 200，防止巨型歌单失控）")
    gm.add_argument("--no-lyrics", action="store_true", default=None, help="不抓取歌词")
    gm.add_argument("--no-cover", action="store_true", default=None, help="不抓取并嵌入封面")
    gm.add_argument("--no-tags", action="store_true", default=None, help="下载后不写音乐标签")
    gm.add_argument("--lrc-file", action="store_true", default=None,
                    help="在音频文件旁额外保存 .lrc 歌词文件")
    gm.add_argument("--music-info", action="store_true",
                    help="只识别音乐 URL 的平台与类型后退出")

    # 网络
    g3 = parser.add_argument_group("网络")
    g3.add_argument("--timeout", type=float, default=None, help="HTTP 超时秒数")
    g3.add_argument("--retries", type=int, default=None, help="失败重试次数")
    g3.add_argument("--proxy", default=None, help="代理，如 http://127.0.0.1:7890")
    g3.add_argument("--user-agent", default=None, help="自定义 User-Agent")
    g3.add_argument("--cookie", default=None, help="Cookie 字符串，如 'a=1; b=2'")
    g3.add_argument("--cookies-from-browser", default=None,
                    help="从浏览器导入 Cookie: chrome/firefox/edge/safari")
    g3.add_argument("--cookie-file", default="", help="Netscape 格式 Cookie 文件")

    # 下载
    g4 = parser.add_argument_group("下载")
    g4.add_argument("--max-size", default=None, help="单文件体积上限，如 200M")
    g4.add_argument("--min-size", default=None, help="单文件体积下限，如 5K")
    g4.add_argument("--overwrite", action="store_true", default=None, help="覆盖已存在文件")
    g4.add_argument("--flat", action="store_true", default=None, help="不按类型分目录，全部平铺")
    g4.add_argument("--hls-concurrency", type=int, default=None, help="HLS 分片并发数")
    g4.add_argument("--max-segments", type=int, default=None, help="单个流的 M3U8 分片上限")
    g4.add_argument("--keep-ts", action="store_true", default=None,
                    help="流媒体转封装后保留原始 .ts 文件")

    # 输出与交互
    g5 = parser.add_argument_group("输出")
    g5.add_argument("--list", action="store_true", help="只列出资源，不下载")
    g5.add_argument("--json", dest="json_out", action="store_true", help="以 JSON 输出结果")
    g5.add_argument("--json-file", default="", help="把 JSON 结果写入文件")
    g5.add_argument("--interactive", "-i", action="store_true", help="下载前交互式挑选资源")
    g5.add_argument("--quiet", "-q", action="store_true", help="减少输出")
    g5.add_argument("--selfcheck", action="store_true", help="检查运行环境与依赖")
    return parser


# --------------------------------------------------------------------------
# 输出辅助
# --------------------------------------------------------------------------

_TYPE_TAG = {
    MediaType.IMAGE: ("图片", "36"),
    MediaType.VIDEO: ("视频", "35"),
    MediaType.AUDIO: ("音频", "33"),
    MediaType.HLS: ("HLS", "35"),
    MediaType.DASH: ("DASH", "35"),
    MediaType.SEGMENT: ("分片", "2"),
    MediaType.OTHER: ("其他", "2"),
}


def render_item_line(idx: int, item: MediaItem) -> str:
    label, color = _TYPE_TAG.get(item.type, ("其他", "2"))
    tag = _c(f"{label:>4}", color)
    size = human_size(item.size) if item.size else ""
    dims = f"{item.width}x{item.height}" if item.width and item.height else ""
    extra = " ".join(x for x in (size, dims) if x)
    name = item.display_name[:46]
    return f"  {idx:>3}. {tag} {name:<48} {dim(extra):<16} {dim(item.url[:70])}"


def print_report(report: CrawlReport, *, verbose: bool = True, limit: int = 0) -> None:
    print()
    print(bold(f"页面: {report.title or report.start_url}"))
    if report.pages and report.pages[0].final_url:
        print(dim(f"地址: {report.pages[0].final_url}"))
    counts = report.count_by_type()
    summary = "  ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
    print(f"共 {bold(str(len(report.items)))} 个资源  {dim(summary)}")
    if report.pages:
        page = report.pages[0]
        flags = []
        if page.rendered:
            flags.append("已渲染")
        if page.meta.get("requests"):
            flags.append(f"嗅探请求 {page.meta['requests']}")
        if flags:
            print(dim("  " + " / ".join(flags)))
    if verbose:
        shown = report.items if limit <= 0 else report.items[:limit]
        for idx, item in enumerate(shown, 1):
            print(render_item_line(idx, item))
        if limit > 0 and len(report.items) > limit:
            print(dim(f"  … 其余 {len(report.items) - limit} 个已省略"))
    if report.errors:
        print(yellow(f"  警告 {len(report.errors)} 条:"))
        for err in report.errors[:5]:
            print(yellow(f"    - {err[:160]}"))


class ProgressPrinter:
    """下载进度显示。"""

    def __init__(self, total: int, quiet: bool = False) -> None:
        self.total = total
        self.quiet = quiet
        self.done = 0
        self.ok = 0
        self.failed = 0
        self.skipped = 0
        self.bytes = 0

    def __call__(self, result: DownloadResult) -> None:
        self.done += 1
        if result.ok:
            self.ok += 1
            self.bytes += result.size
            status = green("✓")
        elif result.skipped:
            self.skipped += 1
            status = dim("–")
        else:
            self.failed += 1
            status = red("✗")
        if self.quiet:
            return
        name = result.item.display_name[:44]
        detail = human_size(result.size) if result.ok else (result.error or "")[:40]
        print(f"  [{self.done:>{len(str(self.total))}}/{self.total}] {status} {name:<46} {dim(detail)}")


def print_summary(results: Sequence[DownloadResult], out_dir: str) -> None:
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok and not r.skipped]
    skipped = [r for r in results if r.skipped]
    total_bytes = sum(r.size for r in ok)
    print()
    print(bold("下载完成"))
    print(f"  成功 {green(str(len(ok)))}   失败 {red(str(len(failed))) if failed else '0'}   "
          f"跳过 {len(skipped)}   共 {human_size(total_bytes)}")
    print(f"  保存于: {cyan(os.path.abspath(out_dir))}")
    if failed:
        print(yellow("  失败明细:"))
        for res in failed[:10]:
            print(yellow(f"    - {res.item.display_name[:50]}: {res.error[:90]}"))


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

def selfcheck() -> int:
    print(bold("运行环境自检"))
    print(f"  Python      : {sys.version.split()[0]}")
    print(f"  工作目录    : {os.getcwd()}")

    ok = True

    for name, mod in (("httpx", "httpx"), ("bs4", "bs4"), ("lxml", "lxml")):
        try:
            __import__(mod)
            print(f"  {name:<12}: {green('已安装')}")
        except ImportError:
            print(f"  {name:<12}: {red('缺失')}  ← pip install {mod}")
            ok = False

    avail, reason = playwright_available()
    if avail:
        print(f"  浏览器渲染  : {green('可用')}")
    else:
        print(f"  浏览器渲染  : {yellow('不可用')} ({reason})")
        print(dim("                 安装: python -m playwright install chromium"))

    if ytdlp_mod.ytdlp_available():
        print(f"  yt-dlp      : {green('可用')}")
    else:
        print(f"  yt-dlp      : {yellow('不可用')} (pip install yt-dlp)")

    ffmpeg = find_ffmpeg()
    if ffmpeg:
        print(f"  ffmpeg      : {green('可用')} ({ffmpeg})")
    else:
        print(f"  ffmpeg      : {dim('未找到')} → 使用内置 TS 转封装")

    try:
        from .remux import ts_to_mp4  # noqa: F401

        print(f"  内置转封装  : {green('可用')}")
    except ImportError:
        print(f"  内置转封装  : {dim('未启用（.ts 仍可播放）')}")

    # 音乐标签写入：有 mutagen 最好，没有则用内置纯 Python 实现
    try:
        from .tags import mutagen_available, write_tags  # noqa: F401

        if mutagen_available():
            print(f"  音乐标签    : {green('可用')} (mutagen)")
        else:
            print(f"  音乐标签    : {green('可用')} (内置实现，建议 pip install mutagen)")
    except ImportError:
        print(f"  音乐标签    : {yellow('不可用')} ← 无法写入 ID3/歌词")
        ok = False

    try:
        from . import music as _music

        supported = [p.name for p in _music.MUSIC_PLATFORMS if p.supported]
        print(f"  音乐平台    : {green(str(len(supported)) + ' 个可抓')} "
              f"{dim('（' + '、'.join(supported[:4]) + ' 等）')}")
    except Exception:
        print(f"  音乐平台    : {yellow('不可用')}")

    try:
        from Crypto.Cipher import AES  # noqa: F401

        print(f"  AES 解密    : {green('可用（支持加密 m3u8）')}")
    except ImportError:
        print(f"  AES 解密    : {yellow('不可用')} (pip install pycryptodome)")
        ok = False

    cfg = load_config()
    if toml_available():
        print(f"  TOML 配置   : {green('可用')}")
        if cfg.error:
            print(f"  配置文件    : {red('有误')} ({cfg.error})")
        elif cfg.get_str("download.out_dir", ""):
            print(f"  配置文件    : {dim(cfg.path)}")
            print(f"  下载地址    : {green(cfg.out_dir())} {dim('(来自配置文件)')}")
        else:
            print(f"  下载地址    : {dim('未配置')} → 默认 {os.path.abspath(DEFAULT_OUT_DIR)}")
    else:
        print(f"  TOML 配置   : {yellow('不可用')} (pip install tomli)")

    print()
    print(green("环境就绪") if ok else yellow("部分功能不可用，但不影响基本抓取"))
    print(dim("  配置下载地址: mh --set-out-dir ~/Downloads/media"))
    return 0 if ok else 1

# --------------------------------------------------------------------------
# 交互式选择
# --------------------------------------------------------------------------

def interactive_select(items: List[MediaItem]) -> List[MediaItem]:
    """让用户按类型/序号挑选要下载的资源。"""
    if not items:
        return []
    print()
    print(bold("请选择要下载的资源"))
    print(dim("  输入编号（如 1,3,5-9）、类型名（image/video/audio/hls）、all 全选、q 退出"))
    while True:
        try:
            raw = input(cyan("选择> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return []
        if not raw:
            continue
        if raw.lower() in {"q", "quit", "exit"}:
            return []
        if raw.lower() in {"all", "*", "a"}:
            return items

        # 类型名
        if "," not in raw and "-" not in raw and not raw.isdigit():
            wanted = {p.strip() for p in raw.split()}
            if wanted & {"image", "video", "audio", "hls", "dash"}:
                picked = [i for i in items if i.type.value in wanted]
            else:
                print(red("  无法识别，请重新输入"))
                continue
            if picked:
                return picked
            print(yellow("  没有匹配的资源"))
            continue

        # 编号 / 区间
        picked = []
        bad = []
        for chunk in raw.replace(" ", "").split(","):
            if not chunk:
                continue
            if "-" in chunk:
                try:
                    a, b = chunk.split("-", 1)
                    picked.extend(items[int(a) - 1: int(b)])
                except (ValueError, IndexError):
                    bad.append(chunk)
            else:
                try:
                    idx = int(chunk) - 1
                    if 0 <= idx < len(items):
                        picked.append(items[idx])
                    else:
                        bad.append(chunk)
                except ValueError:
                    bad.append(chunk)
        if bad:
            print(red(f"  无效输入: {', '.join(bad)}"))
            continue
        if picked:
            return picked
        print(yellow("  未选中任何资源"))


# --------------------------------------------------------------------------
# 配置（下载地址等）
# --------------------------------------------------------------------------

def do_init_config(explicit_config: str, *, force: bool = False) -> int:
    """生成带注释的配置文件模板。"""
    try:
        path = write_template(explicit_config, force=force)
    except ConfigError as exc:
        print(yellow(str(exc)))
        return 1

    print(green("✓ 已生成配置文件模板"))
    print(f"  文件: {cyan(path)}")
    print()
    print(dim("  用编辑器打开它，按注释修改需要的项即可。"))
    print(dim("  所有项都有默认值，只改你关心的那些（比如端口、保存目录）。"))
    print()
    print(f"  查看当前生效配置: {bold('mh --show-config')}")
    return 0


def do_set_out_dir(raw: str, explicit_config: str) -> int:
    """把下载目录写入配置文件。"""
    if not raw.strip():
        print(red("错误: 请提供目录，例如 --set-out-dir ~/Downloads/media"), file=sys.stderr)
        return 2
    if not toml_available():
        print(red("错误: 当前 Python 缺少 tomli，无法写入配置文件"), file=sys.stderr)
        print(dim("  安装: pip install tomli"))
        return 1

    target = parse_out_dir(raw)
    try:
        path = save_config({"download.out_dir": raw}, explicit_config)
    except ConfigError as exc:
        print(red(f"错误: {exc}"), file=sys.stderr)
        return 1

    print(green("✓ 已保存下载地址"))
    print(f"  下载目录: {cyan(target)}")
    print(f"  配置文件: {dim(path)}")
    print(dim("  命令行仍可用 -o 临时覆盖"))
    return 0


def do_unset_out_dir(explicit_config: str) -> int:
    """清除配置文件里的下载目录。"""
    cfg = load_config(explicit_config)
    if not cfg.exists:
        print(dim("没有配置文件，无需清除"))
        return 0
    if cfg.error:
        print(red(f"错误: {cfg.error}"), file=sys.stderr)
        return 1
    if cfg.get_str("download.out_dir", "") == "":
        print(dim(f"配置里本来就没有设置下载目录（{cfg.path}）"))
        return 0
    try:
        path = save_config({"download.out_dir": ""}, explicit_config)
    except ConfigError as exc:
        print(red(f"错误: {exc}"), file=sys.stderr)
        return 1
    print(green("✓ 已清除下载目录配置"))
    print(f"  配置文件: {dim(path)}")
    print(f"  下载目录: {cyan(os.path.abspath(DEFAULT_OUT_DIR))} （默认值）")
    return 0


def do_show_config(args: argparse.Namespace) -> int:
    """显示当前生效的配置与候选路径。"""
    print(bold("mediaharvest 配置"))
    print(describe_config(args.config, args.output))
    print()
    print(dim("候选配置文件（按优先级）:"))
    for idx, path in enumerate(config_candidates(args.config), 1):
        mark = green("●") if os.path.isfile(path) else dim("○")
        print(f"  {mark} {idx}. {path}")
    print()
    print(dim("提示:"))
    print(dim("  生成/重新生成模板 : mh --init-config [--force]"))
    print(dim("  只改下载目录      : mh --set-out-dir ~/Downloads/media"))
    print(dim(f"  环境变量临时覆盖  : {ENV_OUT_DIR}=/tmp/x mh <网址>"))
    return 0


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    if args.selfcheck:
        return selfcheck()
    if args.init_config:
        return do_init_config(args.config, force=args.force)
    if args.set_out_dir is not None:
        return do_set_out_dir(args.set_out_dir, args.config)
    if args.unset_out_dir:
        return do_unset_out_dir(args.config)
    if args.show_config:
        return do_show_config(args)

    if not args.url:
        print(red("错误: 请提供目标网址"), file=sys.stderr)
        print("  例: mh https://example.com -o downloads")
        print(f"  查看/生成配置: mh --show-config / mh --init-config")
        return 2

    # 只识别音乐 URL 就退出：排查「为什么这个链接抓不到」时最有用
    if args.music_info:
        print(bold(args.url))
        print("  " + music_mod.describe(args.url))
        target = music_mod.classify_music_url(args.url)
        if target.platform:
            print(dim(f"  platform={target.platform.key} kind={target.kind.value} "
                      f"track_id={target.track_id or '-'} album_id={target.album_id or '-'} "
                      f"playlist_id={target.playlist_id or '-'}"))
        return 0

    # 配置文件提供默认值，命令行参数优先
    cfg = load_config(args.config)
    if cfg.error:
        print(yellow(f"配置文件有问题: {cfg.error}"))
        print(dim(f"  {cfg.path}"))
    for warn in cfg.warnings[:5]:
        print(yellow(f"配置警告: {warn}"))

    def pick(cli_value: Any, key: str) -> Any:
        """命令行给了就用命令行的，否则取配置（含内置默认值）。"""
        return cli_value if cli_value is not None else cfg.get(key)

    types_raw = pick(args.types, "crawl.types") or "image,video"
    types = tuple(t.strip() for t in str(types_raw).split(",") if t.strip())
    valid = {"image", "video", "audio", "hls", "dash", "segment", "other"}
    unknown = set(types) - valid
    if unknown:
        print(red(f"错误: 未知类型 {', '.join(sorted(unknown))}"), file=sys.stderr)
        print(f"  可选: {', '.join(sorted(valid))}")
        return 2

    # 布尔开关：命令行只能「打开」，所以配置文件为 true 时也生效
    def flag(cli_value: Any, key: str) -> bool:
        return bool(cli_value) or bool(cfg.get(key))

    user_agent = pick(args.user_agent, "network.user_agent") or ""
    if args.cookies_from_browser or cfg.get("network.cookies_from_browser"):
        ensure_browser_path()

    # 下载目录：命令行 > 环境变量 > 配置文件 > 内置默认值
    out_dir = effective_out_dir(args.output, args.config)

    max_size_raw = pick(args.max_size, "download.max_size") or ""
    min_size_raw = pick(args.min_size, "download.min_size") or ""
    max_size = parse_size(max_size_raw) if max_size_raw else None
    min_size = parse_size(min_size_raw) if min_size_raw else None

    options = CrawlOptions(
        render=pick(args.render, "crawl.render") or "auto",
        use_ytdlp=pick(args.ytdlp, "crawl.use_ytdlp") or "auto",
        same_host_only=flag(args.same_host, "crawl.same_host_only"),
        max_depth=int(pick(args.depth, "crawl.depth") or 0),
        max_pages=max(1, int(pick(args.max_pages, "crawl.max_pages") or 1)),
        link_pattern=args.link_pattern,
        include_segments=bool(args.segments) or ("segment" in types),
        headless=not bool(args.show_browser) and bool(cfg.get("crawl.headless")),
        scroll=not bool(args.no_scroll) and bool(cfg.get("crawl.scroll")),
        browser_timeout=float(pick(args.browser_timeout, "crawl.browser_timeout") or 30.0),
        wait_after_load=float(pick(args.wait, "crawl.wait_after_load") or 1.5),
        timeout=float(pick(args.timeout, "crawl.timeout") or 20.0),
        retries=int(pick(args.retries, "crawl.retries") or 0),
        proxy=pick(args.proxy, "network.proxy") or "",
        cookie_string=pick(args.cookie, "network.cookie") or "",
        user_agent=user_agent,
        cookies_from_browser=pick(args.cookies_from_browser,
                                  "network.cookies_from_browser") or "",
        cookie_file=args.cookie_file,
        types=types,
        # 音乐：这些开关都是「命令行只能关，配置可以开」，因此用取反合并
        music=not (bool(args.no_music) or not bool(cfg.get("music.enabled"))),
        quality=str(pick(args.quality, "music.quality") or "best"),
        expand_playlists=not (bool(args.no_playlist)
                              or not bool(cfg.get("music.expand_playlists"))),
        playlist_limit=max(1, int(pick(args.playlist_limit, "music.playlist_limit") or 200)),
        music_lyrics=not (bool(args.no_lyrics) or not bool(cfg.get("music.lyrics"))),
        music_cover=not (bool(args.no_cover) or not bool(cfg.get("music.cover"))),
        music_tags=not (bool(args.no_tags) or not bool(cfg.get("music.tags"))),
    )

    quiet = args.quiet or args.json_out
    if not quiet:
        print(bold(f"目标: {args.url}"))
        print(dim(f"类型: {', '.join(types)}   输出: {out_dir}"))

    def on_progress(kind: str, message: str) -> None:
        if quiet:
            return
        icons = {"error": red("!"), "warn": yellow("!"), "done": green("✓")}
        print(f"  {icons.get(kind, dim('·'))} {message}")

    # --- 抓取 ---
    async with Crawler(options) as crawler:
        report = await crawler.crawl(args.url, progress=on_progress)

        if not report.items:
            print()
            print(yellow("未发现任何媒体资源"))
            if report.errors:
                for err in report.errors[:8]:
                    print(yellow(f"  - {err[:180]}"))
            print(dim("  建议: 试试 --render always 强制渲染，或 --ytdlp always"))
            return 1

        # --- 输出 JSON ---
        if args.json_out or args.json_file:
            payload = {
                "url": report.start_url,
                "title": report.title,
                "counts": report.count_by_type(),
                "items": [i.to_dict() for i in report.items],
                "errors": report.errors,
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            if args.json_file:
                with open(args.json_file, "w", encoding="utf-8") as fh:
                    fh.write(text)
                if not quiet:
                    print(dim(f"JSON 已写入 {args.json_file}"))
            if args.json_out:
                print(text)

        if not quiet:
            print_report(report, verbose=not args.json_out)

        if args.list:
            if quiet and not args.json_out:
                for idx, item in enumerate(report.items, 1):
                    print(f"{idx}\t{item.type.value}\t{item.size or ''}\t{item.url}")
            return 0

        # --- 选择资源 ---
        selected = report.items
        if args.interactive:
            for idx, item in enumerate(selected, 1):
                print(render_item_line(idx, item))
            selected = interactive_select(selected)
            if not selected:
                print(dim("已取消"))
                return 0
            print(dim(f"已选择 {len(selected)} 个资源"))

        # --- 下载 ---
        concurrency = int(pick(args.concurrency, "download.concurrency") or 8)
        hls_concurrency = int(pick(args.hls_concurrency, "download.hls_concurrency") or 8)
        downloader = Downloader(
            crawler.fetcher,
            out_dir=out_dir,
            concurrency=concurrency,
            overwrite=flag(args.overwrite, "download.overwrite"),
            max_size=max_size,
            min_size=min_size,
            hls_concurrency=hls_concurrency,
            max_segments=int(pick(args.max_segments, "advanced.max_segments") or 20000),
            remux=bool(cfg.get("advanced.remux")),
            flat=flag(args.flat, "download.flat"),
            keep_ts=flag(args.keep_ts, "download.keep_ts"),
            music_tags=options.music_tags,
            write_lyrics_file=flag(args.lrc_file, "music.lrc_file"),
        )

        if not quiet and selected and any(
            i.type in (MediaType.HLS, MediaType.DASH) for i in selected
        ):
            if downloader.has_remux or downloader.ffmpeg:
                backend = "ffmpeg" if downloader.ffmpeg else "内置转封装"
                print(dim(f"流媒体将使用{backend}输出 MP4"))
            else:
                print(yellow("未找到转封装后端，流媒体将以 .ts 保存（可直接播放）"))

        print()
        print(dim(f"开始下载 {len(selected)} 个资源（并发 {concurrency}）…"))
        results = await downloader.download_many(
            selected, progress=ProgressPrinter(len(selected), quiet=quiet)
        )

    if not quiet:
        print_summary(results, out_dir)
    else:
        ok = sum(1 for r in results if r.ok)
        failed = sum(1 for r in results if not r.ok and not r.skipped)
        print(f"完成: 成功 {ok} 失败 {failed} 目录 {out_dir}")

    return 0 if any(r.ok for r in results) else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print()
        print(yellow("已中断"))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
