"""下载控制：取消令牌与进度上报。

**为什么需要单独的取消令牌**：早期实现只在「每个文件开始前」检查一次
``should_stop``，导致一个 500MB 的文件一旦开始就必须下完——用户点取消
没有任何反应。真正的取消必须能打断：

* 普通文件的分块读取循环（每读一块检查一次）
* HLS 分片下载（每个分片前检查，并在途任务收到取消信号）
* 转封装阶段（纯 Python 转封装在子线程里，需要能被打断）

因此这里提供一个**可跨线程/跨协程共享**的取消令牌。Web 界面里取消请求
由 Flask 线程处理，而下载跑在另一个线程的事件循环里，只有这种设计才能
让两边可靠通信。
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional


class CancelledError(Exception):
    """下载被用户取消。"""


class CancelToken:
    """取消令牌：任意线程调用 :meth:`cancel`，下载循环即可感知。

    用 ``threading.Event`` 而非 ``asyncio.Event``，因为取消信号可能来自
    完全不同的线程（Flask 的请求线程）。
    """

    __slots__ = ("_event", "_reason", "_cancelled_at")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason = ""
        self._cancelled_at: Optional[float] = None

    # ---- 信号 --------------------------------------------------------

    def cancel(self, reason: str = "用户取消") -> None:
        """请求取消（幂等，可从任意线程调用）。"""
        if not self._event.is_set():
            self._reason = reason
            self._cancelled_at = time.time()
        self._event.set()

    def reset(self) -> None:
        self._event.clear()
        self._reason = ""
        self._cancelled_at = None

    # ---- 查询 --------------------------------------------------------

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def cancelled_at(self) -> Optional[float]:
        return self._cancelled_at

    def check(self) -> None:
        """已取消则抛出 :class:`CancelledError`（供循环内部调用）。"""
        if self._event.is_set():
            raise CancelledError(self._reason or "用户取消")

    def wait(self, timeout: float) -> bool:
        """等待取消信号，返回是否已取消。"""
        return self._event.wait(timeout)


#: 永远不取消的令牌，供 CLI 等场景当默认值
NEVER = CancelToken()


# --------------------------------------------------------------------------
# 进度上报
# --------------------------------------------------------------------------

@dataclass
class Progress:
    """一次下载任务的实时进度快照。

    ``bytes_total`` 为 0 表示大小未知（例如 chunked 响应），此时前端应
    显示「不确定进度」的动画而不是百分比。
    """

    #: 当前正在处理的第几个文件（从 1 开始）
    index: int = 0
    #: 本次任务的文件总数
    total_files: int = 0
    #: 当前文件名
    name: str = ""
    #: 当前文件已下载字节
    bytes_done: int = 0
    #: 当前文件总字节（0 = 未知）
    bytes_total: int = 0
    #: 已完成文件数
    files_done: int = 0
    #: 已完成文件的总字节
    bytes_finished: int = 0
    #: 阶段：downloading / remuxing / finishing
    phase: str = "downloading"
    #: 已成功 / 失败 / 跳过
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    #: 额外信息（如 HLS 的分片进度）
    detail: str = ""

    @property
    def file_percent(self) -> Optional[float]:
        """当前文件完成百分比；大小未知时返回 None。"""
        if self.bytes_total <= 0:
            return None
        return min(100.0, self.bytes_done * 100.0 / self.bytes_total)

    @property
    def overall_percent(self) -> float:
        """整体完成百分比（按文件数估算，当前文件的进度也计入）。"""
        if self.total_files <= 0:
            return 0.0
        current = 0.0
        pct = self.file_percent
        if pct is not None:
            current = pct / 100.0
        return min(100.0, (self.files_done + current) * 100.0 / self.total_files)

    def to_dict(self) -> Dict[str, object]:
        return {
            "index": self.index,
            "total_files": self.total_files,
            "name": self.name,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "file_percent": self.file_percent,
            "files_done": self.files_done,
            "bytes_finished": self.bytes_finished,
            "overall_percent": round(self.overall_percent, 2),
            "phase": self.phase,
            "ok": self.ok,
            "failed": self.failed,
            "skipped": self.skipped,
            "detail": self.detail,
        }


class ProgressReporter:
    """节流后的进度上报器。

    分块下载每 64KB 就会产生一次进度事件，500MB 文件会有数千次；
    这里限制为最多 ``interval`` 秒上报一次，避免刷爆前端与日志。
    首次与最后一次一定上报，保证界面不会停在 0% 或 99%。
    """

    def __init__(
        self,
        callback: Optional[Callable[[Progress], None]] = None,
        *,
        interval: float = 0.15,
    ) -> None:
        self.callback = callback
        self.interval = interval
        self.state = Progress()
        self._last_emit = 0.0
        self._dirty = False

    def emit(self, *, force: bool = False) -> None:
        """按节流策略上报当前状态。"""
        if self.callback is None:
            return
        now = time.monotonic()
        if not force and (now - self._last_emit) < self.interval:
            self._dirty = True
            return
        self._last_emit = now
        self._dirty = False
        try:
            self.callback(_snapshot(self.state))
        except Exception:
            # 进度回调不该影响下载本身
            pass

    def flush(self) -> None:
        """确保最后一次状态被上报。"""
        if self._dirty or True:
            self.emit(force=True)

    # ---- 便捷修改 ----------------------------------------------------

    def start_file(self, index: int, name: str, size: Optional[int] = None) -> None:
        self.state.index = index
        self.state.name = name
        self.state.bytes_done = 0
        self.state.bytes_total = size or 0
        self.state.phase = "downloading"
        self.state.detail = ""
        self.emit(force=True)

    def add_bytes(self, count: int) -> None:
        self.state.bytes_done += count
        self.emit()

    def set_total(self, size: int) -> None:
        self.state.bytes_total = size

    def set_phase(self, phase: str, detail: str = "") -> None:
        self.state.phase = phase
        if detail:
            self.state.detail = detail
        self.emit(force=True)

    def finish_file(self, ok: bool, size: int = 0, skipped: bool = False) -> None:
        self.state.files_done += 1
        if ok:
            self.state.ok += 1
            self.state.bytes_finished += size
        elif skipped:
            self.state.skipped += 1
        else:
            self.state.failed += 1
        self.emit(force=True)


def _snapshot(state: Progress) -> Progress:
    """浅拷贝一份进度，避免回调方读到后续变化的同一个对象。"""
    return Progress(**{f: getattr(state, f) for f in Progress.__dataclass_fields__})


async def _interruptible_sleep(seconds: float, token: CancelToken) -> None:
    """可被取消打断的等待。

    重试前的退避等待也要能被取消——否则用户点了取消还得等几秒才响应。
    """
    if seconds <= 0:
        token.check()
        return
    deadline = time.monotonic() + seconds
    while True:
        token.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.2, remaining))


__all__ = [
    "CancelToken",
    "CancelledError",
    "NEVER",
    "Progress",
    "ProgressReporter",
    "_interruptible_sleep",
]
