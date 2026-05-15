"""Background task service for UI-safe work dispatch."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import count
from typing import Callable, Generic, TypeVar

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal as QtSignal


T = TypeVar("T")

logger = logging.getLogger(__name__)


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
            logger.debug("Task %s skipped (cancelled before run)", self._handle.task_id)
            _safe_emit(self._signals.finished)
            return
        logger.debug("Task %s starting", self._handle.task_id)
        start = time.perf_counter()
        try:
            result = self._callback()
        except Exception as exc:  # pragma: no cover - exact task failures are callback-specific.
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if self._handle.status != TaskStatus.CANCELLED:
                logger.error(
                    "Task %s failed after %.0f ms: %s",
                    self._handle.task_id,
                    elapsed_ms,
                    exc,
                    exc_info=True,
                )
                _safe_emit(self._signals.failed, exc)
            else:
                logger.debug(
                    "Task %s raised after cancellation (suppressed): %s",
                    self._handle.task_id,
                    exc,
                )
            _safe_emit(self._signals.finished)
            return

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if self._handle.status != TaskStatus.CANCELLED:
            logger.debug("Task %s completed in %.0f ms", self._handle.task_id, elapsed_ms)
            _safe_emit(self._signals.completed, result)
        else:
            logger.debug(
                "Task %s completed in %.0f ms but was cancelled; dropping result",
                self._handle.task_id,
                elapsed_ms,
            )
        _safe_emit(self._signals.finished)


class TaskService:
    """Runs expensive service work off the Qt UI thread."""

    def __init__(self, max_workers: int, thread_pool: QThreadPool | None = None) -> None:
        self.max_workers = max_workers
        self._ids = count(1)
        self._pool = thread_pool or QThreadPool()
        self._pool.setMaxThreadCount(max_workers)
        self._active_signals: set[_TaskSignals] = set()
        self._active_handles: dict[str, TaskHandle[object]] = {}
        self._shutting_down = False

    def submit(
        self,
        name: str,
        callback: Callable[[], T],
        *,
        on_success: Callable[[T], None] | None = None,
        on_failure: Callable[[Exception], None] | None = None,
    ) -> TaskHandle[T]:
        handle: TaskHandle[T] = TaskHandle(task_id=f"{name}-{next(self._ids)}")
        if self._shutting_down:
            # Hand back a pre-cancelled handle rather than raising. Shutdown
            # happens while many ViewModels are still emitting submits in
            # response to teardown signals; making every caller handle a new
            # exception path during shutdown would be all-pain-no-gain.
            logger.debug("Task %s rejected: service shutting down", handle.task_id)
            handle.status = TaskStatus.CANCELLED
            return handle
        logger.debug("Task %s submitted", handle.task_id)
        signals = _TaskSignals()
        handle._signals = signals
        self._active_signals.add(signals)
        self._active_handles[handle.task_id] = handle  # type: ignore[assignment]

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
            self._active_handles.pop(handle.task_id, None)
            handle._signals = None

        signals.completed.connect(complete)
        signals.failed.connect(fail)
        signals.finished.connect(cleanup)

        handle.status = TaskStatus.RUNNING
        self._pool.start(_Runnable(handle, callback, signals))
        return handle

    def shutdown(self, timeout_ms: int = 1500) -> None:
        """Cancel queued/running work and wait briefly for the thread pool.

        Qt cannot forcibly stop a QRunnable that is already inside user code,
        so cancellation is cooperative: active handles are marked cancelled and
        late success/failure signals are ignored. ``clear`` drops work that has
        not started yet, and ``waitForDone`` gives in-flight tasks a bounded
        chance to leave before application teardown continues.
        """

        active = len(self._active_handles)
        logger.info("TaskService shutdown: cancelling %d active task(s)", active)
        self._shutting_down = True
        for handle in list(self._active_handles.values()):
            handle.cancel()
        self._pool.clear()
        self._pool.waitForDone(max(0, int(timeout_ms)))
        for handle in list(self._active_handles.values()):
            handle.cancel()
            handle._signals = None
        self._active_handles.clear()
        self._active_signals.clear()
        logger.info("TaskService shutdown complete")

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


def _safe_emit(signal, *args: object) -> None:  # noqa: ANN001
    try:
        signal.emit(*args)
    except RuntimeError as exc:
        # A window can be closed while a background QRunnable is finishing.
        # Dropping the late signal is safer than letting shutdown surface a Qt wrapper error.
        logger.warning("Dropping late task signal: %s", exc)
        return
