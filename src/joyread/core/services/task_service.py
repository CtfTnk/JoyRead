"""Background task service for UI-safe work dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from itertools import count
from typing import Callable, Generic, TypeVar

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal as QtSignal


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
    _signals: QObject | None = field(default=None, repr=False)

    def cancel(self) -> None:
        if self.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            self.status = TaskStatus.CANCELLED


class _TaskSignals(QObject):
    completed = QtSignal(object)
    failed = QtSignal(object)
    finished = QtSignal()


class _Runnable(QRunnable):
    def __init__(self, handle: TaskHandle[T], callback: Callable[[], T], signals: _TaskSignals) -> None:
        super().__init__()
        self._handle = handle
        self._callback = callback
        self._signals = signals

    def run(self) -> None:
        if self._handle.status == TaskStatus.CANCELLED:
            self._signals.finished.emit()
            return
        try:
            result = self._callback()
        except Exception as exc:  # pragma: no cover - exact task failures are callback-specific.
            if self._handle.status != TaskStatus.CANCELLED:
                self._signals.failed.emit(exc)
            self._signals.finished.emit()
            return

        if self._handle.status != TaskStatus.CANCELLED:
            self._signals.completed.emit(result)
        self._signals.finished.emit()


class TaskService:
    """Runs expensive service work off the Qt UI thread."""

    def __init__(self, max_workers: int, thread_pool: QThreadPool | None = None) -> None:
        self.max_workers = max_workers
        self._ids = count(1)
        self._pool = thread_pool or QThreadPool()
        self._pool.setMaxThreadCount(max_workers)
        self._active_signals: set[_TaskSignals] = set()

    def submit(
        self,
        name: str,
        callback: Callable[[], T],
        *,
        on_success: Callable[[T], None] | None = None,
        on_failure: Callable[[Exception], None] | None = None,
    ) -> TaskHandle[T]:
        handle: TaskHandle[T] = TaskHandle(task_id=f"{name}-{next(self._ids)}")
        signals = _TaskSignals()
        handle._signals = signals
        self._active_signals.add(signals)

        def complete(result: object) -> None:
            if handle.status == TaskStatus.CANCELLED:
                return
            handle.result = result  # type: ignore[assignment]
            handle.status = TaskStatus.COMPLETED
            if on_success is not None:
                on_success(result)  # type: ignore[arg-type]

        def fail(error: object) -> None:
            if handle.status == TaskStatus.CANCELLED:
                return
            handle.error = error if isinstance(error, Exception) else Exception(str(error))
            handle.status = TaskStatus.FAILED
            if on_failure is not None:
                on_failure(handle.error)

        def cleanup() -> None:
            self._active_signals.discard(signals)
            handle._signals = None

        signals.completed.connect(complete)
        signals.failed.connect(fail)
        signals.finished.connect(cleanup)

        handle.status = TaskStatus.RUNNING
        self._pool.start(_Runnable(handle, callback, signals))
        return handle

    def submit_placeholder(self, name: str, callback: Callable[[], T] | None = None) -> TaskHandle[T]:
        handle: TaskHandle[T] = TaskHandle(task_id=f"{name}-{next(self._ids)}")
        if callback is None:
            return handle
        handle.status = TaskStatus.RUNNING
        try:
            handle.result = callback()
            handle.status = TaskStatus.COMPLETED
        except Exception as exc:  # pragma: no cover - defensive compatibility path.
            handle.error = exc
            handle.status = TaskStatus.FAILED
        return handle
