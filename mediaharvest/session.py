"""下载会话：时间戳目录与历史记录。

每次下载都会创建一个以时间戳命名的会话目录，把本次的所有文件放在里面，
并按类型分子目录::

    /Users/mac/jm/文件/技术/reptile/download/
    └── 2026-09-24_153045_原创AI剧集/
        ├── images/
        └── videos/

这样每次下载互不干扰，也不会出现「上次的文件被这次覆盖」的问题。

历史记录持久化到项目下的 ``.mediaharvest_history.json``，记录每次会话的
状态、文件清单与结果，供 Web 界面的「下载历史」面板展示，
**并支持对仍在进行的会话执行取消**。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

#: 历史文件保留的最大会话数（避免无限增长）
MAX_HISTORY = 200

#: 会话目录名前缀格式：2026-09-24_153045
STAMP_FORMAT = "%Y-%m-%d_%H%M%S"


def timestamp_name(title: str = "", *, now: Optional[float] = None) -> str:
    """生成「时间戳 + 标题」形式的会话目录名。

    形如 ``2026-09-24_153045_原创AI剧集``。标题会被清洗并截断，
    没有标题时只用时间戳。
    """
    moment = datetime.fromtimestamp(now) if now else datetime.now()
    stamp = moment.strftime(STAMP_FORMAT)
    clean = _clean(title)
    return f"{stamp}_{clean}" if clean else stamp


def _clean(text: str, max_len: int = 40) -> str:
    """清洗标题，使其能安全用作目录名。"""
    text = (text or "").strip()
    if not text:
        return ""
    # 去掉控制字符与路径分隔符
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", text)
    text = re.sub(r"\s+", "_", text)
    # 去掉重复/首尾的下划线
    text = re.sub(r"_{2,}", "_", text).strip("_.-")
    if len(text) > max_len:
        text = text[:max_len].rstrip("_.-")
    return text


def session_dir(base: str, title: str = "", *, now: Optional[float] = None) -> str:
    """在 ``base`` 下创建并返回一个时间戳会话目录（绝对路径）。

    若同一秒内重复创建，会追加 ``_2``、``_3`` 后缀避免冲突。
    """
    root = os.path.abspath(os.path.expanduser(os.path.expandvars(base or ".")))
    name = timestamp_name(title, now=now)
    target = os.path.join(root, name)
    counter = 2
    while os.path.exists(target):
        target = os.path.join(root, f"{name}_{counter}")
        counter += 1
    os.makedirs(target, exist_ok=True)
    return target


# --------------------------------------------------------------------------
# 历史记录
# --------------------------------------------------------------------------

@dataclass
class HistoryEntry:
    """一次下载会话的记录。"""

    id: str
    created: float
    title: str = ""
    page_url: str = ""
    out_dir: str = ""
    status: str = "running"      # running | done | cancelled | error
    total: int = 0
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    bytes_total: int = 0
    message: str = ""
    error: str = ""
    files: List[Dict[str, Any]] = field(default_factory=list)
    finished: Optional[float] = None

    @property
    def duration(self) -> Optional[float]:
        if self.finished is None:
            return None
        return max(0.0, self.finished - self.created)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "created": self.created,
            "created_text": datetime.fromtimestamp(self.created).strftime("%Y-%m-%d %H:%M:%S"),
            "finished_text": (
                datetime.fromtimestamp(self.finished).strftime("%Y-%m-%d %H:%M:%S")
                if self.finished else ""
            ),
            "duration": self.duration,
            "title": self.title,
            "page_url": self.page_url,
            "out_dir": self.out_dir,
            "dir_name": os.path.basename(self.out_dir.rstrip("/")) if self.out_dir else "",
            "status": self.status,
            "total": self.total,
            "ok": self.ok,
            "failed": self.failed,
            "skipped": self.skipped,
            "bytes_total": self.bytes_total,
            "message": self.message,
            "error": self.error,
            "files": self.files,
            "running": self.status == "running",
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HistoryEntry":
        allowed = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in allowed})


class HistoryStore:
    """历史记录的读写（线程安全，落盘为 JSON）。

    下载在后台线程里更新，Flask 请求线程读取，因此需要加锁。
    """

    def __init__(self, path: str = "") -> None:
        if not path:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(root, ".mediaharvest_history.json")
        self.path = path
        self._lock = threading.RLock()
        self._entries: Dict[str, HistoryEntry] = {}
        self._order: List[str] = []
        self.load()

    # ---- 持久化 ------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            self._entries.clear()
            self._order.clear()
            if not os.path.isfile(self.path):
                return
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                return
            items = data.get("entries") if isinstance(data, dict) else data
            if not isinstance(items, list):
                return
            recovered = 0
            for raw in items:
                try:
                    entry = HistoryEntry.from_dict(raw)
                except (TypeError, ValueError):
                    continue
                if not entry.id:
                    continue
                # 进程重启后不可能还有任务在跑：把遗留的 running 标成中断，
                # 否则历史面板会永远显示「进行中」且取消按钮点了没反应。
                if entry.status == "running":
                    entry.status = "error"
                    entry.error = entry.error or "下载未完成（程序已退出或重启）"
                    entry.message = entry.message or "已中断"
                    if entry.finished is None:
                        entry.finished = entry.created
                    recovered += 1
                self._entries[entry.id] = entry
                self._order.append(entry.id)
            if recovered:
                # 立刻落盘，避免下次启动又重复「恢复」
                try:
                    payload = {
                        "version": 1,
                        "entries": [
                            self._entries[i].to_dict()
                            for i in self._order if i in self._entries
                        ],
                    }
                    with open(self.path, "w", encoding="utf-8") as fh:
                        json.dump(payload, fh, ensure_ascii=False, indent=1)
                except OSError:
                    pass

    def recover_stale(self) -> int:
        """把遗留的 running 记录标记为中断，返回处理条数。"""
        count = 0
        with self._lock:
            for entry in self._entries.values():
                if entry.status == "running":
                    entry.status = "error"
                    entry.error = entry.error or "下载未完成（程序已退出或重启）"
                    entry.message = entry.message or "已中断"
                    count += 1
        if count:
            self.save()
        return count

    def save(self) -> None:
        """把历史写入磁盘（失败不影响下载）。"""
        with self._lock:
            payload = {
                "version": 1,
                "entries": [self._entries[i].to_dict() for i in self._order if i in self._entries],
            }
            tmp = self.path + ".tmp"
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except OSError:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass

    # ---- 增删改查 ----------------------------------------------------

    def add(self, entry: HistoryEntry) -> HistoryEntry:
        with self._lock:
            self._entries[entry.id] = entry
            self._order.insert(0, entry.id)          # 最新的排最前
            # 裁剪过旧记录
            while len(self._order) > MAX_HISTORY:
                old = self._order.pop()
                self._entries.pop(old, None)
        self.save()
        return entry

    def get(self, entry_id: str) -> Optional[HistoryEntry]:
        with self._lock:
            return self._entries.get(entry_id)

    def update(self, entry_id: str, **fields: Any) -> Optional[HistoryEntry]:
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                return None
            for key, value in fields.items():
                if hasattr(entry, key):
                    setattr(entry, key, value)
        self.save()
        return entry

    def append_file(self, entry_id: str, file_info: Dict[str, Any]) -> None:
        """追加一个已完成的文件记录。

        为降低磁盘写入频率，这里只在每追加若干条时落盘一次。
        """
        count = 0
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                return
            entry.files.append(file_info)
            count = len(entry.files)
        # 前几条必存（让历史尽快可见），之后每 5 条存一次
        if count <= 3 or count % 5 == 0:
            self.save()

    def list(self, limit: int = MAX_HISTORY) -> List[HistoryEntry]:
        with self._lock:
            out = [self._entries[i] for i in self._order if i in self._entries]
        return out[:limit]

    def remove(self, entry_id: str) -> bool:
        with self._lock:
            if entry_id not in self._entries:
                return False
            self._entries.pop(entry_id, None)
            if entry_id in self._order:
                self._order.remove(entry_id)
        self.save()
        return True

    def clear(self) -> int:
        with self._lock:
            count = len(self._order)
            self._entries.clear()
            self._order.clear()
        self.save()
        return count


__all__ = [
    "HistoryEntry",
    "HistoryStore",
    "MAX_HISTORY",
    "STAMP_FORMAT",
    "session_dir",
    "timestamp_name",
]
