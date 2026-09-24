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

from flask import Flask, Response, jsonify, render_template_string, request, send_file

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

    async def fetch() -> tuple:
        from .fetcher import Fetcher

        async with Fetcher(timeout=20.0, retries=1) as fetcher:
            resp = await fetcher.get(target, referer=referer)
            return resp.status_code, resp.headers.get("content-type", ""), resp.content

    try:
        status, ctype, body = asyncio.run_coroutine_threadsafe(
            fetch(), RUNNER._loop
        ).result(timeout=30)
    except Exception as exc:
        return jsonify({"error": f"拉取失败: {exc}"}), 502

    if status >= 400:
        return jsonify({"error": f"远端返回 {status}"}), 502
    return Response(body, mimetype=ctype or "application/octet-stream")


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
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mediaharvest · 网页媒体抓取</title>
<style>
:root{
  --bg:#0d1117; --panel:#161b22; --panel2:#1c2128; --border:#30363d;
  --fg:#e6edf3; --dim:#8b949e; --accent:#2f81f7; --green:#3fb950;
  --red:#f85149; --yellow:#d29922; --purple:#a371f7;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
header{padding:18px 24px;border-bottom:1px solid var(--border);background:var(--panel);
  position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
h1{font-size:17px;margin:0;font-weight:600}
h1 span{color:var(--accent)}
.badge{font-size:11px;padding:2px 8px;border-radius:10px;background:var(--panel2);
  border:1px solid var(--border);color:var(--dim)}
.badge.on{color:var(--green);border-color:#238636}
.badge.off{color:var(--dim);opacity:.6}
.badge.warn{color:var(--yellow);border-color:#9e6a03}
main{max-width:1220px;margin:0 auto;padding:22px 24px 80px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;
  padding:18px;margin-bottom:18px}
label{display:block;font-size:12px;color:var(--dim);margin-bottom:5px}
input[type=text],input[type=number],select{width:100%;padding:8px 11px;border-radius:7px;
  border:1px solid var(--border);background:var(--bg);color:var(--fg);font-size:13px;font-family:inherit}
input:focus,select:focus{outline:none;border-color:var(--accent)}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.row>div{flex:1;min-width:120px}
button{padding:9px 18px;border-radius:7px;border:1px solid var(--border);background:var(--panel2);
  color:var(--fg);font-size:13px;cursor:pointer;font-family:inherit;transition:.15s;white-space:nowrap}
button:hover:not(:disabled){border-color:var(--accent);background:#21262d}
button:disabled{opacity:.45;cursor:not-allowed}
button.primary{background:#238636;border-color:#2ea043;font-weight:600}
button.primary:hover:not(:disabled){background:#2ea043}
button.danger{background:#8b1a13;border-color:#b62324}
button.sm{padding:5px 11px;font-size:12px}
.hint{font-size:11px;color:var(--dim);margin-top:5px}
details{margin-top:12px}
summary{cursor:pointer;font-size:12px;color:var(--dim);user-select:none;padding:4px 0}
summary:hover{color:var(--fg)}
.grid{display:grid;gap:10px}
.g2{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.g3{grid-template-columns:repeat(auto-fit,minmax(110px,1fr))}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:12px}
.chip{padding:4px 12px;border-radius:14px;border:1px solid var(--border);background:var(--panel2);
  font-size:12px;cursor:pointer;user-select:none;transition:.15s}
.chip:hover{border-color:var(--accent)}
.chip.active{background:#1f6feb33;border-color:var(--accent);color:#79c0ff}
.chip .n{opacity:.65;margin-left:4px}
#log{font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--dim);
  background:var(--bg);border:1px solid var(--border);border-radius:7px;padding:10px;
  max-height:130px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}
#stats{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;margin:12px 0}
#stats b{font-size:16px;display:block}
.media-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));gap:12px}
.tile{background:var(--panel2);border:2px solid var(--border);border-radius:9px;
  overflow:hidden;cursor:pointer;position:relative;transition:.15s}
.tile:hover{border-color:#484f58;transform:translateY(-2px)}
.tile.sel{border-color:var(--accent);box-shadow:0 0 0 3px #1f6feb33}
.tile .thumb{width:100%;height:118px;object-fit:cover;display:block;background:#0b0f14}
.tile .ph{width:100%;height:118px;display:flex;align-items:center;justify-content:center;
  background:#0b0f14;color:var(--dim);font-size:26px}
.tile .meta{padding:7px 9px}
.tile .nm{font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-bottom:3px}
.tile .sub{font-size:10px;color:var(--dim);display:flex;justify-content:space-between;gap:6px}
.tick{position:absolute;top:6px;left:6px;width:19px;height:19px;border-radius:5px;
  border:2px solid #fff9;background:#000000aa;display:flex;align-items:center;
  justify-content:center;font-size:12px;color:#fff}
.tile.sel .tick{background:var(--accent);border-color:var(--accent)}
.tag{position:absolute;top:6px;right:6px;font-size:10px;padding:2px 7px;border-radius:9px;
  background:#000000cc;color:#fff;font-weight:600}
.tag.video{background:#a371f7dd}.tag.hls{background:#db61a2dd}.tag.audio{background:#d29922dd}
.bar{height:6px;background:var(--panel2);border-radius:3px;overflow:hidden;margin-top:8px}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#2f81f7,#3fb950);
  width:0;transition:width .25s}
/* 下载进度面板 */
.dlpanel{background:var(--panel2);border:1px solid var(--border);border-radius:9px;
  padding:13px 15px;margin-bottom:14px}
.dlpanel .row1{display:flex;justify-content:space-between;align-items:center;gap:12px;
  flex-wrap:wrap;margin-bottom:9px}
.dlpanel .fname{font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;flex:1;min-width:0}
.dlpanel .nums{font-size:12px;color:var(--dim);white-space:nowrap;font-variant-numeric:tabular-nums}
.dlpanel .pbar{height:9px;background:#0b0f14;border-radius:5px;overflow:hidden;position:relative}
.dlpanel .pbar>i{display:block;height:100%;background:linear-gradient(90deg,#2f81f7,#3fb950);
  width:0;transition:width .18s ease-out}
/* 大小未知时的流动条纹 */
.dlpanel .pbar.unknown>i{width:100%!important;opacity:.5;
  background:repeating-linear-gradient(45deg,#2f81f7 0 10px,#1f6feb 10px 20px);
  animation:slide 1s linear infinite}
@keyframes slide{to{background-position:28px 0}}
.dlpanel .meta2{display:flex;justify-content:space-between;gap:10px;margin-top:7px;
  font-size:11px;color:var(--dim);flex-wrap:wrap}
.btn-mini{padding:3px 10px;font-size:11px;border-radius:6px}
/* 历史面板 */
.hist-item{background:var(--panel2);border:1px solid var(--border);border-radius:9px;
  padding:12px 14px;margin-bottom:10px;transition:.15s}
.hist-item:hover{border-color:#484f58}
.hist-item.running{border-color:#1f6feb88;background:#1f6feb11}
.hist-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;
  flex-wrap:wrap}
.hist-title{font-size:13px;font-weight:600;margin-bottom:4px;word-break:break-all}
.hist-meta{font-size:11px;color:var(--dim);display:flex;gap:12px;flex-wrap:wrap}
.hist-actions{display:flex;gap:7px;align-items:center;flex-shrink:0}
.pill{font-size:10px;padding:2px 9px;border-radius:10px;font-weight:600;white-space:nowrap}
.pill.run{background:#1f6feb33;color:#79c0ff}
.pill.done{background:#23863633;color:#56d364}
.pill.cancel{background:#8b949e33;color:#c9d1d9}
.pill.error{background:#f8514933;color:#ff7b72}
.hist-files{margin-top:9px;font-size:11px;color:var(--dim);max-height:150px;
  overflow-y:auto;font-family:ui-monospace,Menlo,monospace;line-height:1.7}
.hist-files .f-ok{color:#56d364}.hist-files .f-bad{color:#ff7b72}
.tabs{display:flex;gap:6px;margin-bottom:16px;border-bottom:1px solid var(--border)}
.tab{padding:8px 16px;cursor:pointer;font-size:13px;border-bottom:2px solid transparent;
  color:var(--dim);transition:.15s;user-select:none}
.tab:hover{color:var(--fg)}
.tab.active{color:var(--fg);border-bottom-color:var(--accent);font-weight:600}
.tab .cnt{font-size:10px;opacity:.7;margin-left:5px}
.empty{text-align:center;padding:44px 20px;color:var(--dim)}
.empty .big{font-size:34px;margin-bottom:10px;opacity:.5}
.footbar{position:fixed;bottom:0;left:0;right:0;background:var(--panel);
  border-top:1px solid var(--border);padding:11px 24px;display:flex;gap:14px;
  align-items:center;justify-content:space-between;z-index:30;flex-wrap:wrap}
.footbar .info{font-size:12px;color:var(--dim)}
.err{color:var(--red);font-size:12px;margin-top:6px;white-space:pre-wrap}
.spin{display:inline-block;width:12px;height:12px;border:2px solid #ffffff33;
  border-top-color:var(--accent);border-radius:50%;animation:sp .7s linear infinite;vertical-align:-2px}
@keyframes sp{to{transform:rotate(360deg)}}
.hidden{display:none!important}
</style>
</head>
<body>

<header>
  <h1>media<span>harvest</span></h1>
  <span class="badge" id="b-render">渲染检测中…</span>
  <span class="badge" id="b-ytdlp">yt-dlp</span>
  <span class="badge" id="b-remux">转封装</span>
  <span style="flex:1"></span>
  <span class="badge">v{{ version }}</span>
</header>

<main>
  <!-- 标签页 -->
  <div class="tabs">
    <div class="tab active" data-view="download">抓取下载</div>
    <div class="tab" data-view="history">下载历史<span class="cnt" id="tab-hist-cnt"></span></div>
  </div>

  <div id="view-download">
  <!-- 输入区 -->
  <div class="card">
    <label>目标网址（支持任意网页，或直接粘贴图片/视频/m3u8 地址）</label>
    <div class="row">
      <div style="flex:4;min-width:280px">
        <input type="text" id="url" placeholder="https://example.com/gallery" autocomplete="off">
      </div>
      <div style="flex:0 0 auto">
        <button class="primary" id="btn-crawl">🔍 分析页面</button>
      </div>
    </div>
    <div class="hint">回车即可分析。自动识别静态页 / JS 动态页 / 视频平台，无需手动选择。</div>

    <details>
      <summary>⚙️ 高级选项</summary>
      <div class="grid g2" style="margin-top:12px">
        <div>
          <label>抓取类型</label>
          <select id="types">
            <option value="image,video" selected>图片 + 视频</option>
            <option value="image">仅图片</option>
            <option value="video,hls,dash">仅视频（含流）</option>
            <option value="image,video,audio">图片 + 视频 + 音频</option>
            <option value="image,video,audio,hls,dash,segment">全部（含分片）</option>
          </select>
        </div>
        <div>
          <label>浏览器渲染</label>
          <select id="render">
            <option value="auto" selected>自动（推荐）</option>
            <option value="always">总是渲染（动态页/反爬）</option>
            <option value="never">从不渲染（最快）</option>
          </select>
        </div>
        <div>
          <label>yt-dlp 站点适配</label>
          <select id="ytdlp">
            <option value="auto" selected>自动（视频平台启用）</option>
            <option value="always">总是使用</option>
            <option value="never">从不使用</option>
          </select>
        </div>
        <div>
          <label>站内爬取深度</label>
          <input type="number" id="depth" value="0" min="0" max="5">
        </div>
        <div>
          <label>最多页面数</label>
          <input type="number" id="max_pages" value="1" min="1" max="500">
        </div>
        <div>
          <label>链接过滤正则（可选）</label>
          <input type="text" id="link_pattern" placeholder="/post/|/photo/">
        </div>
        <div>
          <label>下载并发</label>
          <input type="number" id="concurrency" value="8" min="1" max="32">
        </div>
        <div>
          <label>保存目录</label>
          <input type="text" id="out_dir" value="downloads" placeholder="downloads">
          <div class="row" style="margin-top:6px">
            <button class="sm" id="btn-save-out" type="button">存为默认</button>
            <button class="sm" id="btn-reset-out" type="button">恢复默认</button>
            <span class="hint" id="cfg-hint" style="margin:0;align-self:center"></span>
          </div>
        </div>
        <div>
          <label>代理（可选）</label>
          <input type="text" id="proxy" placeholder="http://127.0.0.1:7890">
        </div>
        <div>
          <label>Cookie（可选，登录站点）</label>
          <input type="text" id="cookie" placeholder="sessionid=xxx; token=yyy">
        </div>
        <div>
          <label>从浏览器导入 Cookie</label>
          <select id="cookies_from_browser">
            <option value="">不使用</option>
            <option value="chrome">Chrome</option>
            <option value="firefox">Firefox</option>
            <option value="edge">Edge</option>
            <option value="safari">Safari</option>
          </select>
        </div>
        <div>
          <label>单文件体积上限</label>
          <input type="text" id="max_size" placeholder="如 200M（留空不限）">
        </div>
      </div>
    </details>
  </div>

  <!-- 结果区 -->
  <div class="card hidden" id="result-card">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap">
      <div style="flex:1;min-width:200px">
        <div id="page-title" style="font-weight:600;margin-bottom:3px"></div>
        <div id="page-url" class="hint" style="margin:0"></div>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="sm" id="btn-all">全选</button>
        <button class="sm" id="btn-none">清空</button>
        <button class="sm" id="btn-best">仅高清</button>
      </div>
    </div>

    <div class="chips" id="chips" style="margin-top:14px"></div>
    <div id="stats"></div>
    <div class="media-grid" id="media-grid"></div>
    <div id="crawl-warn"></div>
  </div>

  <!-- 空状态 -->
  <div class="card empty" id="empty">
    <div class="big">🕸️</div>
    <div>粘贴一个网址开始分析</div>
    <div class="hint" style="margin-top:8px">
      支持：普通网页图片 · 动态加载内容 · 视频直链 · m3u8 流 · 抖音/B站/YouTube 等平台
    </div>
  </div>
  </div><!-- /view-download -->

  <!-- 下载历史 -->
  <div class="hidden" id="view-history">
    <div style="display:flex;justify-content:space-between;align-items:center;
                gap:12px;flex-wrap:wrap">
      <div>
        <div style="font-weight:600">下载历史</div>
        <div class="hint" style="margin:0" id="hist-base"></div>
      </div>
      <div style="display:flex;gap:8px">
        <button class="sm" id="btn-hist-refresh">刷新</button>
        <button class="sm danger" id="btn-hist-clear">清空历史</button>
      </div>
    </div>
    <div id="hist-list" style="margin-top:14px"></div>
  </div>
</main>

<!-- 底部操作栏 -->
<div class="footbar hidden" id="footbar">
  <div class="info" id="sel-info">已选 0 项</div>
  <div class="bar" style="flex:1;max-width:380px;margin:0"><i id="bar-fill"></i></div>
  <div style="display:flex;gap:9px;align-items:center">
    <span class="info" id="dl-status"></span>
    <button class="primary" id="btn-download" disabled>⬇ 下载所选</button>
    <button class="sm danger hidden" id="btn-cancel">取消</button>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const state = {items:[], selected:new Set(), filter:'all', crawlJob:null, dlJob:null,
               timer:null, histTimer:null, history:[]};

// ---------- 环境状态 ----------
async function loadStatus(){
  try{
    const s = await (await fetch('/api/status')).json();
    const r = $('#b-render');
    r.textContent = s.render ? '浏览器渲染 ✓' : '浏览器渲染 ✗';
    r.className = 'badge ' + (s.render ? 'on' : 'warn');
    if(!s.render) r.title = s.render_reason;
    const y = $('#b-ytdlp');
    y.textContent = s.ytdlp ? 'yt-dlp ✓' : 'yt-dlp ✗';
    y.className = 'badge ' + (s.ytdlp ? 'on' : 'off');
    const m = $('#b-remux');
    m.textContent = s.ffmpeg ? 'ffmpeg ✓' : (s.remux ? '内置转封装 ✓' : '转封装 ✗');
    m.className = 'badge ' + ((s.ffmpeg||s.remux) ? 'on' : 'off');
    $('#out_dir').value = s.default_out || 'downloads';
    if(s.config_error){
      $('#cfg-hint').innerHTML = `<span class="err">配置有误: ${esc(s.config_error)}</span>`;
    }else if(s.config_out_dir){
      $('#cfg-hint').textContent = `已配置默认 · ${s.config_path}`;
      $('#cfg-hint').title = s.config_path;
    }else{
      $('#cfg-hint').textContent = '未配置（用默认 downloads）';
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
  await loadStatus();
}

// ---------- 收集参数 ----------
function opts(){
  return {
    url: $('#url').value.trim(),
    types: $('#types').value,
    render: $('#render').value,
    ytdlp: $('#ytdlp').value,
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
async function crawl(){
  const url = $('#url').value.trim();
  if(!url){ $('#url').focus(); return; }
  $('#btn-crawl').disabled = true;
  $('#btn-crawl').innerHTML = '<span class="spin"></span> 分析中…';
  $('#empty').classList.add('hidden');
  $('#result-card').classList.remove('hidden');
  $('#media-grid').innerHTML = '<div class="empty" style="grid-column:1/-1"><span class="spin"></span> 正在抓取页面并分析媒体资源…</div>';
  $('#chips').innerHTML = ''; $('#stats').innerHTML = ''; $('#crawl-warn').innerHTML = '';

  try{
    const res = await fetch('/api/crawl', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(opts())});
    const data = await res.json();
    if(data.error){ throw new Error(data.error); }
    state.crawlJob = data.job;
    pollCrawl();
  }catch(e){
    $('#media-grid').innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="err">分析失败: ${esc(e.message)}</div></div>`;
    resetCrawlBtn();
  }
}

function resetCrawlBtn(){
  $('#btn-crawl').disabled = false;
  $('#btn-crawl').innerHTML = '🔍 分析页面';
}

function pollCrawl(){
  clearInterval(state.timer);
  state.timer = setInterval(async () => {
    try{
      const j = await (await fetch('/api/job/' + state.crawlJob)).json();
      if(j.status === 'running' || j.status === 'pending'){
        $('#media-grid').innerHTML = `<div class="empty" style="grid-column:1/-1"><span class="spin"></span> ${esc(j.progress || '抓取中…')}</div>`;
        return;
      }
      clearInterval(state.timer);
      resetCrawlBtn();
      if(j.status === 'error'){
        $('#media-grid').innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="err">抓取失败: ${esc(j.error)}</div></div>`;
        return;
      }
      state.items = j.items || [];
      renderResult(j);
    }catch(e){ clearInterval(state.timer); resetCrawlBtn(); }
  }, 700);
}

// ---------- 渲染结果 ----------
function renderResult(j){
  $('#page-title').textContent = j.title || '(无标题)';
  $('#page-url').textContent = j.url || '';
  if(!state.items.length){
    $('#media-grid').innerHTML = '<div class="empty" style="grid-column:1/-1">未发现媒体资源<br><span class="hint">试试「总是渲染」模式，或检查网址是否正确</span></div>';
    $('#chips').innerHTML = ''; $('#stats').innerHTML = '';
    updateFootbar(); return;
  }
  // 默认选中所有主资源
  state.selected = new Set(state.items.filter(i => i.primary).map(i => i.id));
  renderChips(j.counts || {});
  renderGrid();
  const warn = (j.warnings || []).slice(0,3);
  $('#crawl-warn').innerHTML = warn.length
    ? `<details style="margin-top:14px"><summary>⚠️ ${warn.length} 条提示</summary><div id="log">${warn.map(esc).join('\n')}</div></details>` : '';
  updateFootbar();
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

function renderGrid(){
  const list = visible();
  const html = list.map(i => {
    const sel = state.selected.has(i.id);
    let thumb;
    if(i.type === 'image'){
      const src = `/api/proxy?url=${encodeURIComponent(i.url)}&referer=${encodeURIComponent(i.referer||i.page_url||'')}`;
      thumb = `<img class="thumb" loading="lazy" src="${src}" onerror="this.outerHTML='<div class=\\'ph\\'>🖼️</div>'">`;
    } else {
      thumb = `<div class="ph">${i.type==='video'||i.type==='hls'||i.type==='dash'?'🎬':(i.type==='audio'?'🎵':'📄')}</div>`;
    }
    const size = i.size ? fmtSize(i.size) : '';
    const dims = (i.width && i.height) ? `${i.width}×${i.height}` : '';
    return `<div class="tile ${sel?'sel':''}" data-id="${i.id}">
      <div class="tick">${sel?'✓':''}</div>
      <div class="tag ${i.type}">${TYPE_LABEL[i.type]||i.type}</div>
      ${thumb}
      <div class="meta">
        <div class="nm" title="${esc(i.url)}">${esc(i.display_name||i.url)}</div>
        <div class="sub"><span>${esc(i.source_label||'')}</span><span>${[size,dims].filter(Boolean).join(' · ')}</span></div>
      </div>
    </div>`;
  }).join('');
  $('#media-grid').innerHTML = html || '<div class="empty" style="grid-column:1/-1">该类型下没有资源</div>';
  $('#media-grid').querySelectorAll('.tile').forEach(t => t.onclick = () => {
    const id = t.dataset.id;
    if(state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
    renderGrid(); updateFootbar();
  });
  updateStats();
}

function updateStats(){
  const list = visible();
  const sel = state.items.filter(i => state.selected.has(i.id));
  const bytes = sel.reduce((a,i) => a + (i.size||0), 0);
  const unknown = sel.filter(i => !i.size).length;
  $('#stats').innerHTML = `
    <div><b>${list.length}</b><span class="hint">当前显示</span></div>
    <div><b>${sel.length}</b><span class="hint">已选</span></div>
    <div><b>${bytes?fmtSize(bytes):'—'}</b><span class="hint">${unknown?'部分未知体积':'预估体积'}</span></div>`;
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
    proxy: o.proxy, cookie: o.cookie, cookies_from_browser: o.cookies_from_browser,
    items: items,
    title: $('#page-title').textContent || '',
    out_dir: $('#out_dir').value.trim() || 'downloads',
    session_dir: true,
    download: {
      concurrency: +$('#concurrency').value || 8,
      hls_concurrency: +$('#concurrency').value || 8,
      max_size: $('#max_size').value.trim(),
    },
  };
  $('#btn-download').disabled = true;
  $('#btn-download').innerHTML = '<span class="spin"></span> 下载中…';
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
      if(j.status === 'done'){
        $('#dl-status').innerHTML = `<span style="color:var(--green)">✓ ${esc(j.message||'完成')}</span>`;
        showFailures(j);
        loadHistory();
      } else if(j.status === 'cancelled'){
        $('#dl-status').innerHTML = `<span style="color:var(--yellow)">⊘ ${esc(j.message||'已取消')}</span>`;
        loadHistory();
      } else {
        $('#dl-status').innerHTML = `<span class="err">${esc(j.error||'失败')}</span>`;
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
  span.style.marginLeft = '10px';
  span.textContent = `→ ${dir}`;
  span.title = dir;
  el.parentNode.appendChild(span);
  setTimeout(() => span.remove(), 15000);
}

function showFailures(j){
  const bad = (j.results||[]).filter(r => !r.ok && !r.skipped);
  if(!bad.length) return;
  const el = document.createElement('details');
  el.style.marginTop = '14px';
  el.innerHTML = `<summary>⚠️ ${bad.length} 项失败（点击查看）</summary><div id="log">` +
    bad.slice(0,40).map(r => `${esc(r.name)}: ${esc(r.error)}`).join('\n') + '</div>';
  $('#crawl-warn').appendChild(el);
}

function resetDlBtn(){
  state.dlJob = null;
  $('#btn-download').innerHTML = '⬇ 下载所选';
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
    $('#hist-list').innerHTML = '<div class="empty" style="padding:30px">还没有下载记录</div>';
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
          <div class="hist-meta" style="margin-top:4px">
            <span title="${esc(e.out_dir)}">📁 ${esc(e.dir_name || e.out_dir)}</span>
          </div>
        </div>
        <div class="hist-actions">
          <span class="pill ${meta.cls}">${meta.text}</span>
          ${e.running && e.cancellable
            ? `<button class="sm danger btn-mini btn-hist-cancel" data-id="${e.id}">取消</button>` : ''}
          <button class="sm btn-mini btn-hist-open" data-id="${e.id}">打开目录</button>
          <button class="sm btn-mini btn-hist-del" data-id="${e.id}">删除</button>
        </div>
      </div>
      ${e.running && pct!==undefined ? `
        <div class="pbar ${live.bytes_total?'':'unknown'}" style="margin-top:10px">
          <i style="width:${pct}%"></i>
        </div>
        <div class="hist-meta" style="margin-top:6px">
          <span>${esc(live.name||'')}</span>
          <span>${pct.toFixed(0)}% · ${live.files_done||0}/${live.total_files||e.total} 个</span>
        </div>` : ''}
      ${e.message ? `<div class="hist-meta" style="margin-top:6px"><span>${esc(e.message)}</span></div>` : ''}
      ${e.files && e.files.length ? `
        <details style="margin-top:8px">
          <summary style="font-size:11px">文件清单（${okFiles} 成功${badFiles?` / ${badFiles} 失败`:''}）</summary>
          <div class="hist-files">${e.files.slice(0,200).map(f =>
            `<div class="${f.ok?'f-ok':(f.skipped?'':'f-bad')}">${f.ok?'✓':(f.skipped?'–':'✗')} ${esc(f.name||f.url)}${f.ok&&f.size?` <span style="opacity:.6">${fmtSize(f.size)}</span>`:''}${f.error?` <span class="f-bad">${esc(f.error)}</span>`:''}</div>`
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
      setTimeout(loadHistory, 600);
    }catch(e){ b.textContent = '取消失败'; }
  });
  document.querySelectorAll('.btn-hist-open').forEach(b => b.onclick = async ev => {
    ev.stopPropagation();
    try{
      const r = await fetch(`/api/history/${b.dataset.id}/open`, {method:'POST'});
      const d = await r.json();
      if(d.error) alert(d.error);
    }catch(e){ alert('打开失败: ' + e.message); }
  });
  document.querySelectorAll('.btn-hist-del').forEach(b => b.onclick = async ev => {
    ev.stopPropagation();
    try{
      await fetch(`/api/history/${b.dataset.id}`, {method:'DELETE'});
      loadHistory();
    }catch(e){}
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
$('#btn-download').onclick = download;
$('#btn-cancel').onclick = async () => {
  if(!state.dlJob) return;
  const btn = $('#btn-cancel');
  btn.disabled = true;
  btn.textContent = '取消中…';
  try{
    await fetch(`/api/job/${state.dlJob}/cancel`, {method:'POST'});
    $('#dl-status').innerHTML = '<span style="color:var(--yellow)">正在取消…</span>';
    // 立刻刷新一次，让用户尽快看到反馈
    setTimeout(() => { const t = state.timer; }, 0);
  }catch(e){
    btn.disabled = false;
    btn.textContent = '取消';
  }
};
$('#btn-hist-refresh').onclick = loadHistory;
$('#btn-hist-clear').onclick = async () => {
  if(!confirm('清空下载历史记录？\n（已下载的文件不会被删除）')) return;
  await fetch('/api/history', {method:'DELETE'});
  loadHistory();
};
$('#btn-save-out').onclick = async () => {
  const dir = $('#out_dir').value.trim();
  if(!dir){ $('#cfg-hint').textContent = '请先填写保存目录'; return; }
  try{ await postConfig({out_dir: dir}); }
  catch(e){ $('#cfg-hint').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
};
$('#btn-reset-out').onclick = async () => {
  try{ await postConfig({reset: true}); }
  catch(e){ $('#cfg-hint').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
};
$('#btn-all').onclick = () => { state.selected = new Set(visible().map(i=>i.id)); renderGrid(); updateFootbar(); };
$('#btn-none').onclick = () => { state.selected.clear(); renderGrid(); updateFootbar(); };
$('#btn-best').onclick = () => {
  // 只选没有同名低清版本的主资源，并按类型排除分片
  state.selected = new Set(visible().filter(i => i.primary && i.type!=='segment').map(i=>i.id));
  renderGrid(); updateFootbar();
};
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
