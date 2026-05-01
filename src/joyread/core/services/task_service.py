"""Placeholder background task service contract for future async work."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from itertools import count
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskHandle(Generic[T]):
    task_id: str
    status: TaskStatus = TaskStatus.PENDING
    result: T | None = None
    error: Exception | None = None

    def cancel(self) -> None:
        if self.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            self.status = TaskStatus.CANCELLED


class TaskService:
    """Synchronous placeholder; future implementation can wrap QThreadPool."""

    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers
        self._ids = count(1)

    def submit_placeholder(self, name: str, callback: Callable[[], T] | None = None) -> TaskHandle[T]:
        handle: TaskHandle[T] = TaskHandle(task_id=f"{name}-{next(self._ids)}")
        if callback is None:
            return handle
        handle.status = TaskStatus.RUNNING
        try:
            handle.result = callback()
            handle.status = TaskStatus.COMPLETED
        except Exception as exc:  # pragma: no cover - defensive placeholder path.
            handle.error = exc
            handle.status = TaskStatus.FAILED
        return handle
