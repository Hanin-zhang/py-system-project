"""时序任务注册表（进程内）。

刻意不上 Celery/Redis：单人项目里，任务状态只需要"能查、能重试、能超时"。
进程内注册表在单实例部署下完全够用，且调试时能直接看到内存状态。

已知局限（写进 docs/limitations.md）：
- 进程重启后任务记录丢失（结果文件仍在磁盘上，可用结果目录恢复）
- 不适合多实例横向扩容
这两点在 I6 引入 MySQL 后由 Java 侧接管任务元数据，计算侧只保留本表。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class TaskStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass
class TaskRecord:
    task_id: str
    status: TaskStatus
    created_at: float
    finished_at: float | None = None
    result_dir: str | None = None
    summary: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_detail: dict = field(default_factory=dict)
    scenario: dict = field(default_factory=dict)

    @property
    def elapsed_ms(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.created_at) * 1000

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "elapsed_ms": self.elapsed_ms,
            "result_dir": self.result_dir,
            "summary": self.summary,
            "error": (
                {
                    "code": self.error_code,
                    "message": self.error_message,
                    "detail": self.error_detail,
                }
                if self.error_code
                else None
            ),
            "scenario": self.scenario,
        }


class TaskRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskRecord] = {}

    def create(self, scenario: dict) -> TaskRecord:
        task_id = uuid.uuid4().hex[:16]
        rec = TaskRecord(
            task_id=task_id,
            status=TaskStatus.RUNNING,
            created_at=time.time(),
            scenario=scenario,
        )
        with self._lock:
            self._tasks[task_id] = rec
        return rec

    def succeed(self, task_id: str, *, result_dir: Path | str, summary: dict) -> None:
        with self._lock:
            rec = self._tasks[task_id]
            rec.status = TaskStatus.SUCCEEDED
            rec.finished_at = time.time()
            rec.result_dir = str(result_dir)
            rec.summary = summary

    def fail(
        self,
        task_id: str,
        *,
        code: str,
        message: str,
        detail: dict | None = None,
    ) -> None:
        with self._lock:
            rec = self._tasks[task_id]
            rec.status = TaskStatus.FAILED
            rec.finished_at = time.time()
            rec.error_code = code
            rec.error_message = message
            rec.error_detail = detail or {}

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self, limit: int = 50) -> list[TaskRecord]:
        with self._lock:
            recs = sorted(self._tasks.values(), key=lambda r: -r.created_at)
        return recs[:limit]

    def clear(self) -> None:
        """仅供测试使用。"""
        with self._lock:
            self._tasks.clear()


registry = TaskRegistry()
