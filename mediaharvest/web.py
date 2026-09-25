"""本地 Web 界面：粘贴网址 → 预览资源 → 勾选下载。

后端用 Flask 提供 JSON API，抓取与下载在后台线程的事件循环里执行，
前端轮询任务状态。所有状态保存在内存里，进程退出即清空。

启动方式（任选其一）::

    ./mh-web                       # shell 入口
    python -m mediaharvest.web     # 模块方式
    python web.py                  # 直接运行本文件（PyCharm 右键 Run 也可）
    python run_web.py              # 项目根目录入口

默认 http://127.0.0.1:8848
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 支持「直接运行本文件」（例如 PyCharm 右键 Run）：此时没有包上下文，
# 相对导入会失败，因此把项目根目录加入 sys.path 并补上包信息。
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "mediaharvest"

from flask import Flask, Response, jsonify, render_template_string, request, send_file, stream_with_context

from . import ytdlp as ytdlp_mod
from .browser import ensure_browser_path, playwright_available
from .config import (
    ConfigError,
    DEFAULT_OUT_DIR,
    describe_config,
    effective_out_dir,
    load_config,
    save_config,
)
from .control import CancelToken, Progress, ProgressReporter
from .crawler import CrawlOptions, Crawler
from .downloader import Downloader
from .hls import find_ffmpeg
from .models import DownloadResult, MediaItem, MediaType
from .session import HistoryEntry, HistoryStore, session_dir
from .utils import human_size, parse_size

# --------------------------------------------------------------------------
# 后台任务：在独立线程里跑 asyncio 事件循环
# --------------------------------------------------------------------------

@dataclass
class Job:
    """一次抓取/下载任务的状态。"""

    id: str
    kind: str                     # "crawl" | "download"
    status: str = "pending"       # pending | running | done | error | cancelled
    progress: str = ""
    message: str = ""
    error: str = ""
    created: float = field(default_factory=time.time)

    # 抓取结果
    url: str = ""
    title: str = ""
    items: List[Dict[str, Any]] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    # 下载结果
    results: List[Dict[str, Any]] = field(default_factory=list)
    total: int = 0
    done: int = 0
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    bytes_total: int = 0
    out_dir: str = ""

    #: 实时进度快照（由 ProgressReporter 更新）
    live: Dict[str, Any] = field(default_factory=dict)
    #: 历史记录条目 id（用于在历史面板里取消）
    history_id: str = ""
    #: 关联的取消令牌；由下载任务写入，取消接口从别的线程触发
    token: Any = None
    cancel: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "url": self.url,
            "title": self.title,
            "items": self.items,
            "counts": self.counts,
            "warnings": self.warnings[:20],
            "results": self.results,
            "total": self.total,
            "done": self.done,
            "ok": self.ok,
            "failed": self.failed,
            "skipped": self.skipped,
            "bytes_total": self.bytes_total,
            "bytes_human": human_size(self.bytes_total),
            "out_dir": self.out_dir,
            "dir_name": os.path.basename(self.out_dir.rstrip("/")) if self.out_dir else "",
            "live": self.live,
            "history_id": self.history_id,
            "cancellable": self.status in ("pending", "running"),
        }


class JobRunner:
    """在后台线程维护一个事件循环，串行执行任务。"""

    def __init__(self) -> None:
        self.jobs: Dict[str, Job] = {}
        #: 历史记录里 id → 取消令牌，供「历史面板取消」使用
        self.tokens: Dict[str, Any] = {}
        self.history = HistoryStore()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: "asyncio.Queue[Any]" = None  # type: ignore[assignment]

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._queue = asyncio.Queue()
        loop.run_until_complete(self._consume())

    async def _consume(self) -> None:
        while True:
            job, coro_factory = await self._queue.get()
            try:
                job.status = "running"
                await coro_factory(job)
                if job.status == "running":
                    job.status = "done"
            except Exception as exc:
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
            finally:
                # 任务结束后释放令牌引用，避免无限增长
                if job.history_id:
                    self.tokens.pop(job.history_id, None)
                if job.token is not None:
                    job.token = None
                self._queue.task_done()

    def submit(self, job: Job, coro_factory: Any) -> Job:
        with self._lock:
            self.jobs[job.id] = job
        if self._queue is None or self._loop is None:
            raise RuntimeError("任务队列尚未启动")
        asyncio.run_coroutine_threadsafe(self._queue.put((job, coro_factory)), self._loop)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        """取消一个任务。

        真正生效的是 ``token.cancel()``——它能让下载循环（包括正在传输的
        大文件）立刻停下，而不只是等下一个文件才开始跳过。
        """
        job = self.jobs.get(job_id)
        if not job:
            return False
        job.cancel = True
        if job.token is not None:
            job.token.cancel("用户取消")
        return True

    def cancel_history(self, history_id: str) -> bool:
        """从历史记录取消一个仍在进行的下载。"""
        token = self.tokens.get(history_id)
        if token is None:
            # 令牌可能已随任务结束释放；同时检查历史条目状态
            entry = self.history.get(history_id)
            return bool(entry and entry.status != "running" and False)
        token.cancel("用户从历史取消")
        return True

    def register_token(self, history_id: str, token: Any) -> None:
        with self._lock:
            self.tokens[history_id] = token

    def jobs_snapshot(self) -> List[Dict[str, Any]]:
        """所有任务的简要状态，供历史面板显示进行中的下载。"""
        with self._lock:
            jobs = list(self.jobs.values())
        out = []
        for job in jobs[-50:]:
            if job.kind != "download":
                continue
            out.append({
                "job_id": job.id,
                "history_id": job.history_id,
                "status": job.status,
                "title": job.title,
                "out_dir": job.out_dir,
                "live": job.live,
                "cancellable": job.status in ("pending", "running"),
            })
        return out


RUNNER = JobRunner()


# --------------------------------------------------------------------------
# 参数构建
# --------------------------------------------------------------------------

def config_dir() -> str:
    """本次进程启动时的配置目录（``--config`` / ``MEDIAHARVEST_CONFIG``）。

    由 :func:`serve` 写入，供各接口读取；为空则用默认查找顺序。
    """
    return _CONFIG_PATH


def build_options(data: Dict[str, Any]) -> CrawlOptions:
    """把前端传来的参数转成 CrawlOptions。"""
    types = data.get("types") or ["image", "video"]
    if isinstance(types, str):
        types = [t.strip() for t in types.split(",") if t.strip()]
    return CrawlOptions(
        render=data.get("render", "auto"),
        use_ytdlp=data.get("ytdlp", "auto"),
        same_host_only=bool(data.get("same_host")),
        max_depth=int(data.get("depth") or 0),
        max_pages=max(1, int(data.get("max_pages") or 1)),
        link_pattern=data.get("link_pattern", "") or "",
        include_segments=bool(data.get("segments")),
        headless=data.get("headless", True),
        scroll=data.get("scroll", True),
        wait_after_load=float(data.get("wait") or 1.5),
        browser_timeout=float(data.get("browser_timeout") or 30.0),
        timeout=float(data.get("timeout") or 20.0),
        retries=int(data.get("retries") or 2),
        proxy=data.get("proxy", "") or "",
        cookie_string=data.get("cookie", "") or "",
        user_agent=data.get("user_agent", "") or "",
        cookies_from_browser=data.get("cookies_from_browser", "") or "",
        cookie_file=data.get("cookie_file", "") or "",
        types=tuple(types),
        # 音乐：前端未提供时沿用配置默认值（enabled/lyrics/cover 默认开）
        music=bool(data.get("music", True)),
        quality=data.get("quality") or "best",
        expand_playlists=bool(data.get("expand_playlists", True)),
        playlist_limit=max(1, int(data.get("playlist_limit") or 200)),
        music_lyrics=bool(data.get("music_lyrics", True)),
        music_cover=bool(data.get("music_cover", True)),
        music_tags=bool(data.get("music_tags", True)),
    )


# --------------------------------------------------------------------------
# 任务实现
# --------------------------------------------------------------------------

async def crawl_task(job: Job, options: CrawlOptions) -> None:
    def progress(kind: str, message: str) -> None:
        job.progress = message
        if kind in ("error", "warn"):
            job.warnings.append(message)

    async with Crawler(options) as crawler:
        report = await crawler.crawl(job.url, progress=progress)
        job.title = report.title
        job.counts = report.count_by_type()
        job.items = [item.to_dict() for item in report.items]
        job.warnings.extend(report.errors)
        job.message = f"发现 {len(report.items)} 个资源"


async def download_task(
    job: Job,
    options: CrawlOptions,
    urls: List[Dict[str, Any]],
    out_dir: str,
    dl_opts: Dict[str, Any],
    title: str = "",
    page_url: str = "",
    use_session_dir: bool = True,
) -> None:
    """下载前端勾选的资源。

    每次下载都会创建一个**时间戳会话目录**，本次文件都放在里面；
    同时把这次下载登记到历史记录，并接上取消令牌与实时进度上报。
    """
    # 重建 MediaItem（前端回传的是 to_dict 的结果）
    wanted: List[MediaItem] = []
    for raw in urls:
        try:
            item = MediaItem.from_dict(raw)
        except Exception:
            continue
        wanted.append(item)

    if not wanted:
        job.status = "error"
        job.error = "未选择任何资源"
        return

    job.total = len(wanted)
    # 会话标题的取值优先级：显式标题 → 资源自带标题 → 网页域名 → 兜底
    fallback_title = ""
    if wanted and wanted[0].title:
        fallback_title = wanted[0].title
    elif page_url:
        try:
            from urllib.parse import urlsplit

            fallback_title = urlsplit(page_url).netloc or page_url
        except ValueError:
            fallback_title = page_url
    job.title = (title or fallback_title or "下载").strip()[:80]
    job.url = page_url

    # 时间戳会话目录：每次下载独立存放，互不覆盖
    base_dir = os.path.abspath(out_dir)
    if use_session_dir:
        target_dir = session_dir(base_dir, job.title)
    else:
        target_dir = base_dir
        os.makedirs(target_dir, exist_ok=True)
    job.out_dir = target_dir

    # 登记历史
    entry = HistoryEntry(
        id=job.id,
        created=time.time(),
        title=job.title,
        page_url=page_url,
        out_dir=target_dir,
        status="running",
        total=len(wanted),
    )
    job.history_id = entry.id
    RUNNER.history.add(entry)

    # 取消令牌 + 进度上报
    token = CancelToken()
    job.token = token
    RUNNER.register_token(entry.id, token)

    def on_progress(p: Progress) -> None:
        job.live = p.to_dict()
        job.progress = f"{p.files_done}/{p.total_files}"
        # 把进行中的状态写回历史，历史面板据此显示进度
        RUNNER.history.update(
            entry.id,
            ok=p.ok, failed=p.failed, skipped=p.skipped,
            bytes_total=int(p.bytes_finished),
        )

    reporter = ProgressReporter(on_progress, interval=0.2)

    async with Crawler(options) as crawler:
        downloader = Downloader(
            crawler.fetcher,
            out_dir=target_dir,
            concurrency=int(dl_opts.get("concurrency") or 8),
            overwrite=bool(dl_opts.get("overwrite")),
            max_size=parse_size(dl_opts["max_size"]) if dl_opts.get("max_size") else None,
            min_size=parse_size(dl_opts["min_size"]) if dl_opts.get("min_size") else None,
            hls_concurrency=int(dl_opts.get("hls_concurrency") or 8),
            flat=bool(dl_opts.get("flat")),
            music_tags=bool(options.music_tags),
            write_lyrics_file=bool(dl_opts.get("lrc_file")),
            token=token,
            reporter=reporter,
        )

        def on_result(result: DownloadResult) -> None:
            # 累加统计：进度条与历史都依赖这几个计数
            if result.ok:
                job.ok += 1
                job.bytes_total += result.size
            elif result.skipped:
                job.skipped += 1
            else:
                job.failed += 1
            job.results.append(result.to_dict())
            RUNNER.history.append_file(entry.id, result.to_dict())

        await downloader.download_many(
            wanted, progress=on_result, should_stop=lambda: job.cancel
        )

    # 收尾：写回历史最终状态
    if token.cancelled:
        job.status = "cancelled"
        job.message = f"已取消（完成 {job.ok} 项）"
    else:
        job.message = f"成功 {job.ok}，失败 {job.failed}，跳过 {job.skipped}"

    RUNNER.history.update(
        entry.id,
        status=job.status if job.status != "running" else "done",
        ok=job.ok, failed=job.failed, skipped=job.skipped,
        bytes_total=job.bytes_total,
        message=job.message,
        finished=time.time(),
        out_dir=target_dir,
    )
    job.live = {}  # 结束后不再显示实时进度


# --------------------------------------------------------------------------
# Flask 应用
# --------------------------------------------------------------------------

#: 进程级配置路径（由 serve() 写入；空串表示按默认顺序查找）
_CONFIG_PATH = ""

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False


@app.after_request
def _no_cache(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/")
def index() -> str:
    return render_template_string(PAGE_HTML, version=_version())


@app.route("/assets/logo.png")
def app_logo() -> Response:
    return send_file(os.path.join(os.path.dirname(__file__), "static", "logo.png"), mimetype="image/png")


@app.route("/favicon.ico")
def favicon() -> Response:
    return app_logo()


@app.route("/api/status")
def api_status() -> Response:
    avail, reason = playwright_available()
    ffmpeg = find_ffmpeg()
    try:
        from .remux import ts_to_mp4  # noqa: F401

        has_remux = True
    except ImportError:
        has_remux = False
    try:
        from Crypto.Cipher import AES  # noqa: F401

        has_aes = True
    except ImportError:
        has_aes = False

    cfg = load_config(config_dir())
    return jsonify({
        "version": _version(),
        "render": avail,
        "render_reason": reason,
        "ytdlp": ytdlp_mod.ytdlp_available(),
        "ffmpeg": bool(ffmpeg),
        "ffmpeg_path": ffmpeg,
        "remux": has_remux,
        "aes": has_aes,
        "default_out": effective_out_dir(explicit_config=config_dir()),
        "config_path": cfg.path,
        "config_exists": cfg.exists,
        "config_out_dir": cfg.get_str("download.out_dir", ""),
        "config_error": cfg.error,
    })


@app.route("/api/config", methods=["GET"])
def api_config_get() -> Response:
    """查看当前配置（含候选路径与优先级说明）。"""
    cfg = load_config(config_dir())
    payload = cfg.to_dict()
    payload["effective_out_dir"] = effective_out_dir(explicit_config=config_dir())
    payload["default_out_dir"] = os.path.abspath(DEFAULT_OUT_DIR)
    payload["summary"] = describe_config(config_dir())
    return jsonify(payload)


@app.route("/api/config/out-dir", methods=["POST"])
def api_config_set_out_dir() -> Response:
    """把下载目录写入配置文件（Web 界面的「存为默认」）。"""
    data = request.get_json(force=True, silent=True) or {}

    if data.get("reset"):
        try:
            path = save_config({"download.out_dir": ""}, config_dir())
        except ConfigError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({
            "ok": True,
            "path": path,
            "out_dir": os.path.abspath(DEFAULT_OUT_DIR),
            "message": "已恢复默认下载目录",
        })

    raw = (data.get("out_dir") or "").strip()
    if not raw:
        return jsonify({"error": "请填写保存目录"}), 400

    target = effective_out_dir(raw, config_dir())
    try:
        path = save_config({"download.out_dir": raw}, config_dir())
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "path": path, "out_dir": target,
                    "message": "已存为默认下载目录"})


@app.route("/api/crawl", methods=["POST"])
def api_crawl() -> Response:
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "请填写网址"}), 400
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url

    options = build_options(data)
    job = Job(id=uuid.uuid4().hex[:12], kind="crawl", url=url)
    RUNNER.submit(job, lambda j: crawl_task(j, options))
    return jsonify({"job": job.id})


@app.route("/api/download", methods=["POST"])
def api_download() -> Response:
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items") or []
    if not items:
        return jsonify({"error": "未选择任何资源"}), 400

    options = build_options(data)
    # 前端留空时回落到配置里的下载地址
    out_dir = effective_out_dir((data.get("out_dir") or "").strip(), config_dir())
    dl_opts = data.get("download") or {}
    job = Job(id=uuid.uuid4().hex[:12], kind="download", url=data.get("url", ""))
    RUNNER.submit(
        job,
        lambda j: download_task(
            j, options, items, out_dir, dl_opts,
            title=(data.get("title") or "").strip(),
            page_url=(data.get("url") or "").strip(),
            use_session_dir=bool(data.get("session_dir", True)),
        ),
    )
    return jsonify({"job": job.id, "out_dir": out_dir})


@app.route("/api/history")
def api_history() -> Response:
    """下载历史列表（含仍在进行的会话）。"""
    entries = [e.to_dict() for e in RUNNER.history.list(limit=100)]
    # 把进行中任务的实时进度合并进来，历史面板才能显示进度条与取消按钮
    live = {j["history_id"]: j for j in RUNNER.jobs_snapshot() if j.get("history_id")}
    for entry in entries:
        active = live.get(entry["id"])
        if active:
            entry["live"] = active.get("live") or {}
            entry["running"] = True
            entry["cancellable"] = active.get("cancellable", False)
            entry["job_id"] = active.get("job_id")
        else:
            entry["cancellable"] = False
    return jsonify({"entries": entries, "base_dir": effective_out_dir(explicit_config=config_dir())})


@app.route("/api/history/<entry_id>/cancel", methods=["POST"])
def api_history_cancel(entry_id: str) -> Response:
    """从历史面板取消一个仍在进行的下载。"""
    # 先按历史 id 找令牌
    if RUNNER.cancel_history(entry_id):
        return jsonify({"ok": True, "message": "已发送取消请求"})
    # 兜底：按 job id 找
    if RUNNER.cancel(entry_id):
        return jsonify({"ok": True, "message": "已发送取消请求"})
    return jsonify({"error": "该任务已结束或不存在"}), 404


@app.route("/api/history/<entry_id>", methods=["DELETE"])
def api_history_delete(entry_id: str) -> Response:
    """删除一条历史记录（不删除已下载的文件）。"""
    ok = RUNNER.history.remove(entry_id)
    return jsonify({"ok": ok})


@app.route("/api/history", methods=["DELETE"])
def api_history_clear() -> Response:
    """清空历史记录（不删除已下载的文件）。"""
    count = RUNNER.history.clear()
    return jsonify({"ok": True, "removed": count})


@app.route("/api/history/<entry_id>/open", methods=["POST"])
def api_history_open(entry_id: str) -> Response:
    """在 Finder 里打开该次下载的目录。"""
    entry = RUNNER.history.get(entry_id)
    if not entry or not entry.out_dir:
        return jsonify({"error": "记录不存在"}), 404
    if not os.path.isdir(entry.out_dir):
        return jsonify({"error": f"目录不存在: {entry.out_dir}"}), 404
    try:
        _open_in_file_manager(entry.out_dir)
    except Exception as exc:
        return jsonify({"error": f"打开失败: {exc}"}), 500
    return jsonify({"ok": True, "path": entry.out_dir})


@app.route("/api/history/<entry_id>/file/<int:file_index>")
def api_history_file(entry_id: str, file_index: int) -> Response:
    """预览历史记录里的本地媒体文件。

    只允许访问该历史条目 out_dir 下、下载成功且仍存在的普通文件，避免把
    任意本机路径暴露给浏览器。``conditional=True`` 让 Flask 自动处理
    Range 请求，视频 / 音频可以边下边播和拖动进度。
    """
    entry = RUNNER.history.get(entry_id)
    if not entry or not entry.out_dir:
        return jsonify({"error": "记录不存在"}), 404
    if file_index < 0 or file_index >= len(entry.files):
        return jsonify({"error": "文件不存在"}), 404
    info = entry.files[file_index] or {}
    if not info.get("ok"):
        return jsonify({"error": "文件未下载成功"}), 404

    root = os.path.realpath(entry.out_dir)
    path = os.path.realpath(info.get("path") or "")
    if not path or not os.path.isfile(path):
        return jsonify({"error": "文件不存在"}), 404
    try:
        common = os.path.commonpath([root, path])
    except ValueError:
        return jsonify({"error": "非法文件路径"}), 403
    if common != root:
        return jsonify({"error": "非法文件路径"}), 403

    mimetype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return send_file(path, mimetype=mimetype, conditional=True)


def _open_in_file_manager(path: str) -> None:
    """在系统文件管理器里打开目录（跨平台，失败不影响主流程）。"""
    import subprocess
    import sys as _sys

    if _sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif _sys.platform.startswith("win"):  # pragma: no cover
        os.startfile(path)  # type: ignore[attr-defined]
    else:  # pragma: no cover
        subprocess.Popen(["xdg-open", path])


@app.route("/api/job/<job_id>")
def api_job(job_id: str) -> Response:
    job = RUNNER.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(job.to_dict())


@app.route("/api/job/<job_id>/cancel", methods=["POST"])
def api_cancel(job_id: str) -> Response:
    ok = RUNNER.cancel(job_id)
    return jsonify({"ok": ok})


@app.route("/api/proxy")
def api_proxy() -> Response:
    """图片代理：绕过防盗链在页面里预览媒体。

    只允许 http/https。SSRF 防护分两层：

    1. 主机名若是**字面 IP**，必须是公网地址，否则拒绝。
    2. 主机名若是域名，解析后拒绝指向**环回/链路本地**的地址
       （防 DNS rebinding）。

    注意：不能简单地拒绝所有「私有网段」解析结果——部分企业网络或
    透明代理环境（如 198.18.0.0/15）会把公网域名解析到这类网段，
    一律拦截会导致代理完全不可用。
    """
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    target = request.args.get("url", "")
    if not target.lower().startswith(("http://", "https://")):
        return jsonify({"error": "非法地址"}), 400

    host = urlsplit(target).hostname or ""
    if not host:
        return jsonify({"error": "非法地址"}), 400

    def _normalize(ip: "ipaddress._BaseAddress") -> "ipaddress._BaseAddress":
        """把 IPv4-mapped IPv6（::ffff:1.2.3.4）还原成 IPv4。"""
        mapped = getattr(ip, "ipv4_mapped", None)
        return mapped if mapped is not None else ip

    # 第 1 层：字面 IP 必须是公网
    try:
        literal = ipaddress.ip_address(host)
        if not _normalize(literal).is_global:
            return jsonify({"error": "禁止访问内网地址"}), 403
    except ValueError:
        pass  # 是域名，走第 2 层

    # 第 2 层：域名解析结果不得是环回/链路本地
    try:
        for info in socket.getaddrinfo(host, None):
            ip = _normalize(ipaddress.ip_address(info[4][0]))
            if ip.is_loopback or ip.is_link_local:
                return jsonify({"error": "禁止访问内网地址"}), 403
    except (socket.gaierror, ValueError):
        return jsonify({"error": "域名解析失败"}), 400

    referer = request.args.get("referer", "") or target

    import httpx

    try:
        from .fetcher import DEFAULT_UA

        req_headers = {
            "User-Agent": DEFAULT_UA,
            "Referer": referer,
            "Accept": request.headers.get("Accept", "*/*"),
        }
        if request.headers.get("Range"):
            req_headers["Range"] = request.headers["Range"]

        client = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            max_redirects=10,
        )
        req = client.build_request("GET", target, headers=req_headers)
        upstream = client.send(req, stream=True)
    except Exception as exc:
        return jsonify({"error": f"拉取失败: {exc}"}), 502

    if upstream.status_code >= 400:
        status = upstream.status_code
        upstream.close()
        client.close()
        return jsonify({"error": f"远端返回 {status}"}), 502

    headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() in {"content-length", "content-range", "accept-ranges"}
    }
    ctype = upstream.headers.get("content-type", "") or "application/octet-stream"

    def generate() -> Any:
        try:
            for chunk in upstream.iter_raw(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()
            client.close()

    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        mimetype=ctype,
        headers=headers,
        direct_passthrough=True,
    )


def _version() -> str:
    from . import __version__

    return __version__


def serve(host: str = "127.0.0.1", port: int = 8848, open_browser: bool = True,
          config_path: str = "") -> None:
    """启动 Web 服务。"""
    global _CONFIG_PATH
    _CONFIG_PATH = config_path
    ensure_browser_path()
    RUNNER.start()
    url = f"http://{host}:{port}"
    print(f"\n  mediaharvest Web 界面已启动")
    print(f"  → {url}")
    print(f"  下载目录: {effective_out_dir(explicit_config=config_path)}")
    print(f"  配置文件: {load_config(config_path).path}")
    print("  按 Ctrl+C 退出\n")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)


# --------------------------------------------------------------------------
# 前端页面（单文件，无外部依赖）
# --------------------------------------------------------------------------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>mediaharvest · 网页媒体抓取</title>
<link rel="icon" type="image/png" href="/assets/logo.png?v=20260924">
<link rel="shortcut icon" href="/favicon.ico?v=20260924">
<style>
/* ==========================================================================
   mediaharvest · 设计系统
   深色为主、玻璃质感、极光背景点缀；所有颜色走 token，便于整体换肤。
   ========================================================================== */
:root{
  --bg:#070a12;
  --bg-2:#0a0f1c;
  --surface:rgba(18,24,40,.72);
  --surface-2:rgba(255,255,255,.045);
  --surface-3:rgba(255,255,255,.075);
  --fg:#e9eef8;
  --fg-2:#a7b3c9;
  --fg-3:#6d7a92;
  --stroke:rgba(255,255,255,.085);
  --stroke-2:rgba(255,255,255,.16);
  --brand:#6ea8fe;
  --brand-2:#a78bfa;
  --brand-soft:rgba(110,168,254,.20);
  --ok:#4ade80;
  --ok-soft:rgba(74,222,128,.16);
  --warn:#fbbf24;
  --warn-soft:rgba(251,191,36,.16);
  --bad:#fb7185;
  --bad-soft:rgba(251,113,133,.16);
  --blob-a:rgba(110,168,254,.20);
  --blob-b:rgba(167,139,250,.16);
  --blob-c:rgba(45,212,191,.10);
  --grid-line:rgba(255,255,255,.028);
  --shadow:0 1px 0 rgba(255,255,255,.05) inset,0 24px 48px -30px rgba(0,0,0,.95);
  --shadow-lg:0 32px 70px -34px rgba(0,0,0,.95);
  --r-sm:10px; --r-md:14px; --r-lg:20px;
  --ease:cubic-bezier(.22,.61,.36,1);
}
html[data-theme="light"]{
  --bg:#f4f6fb;
  --bg-2:#e9edf6;
  --surface:rgba(255,255,255,.80);
  --surface-2:rgba(15,23,42,.035);
  --surface-3:rgba(15,23,42,.06);
  --fg:#0f172a;
  --fg-2:#4d5c75;
  --fg-3:#7b8798;
  --stroke:rgba(15,23,42,.10);
  --stroke-2:rgba(15,23,42,.20);
  --brand:#3b6ef6;
  --brand-2:#7c5cf7;
  --brand-soft:rgba(59,110,246,.14);
  --ok:#15803d;
  --ok-soft:rgba(21,128,61,.12);
  --warn:#a16207;
  --warn-soft:rgba(161,98,7,.12);
  --bad:#dc2626;
  --bad-soft:rgba(220,38,38,.10);
  --blob-a:rgba(59,110,246,.16);
  --blob-b:rgba(124,92,247,.14);
  --blob-c:rgba(13,148,136,.10);
  --grid-line:rgba(15,23,42,.035);
  --shadow:0 1px 2px rgba(15,23,42,.06),0 22px 44px -30px rgba(15,23,42,.35);
  --shadow-lg:0 30px 60px -30px rgba(15,23,42,.30);
}
*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"SF Pro SC","Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
  font-feature-settings:"tnum" 1,"cv01" 1;
}
/* 背景：极光光斑 + 细网格 */
.aurora{position:fixed;inset:0;z-index:-1;overflow:hidden;background:var(--bg)}
.aurora::before{
  content:"";position:absolute;inset:-30% -10% auto -10%;height:120%;
  background:
    radial-gradient(48% 42% at 18% 12%,var(--blob-a),transparent 62%),
    radial-gradient(42% 40% at 82% 6%,var(--blob-b),transparent 64%),
    radial-gradient(46% 44% at 60% 88%,var(--blob-c),transparent 66%);
  filter:blur(6px);
}
.aurora::after{
  content:"";position:absolute;inset:0;opacity:.6;
  background-image:linear-gradient(var(--grid-line) 1px,transparent 1px),
                   linear-gradient(90deg,var(--grid-line) 1px,transparent 1px);
  background-size:52px 52px;
  mask-image:radial-gradient(80% 60% at 50% 0%,#000 20%,transparent 78%);
  -webkit-mask-image:radial-gradient(80% 60% at 50% 0%,#000 20%,transparent 78%);
}
::selection{background:var(--brand-soft);color:var(--fg)}
:focus-visible{outline:2px solid var(--brand);outline-offset:2px;border-radius:6px}
::-webkit-scrollbar{width:11px;height:11px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--surface-3);border:3px solid transparent;
  background-clip:content-box;border-radius:8px}
::-webkit-scrollbar-thumb:hover{background:var(--stroke-2);background-clip:content-box}

/* ---------- 顶栏 ---------- */
.topbar{
  position:sticky;top:0;z-index:40;display:flex;align-items:center;gap:16px;flex-wrap:wrap;
  padding:13px 26px;border-bottom:1px solid var(--stroke);
  background:var(--surface);backdrop-filter:blur(18px) saturate(160%);
  -webkit-backdrop-filter:blur(18px) saturate(160%);
}
.brand{display:flex;align-items:center;gap:11px;min-width:0}
.logo{
  width:42px;height:34px;border-radius:11px;display:grid;place-items:center;overflow:hidden;
  background:#000;border:1px solid rgba(255,255,255,.18);
  box-shadow:0 10px 22px -12px rgba(255,255,255,.35),0 1px 0 rgba(255,255,255,.24) inset;
}
.logo img{width:100%;height:100%;object-fit:contain;display:block}
.brand-text{min-width:0}
.brand h1{font-size:15.5px;margin:0;font-weight:650;letter-spacing:-.015em;line-height:1.25}
.brand h1 span{background:linear-gradient(100deg,var(--brand),var(--brand-2));
  -webkit-background-clip:text;background-clip:text;color:transparent}
.brand p{margin:0;font-size:11px;color:var(--fg-3);letter-spacing:.01em}
.status-strip{display:flex;gap:7px;flex-wrap:wrap}
.badge{
  display:inline-flex;align-items:center;gap:6px;font-size:11.5px;font-weight:500;
  padding:4px 11px 4px 9px;border-radius:999px;background:var(--surface-2);
  border:1px solid var(--stroke);color:var(--fg-2);white-space:nowrap;
  transition:border-color .2s var(--ease),color .2s var(--ease)
}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--fg-3);
  box-shadow:0 0 0 3px transparent;transition:.2s var(--ease)}
.badge.on{color:var(--fg)}
.badge.on::before{background:var(--ok);box-shadow:0 0 0 3px var(--ok-soft)}
.badge.warn{color:var(--warn)}
.badge.warn::before{background:var(--warn);box-shadow:0 0 0 3px var(--warn-soft)}
.badge.off{color:var(--fg-3)}
.badge.off::before{background:var(--fg-3);opacity:.5}
.top-actions{margin-left:auto;display:flex;align-items:center;gap:9px}
.version{font-size:11px;color:var(--fg-3);font-variant-numeric:tabular-nums;
  padding:4px 9px;border-radius:999px;border:1px solid var(--stroke);background:var(--surface-2)}
.icon-btn{
  width:34px;height:34px;padding:0;display:grid;place-items:center;border-radius:11px;
  border:1px solid var(--stroke);background:var(--surface-2);color:var(--fg-2);
  cursor:pointer;transition:.2s var(--ease)
}
.icon-btn:hover{color:var(--fg);border-color:var(--stroke-2);background:var(--surface-3)}
.icon-btn svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.9;
  stroke-linecap:round;stroke-linejoin:round}

/* ---------- 布局 ---------- */
main{max-width:1240px;margin:0 auto;padding:26px 26px 140px}
.card{
  background:var(--surface);border:1px solid var(--stroke);border-radius:var(--r-lg);
  padding:22px;margin-bottom:18px;backdrop-filter:blur(16px) saturate(150%);
  -webkit-backdrop-filter:blur(16px) saturate(150%);box-shadow:var(--shadow);
}
.card-title{font-size:13px;font-weight:650;letter-spacing:-.01em;margin:0 0 3px}
.muted{color:var(--fg-3);font-size:12px;margin:0}
label{display:block;font-size:11.5px;color:var(--fg-3);margin-bottom:6px;
  font-weight:500;letter-spacing:.01em}

/* ---------- 表单 ---------- */
input[type=text],input[type=number],select{
  width:100%;padding:9px 12px;border-radius:var(--r-sm);border:1px solid var(--stroke);
  background:var(--bg-2);color:var(--fg);font-size:13px;font-family:inherit;
  transition:border-color .18s var(--ease),box-shadow .18s var(--ease),background .18s
}
input::placeholder{color:var(--fg-3)}
input:hover,select:hover{border-color:var(--stroke-2)}
input:focus,select:focus{outline:none;border-color:var(--brand);
  box-shadow:0 0 0 4px var(--brand-soft);background:var(--bg)}
select{appearance:none;cursor:pointer;padding-right:32px;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%236d7a92' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 10px center;background-size:15px}

/* ---------- 按钮 ---------- */
button{
  display:inline-flex;align-items:center;justify-content:center;gap:7px;
  padding:9px 16px;border-radius:var(--r-sm);border:1px solid var(--stroke);
  background:var(--surface-2);color:var(--fg);font-size:13px;font-weight:500;
  cursor:pointer;font-family:inherit;white-space:nowrap;
  transition:transform .16s var(--ease),border-color .18s var(--ease),
             background .18s var(--ease),box-shadow .18s var(--ease),opacity .18s
}
button:hover:not(:disabled){border-color:var(--stroke-2);background:var(--surface-3)}
button:active:not(:disabled){transform:translateY(1px) scale(.99)}
button:disabled{opacity:.42;cursor:not-allowed}
button.primary{
  border-color:transparent;color:#fff;font-weight:600;
  background:linear-gradient(135deg,var(--brand),var(--brand-2));
  box-shadow:0 12px 26px -14px var(--brand),0 1px 0 rgba(255,255,255,.28) inset
}
button.primary:hover:not(:disabled){filter:brightness(1.07);
  box-shadow:0 16px 32px -14px var(--brand),0 1px 0 rgba(255,255,255,.28) inset}
button.ghost{background:transparent}
button.danger{color:var(--bad);border-color:var(--bad-soft);background:var(--bad-soft)}
button.danger:hover:not(:disabled){border-color:var(--bad);background:var(--bad-soft)}
button.sm{padding:6px 12px;font-size:12px;border-radius:9px}
button.lg{padding:11px 22px;font-size:13.5px;border-radius:12px}
button .i{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round;flex-shrink:0}
.hint{font-size:11.5px;color:var(--fg-3)}

/* ---------- 标签页 ---------- */
.tabs{display:inline-flex;gap:4px;margin-bottom:20px;padding:4px;border-radius:13px;
  border:1px solid var(--stroke);background:var(--surface-2);backdrop-filter:blur(14px);
  -webkit-backdrop-filter:blur(14px)}
.tab{padding:7px 18px;cursor:pointer;font-size:13px;border-radius:10px;color:var(--fg-3);
  transition:.2s var(--ease);user-select:none;font-weight:500}
.tab:hover{color:var(--fg-2)}
.tab.active{color:var(--fg);background:var(--surface-3);
  box-shadow:var(--shadow);font-weight:600}
.tab .cnt{font-size:10.5px;opacity:.75;margin-left:5px;font-variant-numeric:tabular-nums}

/* ---------- 命令区（首屏） ---------- */
.hero{padding:24px}
.cmd{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.cmd-field{
  position:relative;flex:1 1 340px;min-width:260px;display:flex;align-items:center;
  border:1px solid var(--stroke);border-radius:var(--r-md);background:var(--bg-2);
  transition:border-color .18s var(--ease),box-shadow .18s var(--ease),background .18s
}
.cmd-field:focus-within{border-color:var(--brand);background:var(--bg);
  box-shadow:0 0 0 4px var(--brand-soft)}
.cmd-field>svg{width:17px;height:17px;margin-left:13px;flex-shrink:0;fill:none;
  stroke:var(--fg-3);stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
.cmd-field input{border:0!important;background:transparent!important;box-shadow:none!important;
  padding:13px 12px;font-size:14px}
.cmd-field kbd{font:11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--fg-3);
  border:1px solid var(--stroke);border-bottom-width:2px;border-radius:6px;
  padding:4px 7px;margin-right:10px;background:var(--surface-2)}
.hero-foot{display:flex;justify-content:space-between;align-items:center;gap:12px;
  flex-wrap:wrap;margin-top:12px}
.hero-actions{display:flex;gap:8px;align-items:center}

/* ---------- 高级选项 ---------- */
details.adv{margin-top:16px;border-top:1px solid var(--stroke);padding-top:14px}
summary{cursor:pointer;font-size:12px;color:var(--fg-3);user-select:none;
  list-style:none;display:flex;align-items:center;gap:7px;width:fit-content;
  padding:4px 0;transition:color .18s var(--ease)}
summary::-webkit-details-marker{display:none}
summary:hover{color:var(--fg)}
summary::before{content:"";width:13px;height:13px;flex-shrink:0;
  background:currentColor;opacity:.85;
  mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m9 6 6 6-6 6'/%3E%3C/svg%3E") center/contain no-repeat;
  -webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m9 6 6 6-6 6'/%3E%3C/svg%3E") center/contain no-repeat;
  transition:transform .2s var(--ease)}
details[open]>summary::before{transform:rotate(90deg)}
.adv-body{margin-top:16px;display:grid;gap:16px}
.adv-group{border:1px solid var(--stroke);border-radius:var(--r-md);
  background:var(--surface-2);padding:16px}
.adv-group>h3{margin:0 0 13px;font-size:11.5px;font-weight:650;letter-spacing:.04em;
  text-transform:uppercase;color:var(--fg-3);display:flex;align-items:center;gap:8px}
.adv-group>h3::after{content:"";flex:1;height:1px;background:var(--stroke)}
.grid{display:grid;gap:12px}
.g-auto{grid-template-columns:repeat(auto-fit,minmax(168px,1fr))}
.field-wide{grid-column:span 2}
@media(max-width:640px){.field-wide{grid-column:span 1}}
.inline-actions{display:flex;gap:7px;align-items:center;margin-top:7px;flex-wrap:wrap}

/* ---------- 结果区 ---------- */
.res-head{display:flex;justify-content:space-between;align-items:flex-start;
  gap:14px;flex-wrap:wrap}
.res-title{flex:1;min-width:220px;min-width:0}
#page-title{font-size:16px;font-weight:650;letter-spacing:-.015em;margin:0 0 4px;
  line-height:1.35;word-break:break-word}
#page-url{font-size:11.5px;color:var(--fg-3);text-decoration:none;word-break:break-all;
  display:inline-block;max-width:100%}
#page-url:hover{color:var(--brand)}
.res-tools{display:flex;gap:8px;flex-wrap:wrap}

.chips{display:flex;gap:7px;flex-wrap:wrap;margin:16px 0 14px}
.chip{display:inline-flex;align-items:center;gap:7px;padding:6px 13px;border-radius:999px;
  border:1px solid var(--stroke);background:var(--surface-2);font-size:12px;
  cursor:pointer;user-select:none;color:var(--fg-2);font-weight:500;
  transition:.18s var(--ease)}
.chip:hover{border-color:var(--stroke-2);color:var(--fg);transform:translateY(-1px)}
.chip.active{color:var(--fg);border-color:var(--brand);background:var(--brand-soft);
  box-shadow:0 6px 16px -12px var(--brand)}
.chip .n{font-size:10.5px;opacity:.7;font-variant-numeric:tabular-nums}
.chip::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--fg-3);
  transition:.18s var(--ease)}
.chip[data-f="all"]::before{background:linear-gradient(140deg,var(--brand),var(--brand-2))}
.chip[data-f="image"]::before{background:#6ea8fe}
.chip[data-f="video"]::before{background:#a78bfa}
.chip[data-f="audio"]::before{background:#fbbf24}
.chip[data-f="hls"]::before{background:#f472b6}
.chip[data-f="dash"]::before{background:#f472b6}
.chip[data-f="segment"]::before{background:#64748b}
.chip[data-f="other"]::before{background:#64748b}

.stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.stat{flex:0 1 auto;min-width:104px;padding:11px 15px;border-radius:var(--r-md);
  border:1px solid var(--stroke);background:var(--surface-2)}
.stat b{display:block;font-size:19px;font-weight:650;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums;line-height:1.25}
.stat span{font-size:11px;color:var(--fg-3)}

.media-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(184px,1fr));gap:14px}
.tile{position:relative;border:1px solid var(--stroke);border-radius:var(--r-md);
  background:var(--surface-2);overflow:hidden;cursor:pointer;
  transition:transform .2s var(--ease),border-color .2s var(--ease),box-shadow .2s var(--ease)}
.tile:hover{transform:translateY(-3px);border-color:var(--stroke-2);
  box-shadow:var(--shadow-lg)}
.tile.sel{border-color:var(--brand);box-shadow:0 0 0 3px var(--brand-soft),var(--shadow)}
.thumbwrap{position:relative;aspect-ratio:16/10;overflow:hidden;background:
  linear-gradient(140deg,rgba(255,255,255,.05),transparent)}
.tile .thumb{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;
  display:block;transition:transform .35s var(--ease),opacity .3s}
.tile:hover .thumb{transform:scale(1.05)}
.tile .ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  color:var(--fg-3);font-size:26px;opacity:.85}
.thumbwrap::after{content:"";position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(to top,rgba(0,0,0,.42),transparent 55%);opacity:0;
  transition:opacity .25s var(--ease)}
.tile:hover .thumbwrap::after{opacity:1}
.tile .meta{padding:10px 12px 11px}
.tile .nm{font-size:12px;font-weight:500;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;margin-bottom:5px}
.tile .sub{font-size:10.5px;color:var(--fg-3);display:flex;justify-content:space-between;
  gap:8px;font-variant-numeric:tabular-nums}
.tile .sub span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tick{position:absolute;top:9px;left:9px;width:21px;height:21px;border-radius:7px;z-index:2;
  border:1.5px solid rgba(255,255,255,.75);background:rgba(0,0,0,.42);
  backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);
  display:flex;align-items:center;justify-content:center;font-size:12px;color:#fff;
  transition:.18s var(--ease);transform:scale(.92)}
.tile:hover .tick{transform:scale(1)}
.tile.sel .tick{background:linear-gradient(135deg,var(--brand),var(--brand-2));
  border-color:transparent;transform:scale(1);
  box-shadow:0 6px 14px -8px var(--brand)}
.tag{position:absolute;top:9px;right:9px;z-index:2;font-size:10px;padding:3px 9px;
  border-radius:999px;background:rgba(0,0,0,.5);color:#fff;font-weight:600;
  backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);
  border:1px solid rgba(255,255,255,.16);letter-spacing:.02em}
.tag.video{background:rgba(124,58,237,.72)}
.tag.hls,.tag.dash{background:rgba(219,39,119,.72)}
.tag.audio{background:rgba(180,83,9,.72)}
.tag.image{background:rgba(29,78,216,.72)}
.tag.segment,.tag.other{background:rgba(51,65,85,.72)}
.tile .ext,.tile .prev{position:absolute;bottom:8px;z-index:2;width:26px;height:26px;padding:0;
  display:grid;place-items:center;border-radius:8px;color:#fff;text-decoration:none;
  background:rgba(0,0,0,.5);border:1px solid rgba(255,255,255,.18);
  backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);
  opacity:0;transform:translateY(4px);transition:.2s var(--ease)}
.tile .ext{right:8px}
.tile .prev{left:8px}
.tile:hover .ext,.tile:hover .prev{opacity:1;transform:translateY(0)}
.tile .ext:hover,.tile .prev:hover{background:rgba(0,0,0,.75)}
.tile .ext svg,.tile .prev svg{width:13px;height:13px;fill:none;stroke:currentColor;stroke-width:2.1;
  stroke-linecap:round;stroke-linejoin:round}

/* 骨架屏 / 空状态 */
.skel{border:1px solid var(--stroke);border-radius:var(--r-md);overflow:hidden;
  background:var(--surface-2)}
.skel>i{display:block;aspect-ratio:16/10;background:linear-gradient(100deg,
  var(--surface-2) 20%,var(--surface-3) 38%,var(--surface-2) 56%);
  background-size:220% 100%;animation:shimmer 1.35s linear infinite}
.skel>b{display:block;height:9px;margin:11px 12px;border-radius:5px;width:70%;
  background:var(--surface-3);opacity:.6}
@keyframes shimmer{from{background-position:120% 0}to{background-position:-60% 0}}
.empty{text-align:center;padding:46px 20px;color:var(--fg-3);font-size:13px}
.empty.span{grid-column:1/-1}
.empty .big{width:64px;height:64px;margin:0 auto 14px;border-radius:20px;
  display:grid;place-items:center;font-size:27px;
  background:var(--surface-2);border:1px solid var(--stroke);box-shadow:var(--shadow)}
.empty .t{color:var(--fg-2);font-weight:550}
.loading-line{display:flex;align-items:center;justify-content:center;gap:9px;
  padding:14px;margin-bottom:14px;font-size:12.5px;color:var(--fg-2);
  border:1px solid var(--stroke);border-radius:var(--r-md);background:var(--surface-2)}
.err{color:var(--bad);font-size:12.5px;margin-top:6px;white-space:pre-wrap}
.spin{display:inline-block;width:13px;height:13px;border:2px solid var(--stroke-2);
  border-top-color:var(--brand);border-radius:50%;animation:sp .7s linear infinite;
  vertical-align:-2px;flex-shrink:0}
@keyframes sp{to{transform:rotate(360deg)}}

/* 日志 / 折叠提示 */
#log{font:12px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--fg-2);
  background:var(--bg-2);border:1px solid var(--stroke);border-radius:var(--r-sm);
  padding:11px 13px;max-height:150px;overflow-y:auto;white-space:pre-wrap;
  word-break:break-all;margin-top:9px}

/* ---------- 进度条 / 下载面板 ---------- */
.bar{height:6px;background:var(--surface-3);border-radius:999px;overflow:hidden}
.bar>i{display:block;height:100%;width:0;border-radius:999px;
  background:linear-gradient(90deg,var(--brand),var(--brand-2));transition:width .25s var(--ease)}
.pbar{height:8px;background:var(--bg-2);border-radius:999px;overflow:hidden;position:relative}
.pbar>i{display:block;height:100%;width:0;border-radius:999px;
  background:linear-gradient(90deg,var(--brand),var(--brand-2));
  transition:width .18s ease-out;box-shadow:0 0 14px -4px var(--brand)}
.pbar.unknown>i{width:100%!important;opacity:.62;
  background:repeating-linear-gradient(115deg,var(--brand) 0 12px,var(--brand-2) 12px 24px);
  animation:slide 1.1s linear infinite}
@keyframes slide{to{background-position:44px 0}}
.dlpanel{background:var(--surface-2);border:1px solid var(--stroke);
  border-radius:var(--r-md);padding:15px 17px;margin-bottom:14px}
.dlpanel .row1{display:flex;justify-content:space-between;align-items:center;gap:12px;
  flex-wrap:wrap;margin-bottom:11px}
.dlpanel .fname{font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;flex:1;min-width:0}
.dlpanel .nums{font-size:12px;color:var(--fg-3);white-space:nowrap;
  font-variant-numeric:tabular-nums}
.dlpanel .meta2{display:flex;justify-content:space-between;gap:10px;margin-top:9px;
  font-size:11px;color:var(--fg-3);flex-wrap:wrap}

/* ---------- 历史 ---------- */
.hist-head-bar{display:flex;justify-content:space-between;align-items:center;
  gap:12px;flex-wrap:wrap;margin-bottom:16px}
.hist-item{background:var(--surface-2);border:1px solid var(--stroke);
  border-radius:var(--r-md);padding:15px 17px;margin-bottom:11px;
  transition:border-color .2s var(--ease),box-shadow .2s var(--ease)}
.hist-item:hover{border-color:var(--stroke-2);box-shadow:var(--shadow)}
.hist-item.running{border-color:var(--brand);background:var(--brand-soft)}
.hist-head{display:flex;justify-content:space-between;align-items:flex-start;
  gap:14px;flex-wrap:wrap}
.hist-title{font-size:13.5px;font-weight:600;margin-bottom:6px;word-break:break-all;
  letter-spacing:-.01em}
.hist-meta{font-size:11px;color:var(--fg-3);display:flex;gap:14px;flex-wrap:wrap;
  font-variant-numeric:tabular-nums}
.hist-actions{display:flex;gap:7px;align-items:center;flex-shrink:0;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:10.5px;padding:3px 10px;
  border-radius:999px;font-weight:600;white-space:nowrap;border:1px solid transparent}
.pill::before{content:"";width:5px;height:5px;border-radius:50%;background:currentColor}
.pill.run{background:var(--brand-soft);color:var(--brand)}
.pill.done{background:var(--ok-soft);color:var(--ok)}
.pill.cancel{background:var(--surface-3);color:var(--fg-2)}
.pill.error{background:var(--bad-soft);color:var(--bad)}
.hist-files{margin-top:10px;font-size:11px;color:var(--fg-3);max-height:170px;
  overflow-y:auto;font-family:ui-monospace,Menlo,monospace;line-height:1.8;
  border:1px solid var(--stroke);border-radius:var(--r-sm);padding:9px 11px;
  background:var(--bg-2)}
.hist-files .f-ok{color:var(--ok)}
.hist-files .f-bad{color:var(--bad)}
.btn-mini{padding:4px 10px;font-size:11px;border-radius:8px}
.hist-preview{margin-left:8px;color:var(--brand);text-decoration:none;cursor:pointer;
  border:0;background:transparent;padding:0;font:inherit}
.hist-preview:hover{text-decoration:underline}

/* ---------- 底部浮动坞 ---------- */
.footbar{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);z-index:35;
  width:calc(100% - 36px);max-width:1000px;
  display:flex;align-items:center;gap:16px;flex-wrap:wrap;
  padding:12px 16px;border-radius:var(--r-lg);border:1px solid var(--stroke-2);
  background:var(--surface);backdrop-filter:blur(22px) saturate(170%);
  -webkit-backdrop-filter:blur(22px) saturate(170%);box-shadow:var(--shadow-lg);
  animation:rise .32s var(--ease)}
@keyframes rise{from{opacity:0;transform:translate(-50%,14px)}
  to{opacity:1;transform:translate(-50%,0)}}
.footbar .info{font-size:12px;color:var(--fg-2);font-variant-numeric:tabular-nums;
  white-space:nowrap}
.footbar .dock-actions{display:flex;gap:9px;align-items:center;margin-left:auto}

/* ---------- 提示条 ---------- */
.toasts{position:fixed;top:18px;right:18px;z-index:60;display:flex;flex-direction:column;
  gap:9px;pointer-events:none;max-width:min(380px,calc(100% - 36px))}
.toast{display:flex;align-items:flex-start;gap:10px;padding:12px 15px;border-radius:var(--r-md);
  border:1px solid var(--stroke-2);background:var(--surface);
  backdrop-filter:blur(20px) saturate(170%);-webkit-backdrop-filter:blur(20px) saturate(170%);
  box-shadow:var(--shadow-lg);font-size:12.5px;color:var(--fg);
  opacity:0;transform:translateX(14px) scale(.98);transition:.26s var(--ease)}
.toast.in{opacity:1;transform:none}
.toast .ti{width:19px;height:19px;border-radius:7px;display:grid;place-items:center;
  font-size:11px;font-weight:700;flex-shrink:0;background:var(--surface-3);color:var(--fg-2)}
.toast.ok .ti{background:var(--ok-soft);color:var(--ok)}
.toast.bad .ti{background:var(--bad-soft);color:var(--bad)}
.toast.warn .ti{background:var(--warn-soft);color:var(--warn)}
.toast .tm{flex:1;min-width:0;word-break:break-word;line-height:1.5}

/* ---------- 媒体预览 ---------- */
.preview{position:fixed;inset:0;z-index:70;display:grid;place-items:center;
  padding:14px;background:rgba(0,0,0,.72);backdrop-filter:blur(10px);
  -webkit-backdrop-filter:blur(10px)}
.preview-box{width:min(1500px,calc(100vw - 28px));height:min(920px,calc(100vh - 28px));
  display:flex;flex-direction:column;border:1px solid var(--stroke-2);
  border-radius:var(--r-lg);background:var(--surface);box-shadow:var(--shadow-lg);
  overflow:hidden}
.preview[data-kind="audio"] .preview-box{height:auto;min-height:260px}
.preview-head{display:flex;align-items:center;gap:12px;justify-content:space-between;
  padding:12px 14px;border-bottom:1px solid var(--stroke);background:var(--surface-2)}
.preview-title{font-size:13px;font-weight:600;min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.preview-actions{display:flex;gap:8px;align-items:center;flex-shrink:0}
.preview-actions a{display:inline-flex;align-items:center;justify-content:center;color:var(--fg);
  text-decoration:none;border:1px solid var(--stroke);background:var(--surface-2)}
.preview-actions a:hover{border-color:var(--stroke-2);background:var(--surface-3)}
.preview-body{padding:14px;display:grid;place-items:center;min-height:0;flex:1;overflow:auto;
  background:rgba(0,0,0,.18)}
.preview-body img{max-width:100%;max-height:100%;object-fit:contain;
  border-radius:var(--r-sm)}
.preview-body video{width:100%;height:100%;object-fit:contain;border-radius:var(--r-sm);
  background:#000}
.preview-body audio{width:min(720px,100%)}
.preview-empty{color:var(--fg-3);font-size:13px;text-align:center;padding:40px 20px}

.hidden{display:none!important}
@media(max-width:720px){
  main{padding:20px 16px 150px}
  .topbar{padding:12px 16px}
  .brand p{display:none}
  .media-grid{grid-template-columns:repeat(auto-fill,minmax(146px,1fr));gap:11px}
  .footbar{flex-direction:column;align-items:stretch;gap:10px;bottom:12px}
  .footbar .dock-actions{margin-left:0;justify-content:space-between}
  .cmd-field kbd{display:none}
}
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms!important;
    animation-iteration-count:1!important;transition-duration:.01ms!important}
}
</style>
</head>
<body>
<div class="aurora" aria-hidden="true"></div>

<header class="topbar">
  <div class="brand">
    <div class="logo" aria-hidden="true">
      <img src="/assets/logo.png?v=20260924" alt="">
    </div>
    <div class="brand-text">
      <h1>media<span>harvest</span></h1>
      <p>网页媒体抓取 · 图片 / 视频 / 音乐</p>
    </div>
  </div>
  <div class="status-strip">
    <span class="badge" id="b-render">渲染检测中…</span>
    <span class="badge" id="b-ytdlp">yt-dlp</span>
    <span class="badge" id="b-remux">转封装</span>
  </div>
  <div class="top-actions">
    <span class="version">v{{ version }}</span>
    <button class="icon-btn" id="btn-theme" type="button" title="切换深色 / 浅色" aria-label="切换主题"></button>
  </div>
</header>

<main>
  <!-- 标签页 -->
  <div class="tabs" role="tablist">
    <div class="tab active" data-view="download" role="tab">抓取下载</div>
    <div class="tab" data-view="history" role="tab">下载历史<span class="cnt" id="tab-hist-cnt"></span></div>
  </div>

  <div id="view-download">
  <!-- 输入区 -->
  <section class="card hero">
    <label for="url">目标网址</label>
    <div class="cmd">
      <div class="cmd-field">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/></svg>
        <input type="text" id="url" placeholder="粘贴网页、图片、视频或 m3u8 地址…" autocomplete="off" spellcheck="false">
        <kbd>/</kbd>
      </div>
      <button class="primary lg" id="btn-crawl" type="button">
        <svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20.5 20.5-4-4"/></svg>分析页面
      </button>
    </div>
    <div class="hero-foot">
      <span class="hint">自动识别静态页 / JS 动态页 / 视频平台，回车即可开始，无需手动选择。</span>
      <div class="hero-actions">
        <button class="sm ghost" id="btn-paste" type="button">从剪贴板粘贴</button>
      </div>
    </div>

    <details class="adv">
      <summary>高级选项</summary>
      <div class="adv-body">
        <div class="adv-group">
          <h3>抓取行为</h3>
          <div class="grid g-auto">
            <div>
              <label for="types">抓取类型</label>
              <select id="types">
                <option value="image,video" selected>图片 + 视频</option>
                <option value="image">仅图片</option>
                <option value="video,hls,dash">仅视频（含流）</option>
                <option value="audio">仅音频 / 音乐</option>
                <option value="image,video,audio">图片 + 视频 + 音频</option>
                <option value="image,video,audio,hls,dash,segment">全部（含分片）</option>
              </select>
            </div>
            <div>
              <label for="render">浏览器渲染</label>
              <select id="render">
                <option value="auto" selected>自动（推荐）</option>
                <option value="always">总是渲染（动态页 / 反爬）</option>
                <option value="never">从不渲染（最快）</option>
              </select>
            </div>
            <div>
              <label for="ytdlp">yt-dlp 站点适配</label>
              <select id="ytdlp">
                <option value="auto" selected>自动（视频平台启用）</option>
                <option value="always">总是使用</option>
                <option value="never">从不使用</option>
              </select>
            </div>
            <div>
              <label for="depth">站内爬取深度</label>
              <input type="number" id="depth" value="0" min="0" max="5">
            </div>
            <div>
              <label for="max_pages">最多页面数</label>
              <input type="number" id="max_pages" value="1" min="1" max="500">
            </div>
            <div class="field-wide">
              <label for="link_pattern">链接过滤正则（可选）</label>
              <input type="text" id="link_pattern" placeholder="/post/|/photo/" spellcheck="false">
            </div>
          </div>
        </div>

        <div class="adv-group">
          <h3>下载与保存</h3>
          <div class="grid g-auto">
            <div>
              <label for="quality">音质（音乐站点）</label>
              <select id="quality">
                <option value="best" selected>最高音质（无损优先）</option>
                <option value="lossless">仅无损（没有则跳过）</option>
                <option value="high">高音质（m4a 优先）</option>
                <option value="medium">中等音质（约 192k）</option>
                <option value="low">省流（约 128k）</option>
              </select>
            </div>
            <div>
              <label for="expand_playlists">专辑 / 歌单</label>
              <select id="expand_playlists">
                <option value="1" selected>整张抓取（展开全部曲目）</option>
                <option value="0">只抓单个目标</option>
              </select>
            </div>
            <div>
              <label for="music_tags">音乐标签与歌词</label>
              <select id="music_tags">
                <option value="1" selected>写入标签 + 歌词 + 封面</option>
                <option value="0">不写标签（仅保存音频）</option>
              </select>
            </div>
            <div>
              <label for="concurrency">下载并发</label>
              <input type="number" id="concurrency" value="8" min="1" max="32">
            </div>
            <div>
              <label for="max_size">单文件体积上限</label>
              <input type="text" id="max_size" placeholder="如 200M（留空不限）" spellcheck="false">
            </div>
            <div class="field-wide">
              <label for="out_dir">保存目录</label>
              <input type="text" id="out_dir" value="downloads" placeholder="downloads" spellcheck="false">
              <div class="inline-actions">
                <button class="sm" id="btn-save-out" type="button">存为默认</button>
                <button class="sm ghost" id="btn-reset-out" type="button">恢复默认</button>
                <span class="hint" id="cfg-hint"></span>
              </div>
            </div>
          </div>
        </div>

        <div class="adv-group">
          <h3>网络与登录</h3>
          <div class="grid g-auto">
            <div>
              <label for="proxy">代理（可选）</label>
              <input type="text" id="proxy" placeholder="http://127.0.0.1:7890" spellcheck="false">
            </div>
            <div>
              <label for="cookie">Cookie（可选，登录站点）</label>
              <input type="text" id="cookie" placeholder="sessionid=xxx; token=yyy" spellcheck="false">
            </div>
            <div>
              <label for="cookies_from_browser">从浏览器导入 Cookie</label>
              <select id="cookies_from_browser">
                <option value="">不使用</option>
                <option value="chrome">Chrome</option>
                <option value="firefox">Firefox</option>
                <option value="edge">Edge</option>
                <option value="safari">Safari</option>
              </select>
            </div>
          </div>
        </div>
      </div>
    </details>
  </section>

  <!-- 结果区 -->
  <section class="card hidden" id="result-card">
    <div class="res-head">
      <div class="res-title">
        <h2 id="page-title"></h2>
        <a id="page-url" href="#" target="_blank" rel="noreferrer"></a>
      </div>
      <div class="res-tools">
        <button class="sm" id="btn-all" type="button">全选</button>
        <button class="sm" id="btn-none" type="button">清空</button>
        <button class="sm" id="btn-best" type="button">仅高清</button>
      </div>
    </div>

    <div class="chips" id="chips"></div>
    <div class="stats" id="stats"></div>
    <div id="crawl-progress"></div>
    <div class="media-grid" id="media-grid"></div>
    <div id="crawl-warn"></div>
  </section>

  <!-- 空状态 -->
  <section class="card empty" id="empty">
    <div class="big" aria-hidden="true">🕸️</div>
    <div class="t">粘贴一个网址开始分析</div>
    <div class="hint" style="margin-top:9px;line-height:1.9">
      支持普通网页图片 · 动态加载内容 · 视频直链 · m3u8 流 · 抖音 / B站 / YouTube 等平台
    </div>
  </section>
  </div><!-- /view-download -->

  <!-- 下载历史 -->
  <div class="hidden" id="view-history">
    <section class="card">
      <div class="hist-head-bar">
        <div>
          <div class="card-title">下载历史</div>
          <div class="muted" id="hist-base"></div>
        </div>
        <div style="display:flex;gap:8px">
          <button class="sm" id="btn-hist-refresh" type="button">刷新</button>
          <button class="sm danger" id="btn-hist-clear" type="button">清空历史</button>
        </div>
      </div>
      <div id="hist-list"></div>
    </section>
  </div>
</main>

<!-- 底部浮动操作坞 -->
<div class="footbar hidden" id="footbar">
  <div class="info" id="sel-info">已选 0 项</div>
  <div class="bar" style="flex:1 1 200px;min-width:140px"><i id="bar-fill"></i></div>
  <div class="dock-actions">
    <span class="info" id="dl-status"></span>
    <button class="primary" id="btn-download" type="button" disabled>
      <svg class="i" viewBox="0 0 24 24"><path d="M12 4v11"/><path d="m7.5 10.5 4.5 4.5 4.5-4.5"/><path d="M5 20h14"/></svg>下载所选
    </button>
    <button class="sm danger hidden" id="btn-cancel" type="button">取消</button>
  </div>
</div>

<div class="preview hidden" id="preview-modal" role="dialog" aria-modal="true" aria-label="媒体预览">
  <div class="preview-box">
    <div class="preview-head">
      <div class="preview-title" id="preview-title"></div>
      <div class="preview-actions">
        <a class="btn-mini" id="preview-open" href="#" target="_blank" rel="noreferrer">新标签打开</a>
        <button class="sm" id="preview-close" type="button">关闭</button>
      </div>
    </div>
    <div class="preview-body" id="preview-body"></div>
  </div>
</div>

<div class="toasts" id="toasts" aria-live="polite"></div>

<script>
const $ = s => document.querySelector(s);
const state = {items:[], selected:new Set(), filter:'all', crawlJob:null, dlJob:null,
               timer:null, histTimer:null, history:[]};

const SVG = {
  crawl:'<svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20.5 20.5-4-4"/></svg>',
  dl:'<svg class="i" viewBox="0 0 24 24"><path d="M12 4v11"/><path d="m7.5 10.5 4.5 4.5 4.5-4.5"/><path d="M5 20h14"/></svg>',
  busy:'<span class="spin"></span>',
};

/* ---------- 主题 ---------- */
const SUN = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
const MOON = '<svg viewBox="0 0 24 24"><path d="M21 12.8A8.5 8.5 0 1 1 11.2 3a6.6 6.6 0 0 0 9.8 9.8Z"/></svg>';
function setTheme(t){
  document.documentElement.dataset.theme = t;
  const btn = $('#btn-theme');
  if(btn){ btn.innerHTML = t === 'dark' ? SUN : MOON; btn.title = t === 'dark' ? '切换到浅色' : '切换到深色'; }
  try{ localStorage.setItem('mh-theme', t); }catch(e){}
}
(function initTheme(){
  let t = null;
  try{ t = localStorage.getItem('mh-theme'); }catch(e){}
  setTheme(t || 'dark');  // 默认深色，只有用户手动切过才跟随保存值
})();
$('#btn-theme').onclick = () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');

/* ---------- 轻提示 ---------- */
function toast(msg, kind, ms){
  const box = $('#toasts');
  if(!box) return;
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  const ico = kind === 'ok' ? '✓' : kind === 'bad' ? '✕' : kind === 'warn' ? '!' : 'i';
  el.innerHTML = `<span class="ti">${ico}</span><span class="tm">${esc(msg)}</span>`;
  box.appendChild(el);
  requestAnimationFrame(() => el.classList.add('in'));
  setTimeout(() => { el.classList.remove('in'); setTimeout(() => el.remove(), 300); }, ms || 3400);
}

// ---------- 环境状态 ----------
async function loadStatus(){
  try{
    const s = await (await fetch('/api/status')).json();
    const r = $('#b-render');
    r.textContent = s.render ? '浏览器渲染' : '浏览器渲染不可用';
    r.className = 'badge ' + (s.render ? 'on' : 'warn');
    if(!s.render) r.title = s.render_reason;
    const y = $('#b-ytdlp');
    y.textContent = s.ytdlp ? 'yt-dlp 就绪' : 'yt-dlp 缺失';
    y.className = 'badge ' + (s.ytdlp ? 'on' : 'off');
    const m = $('#b-remux');
    m.textContent = s.ffmpeg ? 'ffmpeg 转封装' : (s.remux ? '内置转封装' : '转封装不可用');
    m.className = 'badge ' + ((s.ffmpeg||s.remux) ? 'on' : 'off');
    $('#out_dir').value = s.default_out || 'downloads';
    if(s.config_error){
      $('#cfg-hint').innerHTML = `<span class="err">配置有误: ${esc(s.config_error)}</span>`;
    }else if(s.config_out_dir){
      $('#cfg-hint').textContent = `已配置默认 · ${s.config_path}`;
      $('#cfg-hint').title = s.config_path;
    }else{
      $('#cfg-hint').textContent = '未配置（使用默认 downloads）';
    }
  }catch(e){ console.error(e); }
}

// ---------- 下载地址配置 ----------
async function postConfig(body){
  const res = await fetch('/api/config/out-dir', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const data = await res.json();
  if(data.error) throw new Error(data.error);
  $('#out_dir').value = data.out_dir;
  $('#cfg-hint').textContent = `${data.message} · ${data.path}`;
  $('#cfg-hint').title = data.path;
  toast(data.message, 'ok');
  await loadStatus();
}

// ---------- 收集参数 ----------
function opts(){
  return {
    url: $('#url').value.trim(),
    types: $('#types').value,
    render: $('#render').value,
    ytdlp: $('#ytdlp').value,
    quality: $('#quality').value,
    expand_playlists: $('#expand_playlists').value === '1',
    music_tags: $('#music_tags').value === '1',
    depth: +$('#depth').value || 0,
    max_pages: +$('#max_pages').value || 1,
    link_pattern: $('#link_pattern').value.trim(),
    proxy: $('#proxy').value.trim(),
    cookie: $('#cookie').value.trim(),
    cookies_from_browser: $('#cookies_from_browser').value,
    headless: true,
    scroll: true,
  };
}

// ---------- 分析 ----------
function skeletons(n){
  return Array.from({length:n||10}, () => '<div class="skel"><i></i><b></b></div>').join('');
}
function setCrawlProgress(text){
  const el = $('#crawl-progress');
  if(!el) return;
  el.innerHTML = text
    ? `<div class="loading-line"><span class="spin"></span><span>${esc(text)}</span></div>` : '';
}

async function crawl(){
  const url = $('#url').value.trim();
  if(!url){ $('#url').focus(); toast('请先填写目标网址', 'warn', 2400); return; }
  const btn = $('#btn-crawl');
  btn.disabled = true;
  btn.innerHTML = SVG.busy + '分析中…';
  $('#empty').classList.add('hidden');
  $('#result-card').classList.remove('hidden');
  $('#page-title').textContent = '正在分析…';
  const pu = $('#page-url'); pu.textContent = url; pu.href = url.indexOf('http') === 0 ? url : 'https://' + url;
  $('#chips').innerHTML = ''; $('#stats').innerHTML = ''; $('#crawl-warn').innerHTML = '';
  $('#media-grid').innerHTML = skeletons(10);
  setCrawlProgress('正在抓取页面并分析媒体资源…');

  try{
    const res = await fetch('/api/crawl', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(opts())});
    const data = await res.json();
    if(data.error){ throw new Error(data.error); }
    state.crawlJob = data.job;
    pollCrawl();
  }catch(e){
    setCrawlProgress('');
    $('#media-grid').innerHTML = `<div class="empty span"><div class="err">分析失败: ${esc(e.message)}</div></div>`;
    resetCrawlBtn();
    toast('分析失败: ' + e.message, 'bad', 5000);
  }
}

function resetCrawlBtn(){
  $('#btn-crawl').disabled = false;
  $('#btn-crawl').innerHTML = SVG.crawl + '分析页面';
}

function pollCrawl(){
  clearInterval(state.timer);
  state.timer = setInterval(async () => {
    try{
      const j = await (await fetch('/api/job/' + state.crawlJob)).json();
      if(j.status === 'running' || j.status === 'pending'){
        setCrawlProgress(j.progress || '抓取中…');
        return;
      }
      clearInterval(state.timer);
      setCrawlProgress('');
      resetCrawlBtn();
      if(j.status === 'error'){
        $('#media-grid').innerHTML = `<div class="empty span"><div class="err">抓取失败: ${esc(j.error)}</div></div>`;
        toast('抓取失败: ' + (j.error || '未知错误'), 'bad', 5000);
        return;
      }
      state.items = j.items || [];
      renderResult(j);
    }catch(e){ clearInterval(state.timer); setCrawlProgress(''); resetCrawlBtn(); }
  }, 700);
}

// ---------- 渲染结果 ----------
function renderResult(j){
  $('#page-title').textContent = j.title || '(无标题)';
  const pu = $('#page-url');
  pu.textContent = j.url || '';
  pu.href = j.url || '#';
  if(!state.items.length){
    $('#media-grid').innerHTML = '<div class="empty span"><div class="big">🔍</div>' +
      '<div class="t">未发现媒体资源</div>' +
      '<div class="hint" style="margin-top:8px">试试切换到「总是渲染」模式，或检查网址是否正确</div></div>';
    $('#chips').innerHTML = ''; $('#stats').innerHTML = '';
    updateFootbar();
    toast('未发现可下载的媒体资源', 'warn');
    return;
  }
  // 默认选中所有主资源
  state.selected = new Set(state.items.filter(i => i.primary).map(i => i.id));
  renderChips(j.counts || {});
  renderGrid();
  const warn = (j.warnings || []).slice(0,3);
  $('#crawl-warn').innerHTML = warn.length
    ? `<details class="adv"><summary>${warn.length} 条抓取提示</summary><div id="log">${warn.map(esc).join('\n')}</div></details>` : '';
  updateFootbar();
  toast(`分析完成 · 共发现 ${state.items.length} 项资源`, 'ok');
}

const TYPE_LABEL = {image:'图片', video:'视频', audio:'音频', hls:'HLS流', dash:'DASH流', segment:'分片', other:'其他'};

function renderChips(counts){
  const total = state.items.length;
  let html = `<div class="chip ${state.filter==='all'?'active':''}" data-f="all">全部<span class="n">${total}</span></div>`;
  for(const [k,v] of Object.entries(counts)){
    if(!v) continue;
    html += `<div class="chip ${state.filter===k?'active':''}" data-f="${k}">${TYPE_LABEL[k]||k}<span class="n">${v}</span></div>`;
  }
  $('#chips').innerHTML = html;
  $('#chips').querySelectorAll('.chip').forEach(c => c.onclick = () => {
    state.filter = c.dataset.f; renderChips(counts); renderGrid();
  });
}

function visible(){
  return state.filter === 'all' ? state.items : state.items.filter(i => i.type === state.filter);
}

const EXT_ICON = '<svg viewBox="0 0 24 24"><path d="M14 5h5v5"/><path d="M19 5 11 13"/><path d="M18 14.5V18a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h3.5"/></svg>';
const PREVIEW_ICON = '<svg viewBox="0 0 24 24"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="3"/></svg>';

function canPreviewType(type){
  return ['image','video','audio','hls','dash'].includes(type);
}

function remotePreviewUrl(item){
  return `/api/proxy?url=${encodeURIComponent(item.url)}&referer=${encodeURIComponent(item.referer||item.page_url||'')}`;
}

function openPreview(kind, src, title, openUrl){
  const modal = $('#preview-modal');
  const body = $('#preview-body');
  $('#preview-title').textContent = title || '媒体预览';
  $('#preview-open').href = openUrl || src;
  modal.dataset.kind = kind || '';
  let html = '';
  if(kind === 'image'){
    html = `<img src="${esc(src)}" alt="${esc(title||'媒体预览')}">`;
  }else if(kind === 'audio'){
    html = `<audio src="${esc(src)}" controls autoplay data-preview-player></audio>`;
  }else if(kind === 'video' || kind === 'hls' || kind === 'dash'){
    html = `<video src="${esc(src)}" controls autoplay playsinline data-preview-player></video>`;
  }else{
    html = '<div class="preview-empty">这种文件暂不支持内嵌预览，可以在新标签页打开。</div>';
  }
  body.innerHTML = html;
  const player = body.querySelector('[data-preview-player]');
  if(player){
    player.onerror = () => {
      body.innerHTML = '<div class="preview-empty">浏览器无法直接播放这个媒体格式。可以点右上角“新标签打开”，或先下载后从历史记录预览本地文件。</div>';
    };
  }
  modal.classList.remove('hidden');
}

function closePreview(){
  const modal = $('#preview-modal');
  $('#preview-body').innerHTML = '';
  modal.dataset.kind = '';
  modal.classList.add('hidden');
}

function fmtDur(s){
  if(!s && s !== 0) return '';
  s = Math.round(s);
  const m = Math.floor(s / 60), r = s % 60;
  return m + ':' + String(r).padStart(2, '0');
}

function renderGrid(){
  const list = visible();
  const html = list.map(i => {
    const sel = state.selected.has(i.id);
    let thumb = '';
    if(i.type === 'image'){
      const src = `/api/proxy?url=${encodeURIComponent(i.url)}&referer=${encodeURIComponent(i.referer||i.page_url||'')}`;
      // 占位层常驻，图片加载失败时直接移除 <img>，不会出现浏览器破图图标
      thumb = `<div class="ph">🖼️</div><img class="thumb" loading="lazy" src="${src}" onerror="this.remove()">`;
    } else {
      thumb = `<div class="ph">${i.type==='video'||i.type==='hls'||i.type==='dash'?'🎬':(i.type==='audio'?'🎵':'📄')}</div>`;
    }
    const size = i.size ? fmtSize(i.size) : '';
    const dims = (i.width && i.height) ? `${i.width}×${i.height}` : '';
    const dur = fmtDur(i.duration);
    // 音乐条目展示「歌手 - 歌名」与音质，比 URL 文件名有用得多
    const m = i.music;
    const name = m && m.display ? m.display : (i.display_name || i.url);
    const bits = [dur, size, dims].filter(Boolean);
    if (m && m.album) bits.unshift(m.album);
    if (i.meta && i.meta.format_label) bits.push(i.meta.format_label);
    if (m && m.lyrics) bits.push('含歌词');
    return `<div class="tile ${sel?'sel':''}" data-id="${i.id}" title="${esc(name)}">
      <div class="tick">${sel?'✓':''}</div>
      <div class="tag ${i.type}">${TYPE_LABEL[i.type]||i.type}</div>
      <div class="thumbwrap">${thumb}
        ${canPreviewType(i.type) ? `<button class="prev" type="button" title="在线预览">${PREVIEW_ICON}</button>` : ''}
        <a class="ext" href="${esc(i.url)}" target="_blank" rel="noreferrer" title="在新标签页打开原始地址">${EXT_ICON}</a>
      </div>
      <div class="meta">
        <div class="nm" title="${esc(i.url)}">${esc(name)}</div>
        <div class="sub"><span>${esc(i.source_label||'')}</span><span>${esc(bits.join(' · '))}</span></div>
      </div>
    </div>`;
  }).join('');
  $('#media-grid').innerHTML = html || '<div class="empty span">该类型下没有资源</div>';
  $('#media-grid').querySelectorAll('.tile').forEach(t => {
    t.onclick = () => {
      const id = t.dataset.id;
      if(state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
      renderGrid(); updateFootbar();
    };
    // 「打开原地址」不应连带切换选中状态
    const ext = t.querySelector('.ext');
    if(ext) ext.onclick = ev => ev.stopPropagation();
    const prev = t.querySelector('.prev');
    if(prev) prev.onclick = ev => {
      ev.stopPropagation();
      const item = state.items.find(x => x.id === t.dataset.id);
      if(!item) return;
      openPreview(item.type, remotePreviewUrl(item), item.display_name || item.url, item.url);
    };
  });
  updateStats();
}

function updateStats(){
  const list = visible();
  const sel = state.items.filter(i => state.selected.has(i.id));
  const bytes = sel.reduce((a,i) => a + (i.size||0), 0);
  const unknown = sel.filter(i => !i.size).length;
  $('#stats').innerHTML = `
    <div class="stat"><b>${list.length}</b><span>当前显示</span></div>
    <div class="stat"><b>${sel.length}</b><span>已选资源</span></div>
    <div class="stat"><b>${bytes?fmtSize(bytes):'—'}</b><span>${unknown?'部分体积未知':'预估体积'}</span></div>`;
}

function updateFootbar(){
  const n = state.selected.size;
  $('#footbar').classList.toggle('hidden', !state.items.length);
  $('#sel-info').textContent = `已选 ${n} / ${state.items.length} 项`;
  $('#btn-download').disabled = n === 0 || !!state.dlJob;
}

// ---------- 下载 ----------
async function download(){
  if(!state.selected.size) return;
  const items = state.items.filter(i => state.selected.has(i.id));
  const o = opts();
  const body = {
    url: o.url, types: o.types, render: o.render, ytdlp: o.ytdlp,
    quality: o.quality,
    expand_playlists: o.expand_playlists,
    music_tags: o.music_tags,
    proxy: o.proxy, cookie: o.cookie, cookies_from_browser: o.cookies_from_browser,
    items: items,
    title: $('#page-title').textContent || '',
    out_dir: $('#out_dir').value.trim() || 'downloads',
    session_dir: true,
    download: {
      concurrency: +$('#concurrency').value || 8,
      hls_concurrency: +$('#concurrency').value || 8,
      max_size: $('#max_size').value.trim(),
      lrc_file: false,
    },
  };
  $('#btn-download').disabled = true;
  $('#btn-download').innerHTML = SVG.busy + '下载中…';
  $('#btn-cancel').classList.remove('hidden');
  $('#btn-cancel').disabled = false;
  $('#dl-status').textContent = '';
  $('#bar-fill').style.width = '0%';
  showDlPanel('准备中…', 0);

  try{
    const res = await fetch('/api/download', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)});
    const data = await res.json();
    if(data.error){ throw new Error(data.error); }
    state.dlJob = data.job;
    pollDownload();
  }catch(e){
    $('#dl-status').innerHTML = `<span class="err">${esc(e.message)}</span>`;
    hideDlPanel();
    resetDlBtn();
    toast('下载启动失败: ' + e.message, 'bad', 5000);
  }
}

// ---------- 下载进度面板 ----------
function showDlPanel(name, pct, opts){
  opts = opts || {};
  let el = $('#dl-panel');
  if(!el){
    el = document.createElement('div');
    el.id = 'dl-panel';
    el.className = 'dlpanel';
    const grid = $('#media-grid');
    grid.parentNode.insertBefore(el, grid);
  }
  el.innerHTML = `
    <div class="row1">
      <div class="fname" title="${esc(name)}">${esc(name)}</div>
      <div class="nums" id="dl-numbers"></div>
    </div>
    <div class="pbar ${opts.unknown?'unknown':''}"><i style="width:${pct||0}%"></i></div>
    <div class="meta2">
      <span id="dl-phase">${esc(opts.phase||'')}</span>
      <span id="dl-detail">${esc(opts.detail||'')}</span>
    </div>`;
}

function updateDlPanel(live){
  const el = $('#dl-panel');
  if(!el || !live || !live.total_files) return;
  const pct = live.overall_percent || 0;
  const unknown = !live.bytes_total;
  const bar = el.querySelector('.pbar');
  const fill = el.querySelector('.pbar > i');
  bar.classList.toggle('unknown', unknown);
  fill.style.width = pct + '%';

  const fp = live.file_percent;
  el.querySelector('.fname').textContent = live.name || '';
  el.querySelector('.fname').title = live.name || '';
  $('#dl-numbers').innerHTML =
    `<b style="color:var(--fg)">${pct.toFixed(0)}%</b>` +
    (fp !== null && fp !== undefined ? ` · 当前 ${fp.toFixed(0)}%` : '') +
    ` · ${live.files_done}/${live.total_files} 个`;
  const phaseText = {downloading:'下载中', remuxing:'转封装中', finishing:'收尾中'}[live.phase] || live.phase;
  $('#dl-phase').textContent = phaseText;
  const parts = [];
  if(live.bytes_total) parts.push(`${fmtSize(live.bytes_done)} / ${fmtSize(live.bytes_total)}`);
  if(live.ok) parts.push(`成功 ${live.ok}`);
  if(live.failed) parts.push(`失败 ${live.failed}`);
  if(live.detail) parts.push(live.detail);
  $('#dl-detail').textContent = parts.join(' · ');
}

function hideDlPanel(){
  const el = $('#dl-panel');
  if(el) el.remove();
}

function pollDownload(){
  clearInterval(state.timer);
  state.timer = setInterval(async () => {
    try{
      const j = await (await fetch('/api/job/' + state.dlJob)).json();
      const live = j.live || {};
      const pct = live.overall_percent !== undefined ? live.overall_percent
                  : (j.total ? j.done / j.total * 100 : 0);
      $('#bar-fill').style.width = pct + '%';
      if(live.total_files) updateDlPanel(live);
      else if(j.status === 'running') showDlPanel('准备中…', pct);

      $('#dl-status').textContent =
        `${j.ok||0} 成功` + (j.failed?` · ${j.failed} 失败`:'') +
        (j.bytes_total?` · ${j.bytes_human}`:'');

      if(j.status === 'running' || j.status === 'pending') return;
      // 结束
      clearInterval(state.timer);
      hideDlPanel();
      resetDlBtn();
      $('#btn-cancel').classList.add('hidden');
      $('#bar-fill').style.width = '0%';
      if(j.status === 'done'){
        $('#dl-status').innerHTML = `<span style="color:var(--ok)">已完成 ${j.ok||0} 项</span>`;
        showFailures(j);
        toast(j.message || `下载完成 · ${j.ok||0} 项`, j.failed ? 'warn' : 'ok', 5200);
        loadHistory();
      } else if(j.status === 'cancelled'){
        $('#dl-status').innerHTML = `<span style="color:var(--warn)">已取消</span>`;
        toast(j.message || '下载已取消', 'warn');
        loadHistory();
      } else {
        $('#dl-status').innerHTML = `<span class="err">${esc(j.error||'失败')}</span>`;
        toast('下载失败: ' + (j.error || '未知错误'), 'bad', 5200);
      }
      if(j.out_dir) addOutDirHint(j.out_dir);
    }catch(e){ clearInterval(state.timer); hideDlPanel(); resetDlBtn(); }
  }, 400);
}

function addOutDirHint(dir){
  const el = $('#dl-status');
  const old = $('#out-dir-hint');
  if(old) old.remove();
  const span = document.createElement('span');
  span.id = 'out-dir-hint';
  span.className = 'hint';
  span.style.marginLeft = '2px';
  span.textContent = `→ ${dir}`;
  span.title = dir;
  el.parentNode.appendChild(span);
  setTimeout(() => span.remove(), 15000);
}

function showFailures(j){
  const bad = (j.results||[]).filter(r => !r.ok && !r.skipped);
  if(!bad.length) return;
  const el = document.createElement('details');
  el.className = 'adv';
  el.innerHTML = `<summary>${bad.length} 项下载失败（点击查看）</summary><div id="log">` +
    bad.slice(0,40).map(r => `${esc(r.name)}: ${esc(r.error)}`).join('\n') + '</div>';
  $('#crawl-warn').appendChild(el);
}

function resetDlBtn(){
  state.dlJob = null;
  $('#btn-download').innerHTML = SVG.dl + '下载所选';
  updateFootbar();
}

// ---------- 工具 ----------
function fmtSize(b){
  if(!b) return '';
  const u = ['B','KB','MB','GB']; let i = 0, n = b;
  while(n >= 1024 && i < u.length-1){ n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + ' ' + u[i];
}
function esc(s){
  return String(s==null?'':s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ---------- 下载历史 ----------
async function loadHistory(){
  try{
    const res = await fetch('/api/history');
    const data = await res.json();
    state.history = data.entries || [];
    $('#hist-base').textContent = data.base_dir ? '保存位置：' + data.base_dir : '';
    renderHistory();
    const running = state.history.filter(e => e.running).length;
    $('#tab-hist-cnt').textContent = state.history.length
      ? (running ? ` (${running} 进行中)` : ` (${state.history.length})`) : '';
  }catch(e){ console.error(e); }
}

const STATUS_META = {
  running:   {cls:'run',    text:'进行中'},
  done:      {cls:'done',   text:'已完成'},
  cancelled: {cls:'cancel', text:'已取消'},
  error:     {cls:'error',  text:'失败'},
};

function renderHistory(){
  const list = state.history;
  if(!list.length){
    $('#hist-list').innerHTML = '<div class="empty"><div class="big">🗂️</div>' +
      '<div class="t">还没有下载记录</div>' +
      '<div class="hint" style="margin-top:8px">完成一次下载后，这里会保留文件清单与保存位置</div></div>';
    return;
  }
  $('#hist-list').innerHTML = list.map(e => {
    const meta = STATUS_META[e.status] || STATUS_META.done;
    const okFiles = (e.files||[]).filter(f => f.ok).length;
    const badFiles = (e.files||[]).filter(f => !f.ok && !f.skipped).length;
    const live = e.live || {};
    const pct = live.overall_percent;

    return `<div class="hist-item ${e.running?'running':''}" data-id="${e.id}">
      <div class="hist-head">
        <div style="flex:1;min-width:0">
          <div class="hist-title">${esc(e.title || e.dir_name || '(未命名)')}</div>
          <div class="hist-meta">
            <span>${esc(e.created_text)}</span>
            ${e.duration!=null?`<span>耗时 ${e.duration.toFixed(1)}s</span>`:''}
            <span>${e.ok||0} 成功${e.failed?` · ${e.failed} 失败`:''}${e.skipped?` · ${e.skipped} 跳过`:''}</span>
            ${e.bytes_total?`<span>${fmtSize(e.bytes_total)}</span>`:''}
          </div>
          <div class="hist-meta" style="margin-top:5px">
            <span title="${esc(e.out_dir)}">📁 ${esc(e.dir_name || e.out_dir)}</span>
          </div>
        </div>
        <div class="hist-actions">
          <span class="pill ${meta.cls}">${meta.text}</span>
          ${e.running && e.cancellable
            ? `<button class="sm danger btn-mini btn-hist-cancel" data-id="${e.id}" type="button">取消</button>` : ''}
          <button class="sm btn-mini btn-hist-open" data-id="${e.id}" type="button">打开目录</button>
          <button class="sm btn-mini btn-hist-del" data-id="${e.id}" type="button">删除</button>
        </div>
      </div>
      ${e.running && pct!==undefined ? `
        <div class="pbar ${live.bytes_total?'':'unknown'}" style="margin-top:12px">
          <i style="width:${pct}%"></i>
        </div>
        <div class="hist-meta" style="margin-top:7px">
          <span>${esc(live.name||'')}</span>
          <span>${pct.toFixed(0)}% · ${live.files_done||0}/${live.total_files||e.total} 个</span>
        </div>` : ''}
      ${e.message ? `<div class="hist-meta" style="margin-top:7px"><span>${esc(e.message)}</span></div>` : ''}
      ${e.files && e.files.length ? `
        <details class="adv" style="margin-top:10px;border-top:0;padding-top:0">
          <summary>文件清单（${okFiles} 成功${badFiles?` / ${badFiles} 失败`:''}）</summary>
          <div class="hist-files">${e.files.slice(0,200).map((f,idx) =>
            `<div class="${f.ok?'f-ok':(f.skipped?'':'f-bad')}">${f.ok?'✓':(f.skipped?'–':'✗')} ${esc(f.name||f.url)}${f.ok&&f.size?` <span style="opacity:.6">${fmtSize(f.size)}</span>`:''}${f.ok&&canPreviewType(f.type)?` <button class="hist-preview" data-entry="${e.id}" data-index="${idx}" data-type="${esc(f.type)}" data-title="${esc(f.name||f.url)}" type="button">预览</button>`:''}${f.error?` <span class="f-bad">${esc(f.error)}</span>`:''}</div>`
          ).join('')}</div>
        </details>` : ''}
    </div>`;
  }).join('');

  // 绑定按钮
  document.querySelectorAll('.btn-hist-cancel').forEach(b => b.onclick = async ev => {
    ev.stopPropagation();
    b.disabled = true; b.textContent = '取消中…';
    try{
      const r = await fetch(`/api/history/${b.dataset.id}/cancel`, {method:'POST'});
      const d = await r.json();
      if(d.error) throw new Error(d.error);
      toast('已请求取消该任务', 'warn');
      setTimeout(loadHistory, 600);
    }catch(e){ b.textContent = '取消失败'; toast('取消失败: ' + e.message, 'bad'); }
  });
  document.querySelectorAll('.btn-hist-open').forEach(b => b.onclick = async ev => {
    ev.stopPropagation();
    try{
      const r = await fetch(`/api/history/${b.dataset.id}/open`, {method:'POST'});
      const d = await r.json();
      if(d.error) throw new Error(d.error);
    }catch(e){ toast('打开失败: ' + e.message, 'bad'); }
  });
  document.querySelectorAll('.btn-hist-del').forEach(b => b.onclick = async ev => {
    ev.stopPropagation();
    try{
      await fetch(`/api/history/${b.dataset.id}`, {method:'DELETE'});
      toast('已删除该条记录', 'ok', 2400);
      loadHistory();
    }catch(e){}
  });
  document.querySelectorAll('.hist-preview').forEach(b => b.onclick = ev => {
    ev.stopPropagation();
    const src = `/api/history/${b.dataset.entry}/file/${b.dataset.index}`;
    openPreview(b.dataset.type, src, b.dataset.title || '媒体预览', src);
  });
}

function startHistPoll(){
  clearInterval(state.histTimer);
  state.histTimer = setInterval(() => {
    // 只在历史页可见、或有任务在跑时刷新，避免无谓轮询
    const onHist = !$('#view-history').classList.contains('hidden');
    const anyRunning = state.history.some(e => e.running) || !!state.dlJob;
    if(onHist || anyRunning) loadHistory();
  }, 1500);
}

// ---------- 标签切换 ----------
document.querySelectorAll('.tab').forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    const view = tab.dataset.view;
    $('#view-download').classList.toggle('hidden', view !== 'download');
    $('#view-history').classList.toggle('hidden', view !== 'history');
    $('#footbar').classList.toggle('hidden', view !== 'download' || !state.items.length);
    if(view === 'history') loadHistory();
  };
});

// ---------- 事件绑定 ----------
$('#btn-crawl').onclick = crawl;
$('#url').addEventListener('keydown', e => { if(e.key === 'Enter') crawl(); });
$('#btn-paste').onclick = async () => {
  try{
    const t = (await navigator.clipboard.readText() || '').trim();
    if(!t){ toast('剪贴板里没有文本', 'warn'); return; }
    $('#url').value = t;
    $('#url').focus();
  }catch(e){ toast('浏览器拒绝了剪贴板访问，请手动粘贴', 'warn'); }
};
$('#btn-download').onclick = download;
$('#btn-cancel').onclick = async () => {
  if(!state.dlJob) return;
  const btn = $('#btn-cancel');
  btn.disabled = true;
  btn.textContent = '取消中…';
  try{
    await fetch(`/api/job/${state.dlJob}/cancel`, {method:'POST'});
    $('#dl-status').innerHTML = '<span style="color:var(--warn)">正在取消…</span>';
    setTimeout(() => { btn.disabled = false; btn.textContent = '取消'; }, 2500);
  }catch(e){
    btn.disabled = false;
    btn.textContent = '取消';
  }
};
$('#btn-hist-refresh').onclick = loadHistory;
$('#btn-hist-clear').onclick = async () => {
  if(!confirm('清空下载历史记录？\n（已下载的文件不会被删除）')) return;
  await fetch('/api/history', {method:'DELETE'});
  toast('历史记录已清空', 'ok');
  loadHistory();
};
$('#btn-save-out').onclick = async () => {
  const dir = $('#out_dir').value.trim();
  if(!dir){ $('#cfg-hint').textContent = '请先填写保存目录'; toast('请先填写保存目录', 'warn'); return; }
  try{ await postConfig({out_dir: dir}); }
  catch(e){ $('#cfg-hint').innerHTML = `<span class="err">${esc(e.message)}</span>`; toast(e.message, 'bad'); }
};
$('#btn-reset-out').onclick = async () => {
  try{ await postConfig({reset: true}); }
  catch(e){ $('#cfg-hint').innerHTML = `<span class="err">${esc(e.message)}</span>`; toast(e.message, 'bad'); }
};
$('#preview-close').onclick = closePreview;
$('#preview-modal').onclick = e => {
  if(e.target === $('#preview-modal')) closePreview();
};
$('#btn-all').onclick = () => { state.selected = new Set(visible().map(i=>i.id)); renderGrid(); updateFootbar(); };
$('#btn-none').onclick = () => { state.selected.clear(); renderGrid(); updateFootbar(); };
$('#btn-best').onclick = () => {
  // 只选没有同名低清版本的主资源，并按类型排除分片
  state.selected = new Set(visible().filter(i => i.primary && i.type!=='segment').map(i=>i.id));
  renderGrid(); updateFootbar();
};

// ---------- 快捷键 ----------
document.addEventListener('keydown', e => {
  const tag = (e.target.tagName || '').toLowerCase();
  const typing = tag === 'input' || tag === 'select' || tag === 'textarea';
  if(e.key === 'Escape' && !$('#preview-modal').classList.contains('hidden')){
    closePreview();
    return;
  }
  if(e.key === '/' && !typing){ e.preventDefault(); $('#url').focus(); $('#url').select(); return; }
  if(e.key === 'Escape'){
    if(document.activeElement && typing){ document.activeElement.blur(); return; }
    if(state.items.length){ state.selected.clear(); renderGrid(); updateFootbar(); }
    return;
  }
  if((e.metaKey || e.ctrlKey) && e.key === 'Enter'){
    e.preventDefault();
    if(state.selected.size && !state.dlJob) download();
    else if(!typing || e.target === $('#url')) crawl();
  }
});

window.addEventListener('beforeunload', e => {
  if(state.dlJob){ e.preventDefault(); e.returnValue = ''; }
});
loadStatus();
loadHistory();
startHistPoll();
</script>
</body>
</html>
"""


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="mh-web",
        description="mediaharvest Web 界面（端口、下载目录等可在 mediaharvest.toml 里配置）",
    )
    parser.add_argument("--host", default=None,
                        help="监听地址（默认取配置文件，未配置则 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None,
                        help="端口（默认取配置文件，未配置则 8848）")
    parser.add_argument("--no-browser", action="store_true", default=None,
                        help="不自动打开浏览器")
    parser.add_argument("--config", default="", metavar="PATH",
                        help="指定配置文件路径（默认 mediaharvest.toml）")
    parser.add_argument("--show-config", action="store_true",
                        help="显示当前生效的配置后退出")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.show_config:
        print(describe_config(args.config))
        return 0
    if cfg.error:
        print(f"配置文件有问题: {cfg.error}")
        print(f"  {cfg.path}")

    # 命令行 > 环境变量 > 配置文件 > 内置默认值
    host = args.host if args.host is not None else cfg.get_str("web.host", "127.0.0.1")
    port = args.port if args.port is not None else cfg.get_int("web.port", 8848)
    open_browser = (
        not args.no_browser
        if args.no_browser is not None
        else cfg.get_bool("web.open_browser", True)
    )
    serve(host, port, open_browser=bool(open_browser), config_path=args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
