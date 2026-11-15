"""下载任务协调器（阶段 1 / A02）。

所有下载入口（手动单条、批量、定时扫描、自动洗版）只提交 DownloadCommand，
由协调器统一认领并串行执行：

- 以 BVID 为幂等键：同一 BVID 在排队或执行期间只允许一个任务；
- 单 worker 串行下载，避免多入口并发处理同一视频、加剧 B 站风控；
- 任务状态：queued / running / succeeded / failed / skipped；
- 任务来源：manual / batch / scheduled_scan / rewash。

当前部署是单进程 uvicorn（见 docker-compose.yml），所以任务状态保存在
进程内。若将来改为多 worker 或多实例部署，需要把认领与任务状态持久化
（数据库任务表或专用队列），不能直接复用本模块的内存状态。

第一阶段只实现去重（BVID 认领）与 FIFO 串行执行；任务取消、优先级和
自动重试尚未提供——失败任务由下一次扫描或用户重新投递来重试。

本模块不依赖 Scanner、FastAPI 或 yt-dlp，便于单独测试。
"""

from __future__ import annotations

import itertools
import re
import threading
import time
from dataclasses import dataclass, field
from queue import Queue
from typing import Callable, Optional

BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_SKIPPED = "skipped"

FINAL_STATES = (STATE_SUCCEEDED, STATE_FAILED, STATE_SKIPPED)

SOURCE_MANUAL = "manual"
SOURCE_BATCH = "batch"
SOURCE_SCHEDULED_SCAN = "scheduled_scan"
SOURCE_REWASH = "rewash"


@dataclass
class DownloadCommand:
    """一次下载请求。所有入口都转换成该对象后提交给协调器。"""

    bvid: str
    source: str = SOURCE_MANUAL
    scope: Optional[str] = None            # 'full' | 'mc' | None（沿用配置）
    group_code: Optional[str] = None       # 分团身份，贯穿目录、NFO、历史与查询
    include_disabled: bool = True          # 是否允许匹配未开启自动入库的队伍
    force: bool = False                    # 跳过“已下载”判断（洗版/强制重下）
    preserve_existing: Optional[bool] = None
    title: str = ""
    context: dict = field(default_factory=dict)

    def __post_init__(self):
        if not BVID_RE.match(self.bvid or ""):
            raise ValueError(f"无效的 BVID: {self.bvid!r}")
        # 强制重下意味着目标目录里可能存在旧文件，失败时必须保留旧媒体
        if self.preserve_existing is None:
            self.preserve_existing = bool(self.force)


@dataclass
class DownloadTask:
    id: int
    command: DownloadCommand
    state: str = STATE_QUEUED
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def queue_item(self) -> dict:
        """前端「排队中」列表所需的字段（保持旧接口结构）。"""
        return {
            "id": self.id,
            "bvid": self.command.bvid,
            "title": self.command.title or self.command.bvid,
            "scope": self.command.scope or "auto",
            "force": self.command.force,
            "group": self.command.group_code or "",
        }

    def status_item(self) -> dict:
        return {
            "id": self.id,
            "bvid": self.command.bvid,
            "source": self.command.source,
            "state": self.state,
            "error": self.error,
            "title": self.command.title,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# runner 返回 False 表示任务失败；返回 True/None 表示执行完成
Runner = Callable[[DownloadCommand], Optional[bool]]


class DownloadCoordinator:
    """进程内任务协调器：BVID 幂等认领 + 单 worker 串行执行。"""

    def __init__(self, runner: Runner, is_downloaded: Callable[[str], bool],
                 keep_finished: int = 100):
        self._runner = runner
        self._is_downloaded = is_downloaded
        self._keep_finished = max(0, int(keep_finished))
        self._lock = threading.Lock()
        self._pending: "Queue[int]" = Queue()
        self._claimed: dict[str, int] = {}   # bvid -> task_id（排队 + 执行中）
        self._tasks: dict[int, DownloadTask] = {}
        self._seq = itertools.count(1)
        self._worker = threading.Thread(
            target=self._work, name="download-worker", daemon=True)
        self._worker.start()

    # ── 提交 ───────────────────────────────────────────────────────────
    def submit(self, command: DownloadCommand) -> Optional[DownloadTask]:
        """提交单个任务；同一 BVID 已在队列/执行中时返回 None。"""
        with self._lock:
            if command.bvid in self._claimed:
                return None
            task = self._accept_locked(command)
        self._pending.put(task.id)
        return task

    def submit_many(self, commands) -> tuple[list[DownloadTask], list[DownloadCommand]]:
        """批量提交；返回 (已认领任务, 因重复被跳过的命令)。"""
        accepted, duplicates = [], []
        with self._lock:
            for command in commands:
                if command.bvid in self._claimed:
                    duplicates.append(command)
                    continue
                accepted.append(self._accept_locked(command))
        for task in accepted:
            self._pending.put(task.id)
        return accepted, duplicates

    def _accept_locked(self, command: DownloadCommand) -> DownloadTask:
        task = DownloadTask(next(self._seq), command)
        self._claimed[command.bvid] = task.id
        self._tasks[task.id] = task
        self._prune_locked()
        return task

    def _prune_locked(self):
        finished = [tid for tid, task in self._tasks.items()
                    if task.state in FINAL_STATES]
        stale = finished[:max(0, len(finished) - self._keep_finished)]
        for task_id in stale:
            self._tasks.pop(task_id, None)

    # ── 查询 ───────────────────────────────────────────────────────────
    def is_claimed(self, bvid: str) -> bool:
        with self._lock:
            return bvid in self._claimed

    def queue_snapshot(self) -> list[dict]:
        """尚未开始执行的任务（按提交顺序）。"""
        with self._lock:
            queued = sorted(
                (task for task in self._tasks.values() if task.state == STATE_QUEUED),
                key=lambda task: task.id,
            )
            return [task.queue_item() for task in queued]

    def describe(self, task_id: int) -> Optional[dict]:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.status_item() if task else None

    def tasks_snapshot(self) -> list[dict]:
        with self._lock:
            return [task.status_item() for task in self._tasks.values()]

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """等待所有任务结束；返回是否在超时前清空（测试/运维用）。"""
        deadline = None if timeout is None else time.time() + timeout
        while True:
            with self._lock:
                if not self._claimed:
                    return True
            if deadline is not None and time.time() >= deadline:
                return False
            time.sleep(0.02)

    # ── 执行 ───────────────────────────────────────────────────────────
    def _work(self):
        while True:
            task_id = self._pending.get()
            with self._lock:
                task = self._tasks.get(task_id)
                if task is None:
                    self._pending.task_done()
                    continue
                task.state = STATE_RUNNING
                task.started_at = time.time()

            state, error = STATE_SUCCEEDED, ""
            try:
                if (not task.command.force
                        and self._is_downloaded(task.command.bvid)):
                    state = STATE_SKIPPED
                elif self._runner(task.command) is False:
                    state = STATE_FAILED
            except Exception as exc:                    # noqa: BLE001
                state = STATE_FAILED
                error = f"{type(exc).__name__}: {exc}"
            finally:
                with self._lock:
                    task.state = state
                    task.error = error[:500]
                    task.finished_at = time.time()
                    self._claimed.pop(task.command.bvid, None)
                    self._prune_locked()
                self._pending.task_done()
