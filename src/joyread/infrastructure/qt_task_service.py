"""Qt thread-pool adapter for application task contracts."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from itertools import count
from typing import TypeVar

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal as QtSignal

from joyread.app.tasking import TaskHandle, TaskPriority, TaskStatus
from joyread.infrastructure.logging import describe_callback


T = TypeVar("T")
I = TypeVar("I")
logger = logging.getLogger(__name__)


class _TaskSignals(QObject):
    item = QtSignal(object)
    completed = QtSignal(object)
    failed = QtSignal(object)
    finished = QtSignal()


class _Runnable(QRunnable):
    def __init__(
        self,
        handle: TaskHandle[T],
        callback: Callable[[], T],
        signals: _TaskSignals,
        on_discard: Callable[[T], None] | None = None,
    ) -> None:
        super().__init__()
        self._handle = handle
        self._callback = callback
        self._signals = signals
        self._on_discard = on_discard

    def run(self) -> None:
        callback_label = self._handle.callback_label or "<unknown>"
        if self._handle.status == TaskStatus.CANCELLED:
            _safe_emit(self._signals.finished, context=f"task={self._handle.task_id} callback={callback_label}")
            return
        logger.debug("Task %s starting callback=%s", self._handle.task_id, callback_label)
        start = time.perf_counter()
        try:
            result = self._callback()
        except Exception as exc:  # pragma: no cover - callback-specific failures.
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if self._handle.status != TaskStatus.CANCELLED:
                logger.error(
                    "Task %s failed callback=%s after %.0f ms: %s",
                    self._handle.task_id,
                    callback_label,
                    elapsed_ms,
                    exc,
                    exc_info=True,
                )
                _safe_emit(self._signals.failed, exc, context=f"task={self._handle.task_id} callback={callback_label}")
            _safe_emit(self._signals.finished, context=f"task={self._handle.task_id} callback={callback_label}")
            return
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if self._handle.status != TaskStatus.CANCELLED:
            logger.debug(
                "Task %s completed callback=%s in %.0f ms",
                self._handle.task_id,
                callback_label,
                elapsed_ms,
            )
            _safe_emit(self._signals.completed, result, context=f"task={self._handle.task_id} callback={callback_label}")
        elif self._on_discard is not None:
            try:
                self._on_discard(result)
            except Exception as exc:  # Cleanup must not strand TaskService state.
                logger.warning(
                    "Discard cleanup failed task=%s callback=%s: %s",
                    self._handle.task_id,
                    callback_label,
                    exc,
                )
        _safe_emit(self._signals.finished, context=f"task={self._handle.task_id} callback={callback_label}")


class TaskService:
    """Execute application tasks on ``QThreadPool`` and marshal results by signal."""

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
        on_discard: Callable[[T], None] | None = None,
        priority: TaskPriority | int = TaskPriority.NORMAL,
    ) -> TaskHandle[T]:
        return self._submit(
            name,
            callback,
            on_success=on_success,
            on_failure=on_failure,
            on_discard=on_discard,
            priority=priority,
        )

    def submit_stream(
        self,
        name: str,
        callback: Callable[[Callable[[I], None]], T],
        *,
        on_item: Callable[[I], None],
        on_success: Callable[[T], None] | None = None,
        on_failure: Callable[[Exception], None] | None = None,
        priority: TaskPriority | int = TaskPriority.NORMAL,
    ) -> TaskHandle[T]:
        signals = _TaskSignals()

        def emit_item(item: I) -> None:
            _safe_emit(signals.item, item, context=f"task={name} stream-item")

        return self._submit(
            name,
            lambda: callback(emit_item),
            on_success=on_success,
            on_failure=on_failure,
            on_item=on_item,
            priority=priority,
            signals=signals,
        )

    def _submit(
        self,
        name: str,
        callback: Callable[[], T],
        *,
        on_success: Callable[[T], None] | None = None,
        on_failure: Callable[[Exception], None] | None = None,
        on_discard: Callable[[T], None] | None = None,
        on_item: Callable[[object], None] | None = None,
        priority: TaskPriority | int = TaskPriority.NORMAL,
        signals: _TaskSignals | None = None,
    ) -> TaskHandle[T]:
        callback_label = describe_callback(callback)
        handle: TaskHandle[T] = TaskHandle(task_id=f"{name}-{next(self._ids)}", callback_label=callback_label)
        if self._shutting_down:
            handle.status = TaskStatus.CANCELLED
            return handle
        logger.debug("Task %s submitted callback=%s", handle.task_id, callback_label)
        signals = signals or _TaskSignals()
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
        if on_item is not None:
            signals.item.connect(lambda item: on_item(item) if handle.status != TaskStatus.CANCELLED else None)
        handle.status = TaskStatus.RUNNING
        self._pool.start(_Runnable(handle, callback, signals, on_discard), int(priority))
        return handle

    def shutdown(self, timeout_ms: int = 1500) -> None:
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
        handle: TaskHandle[T] = TaskHandle(
            task_id=f"{name}-{next(self._ids)}",
            callback_label=describe_callback(callback) if callback is not None else "<none>",
        )
        if callback is None:
            return handle
        handle.status = TaskStatus.RUNNING
        try:
            handle.result = callback()
            handle.status = TaskStatus.COMPLETED
        except Exception as exc:  # pragma: no cover - compatibility path.
            handle.error = exc
            handle.status = TaskStatus.FAILED
        return handle


def _safe_emit(signal, *args: object, context: str = "") -> None:  # noqa: ANN001
    try:
        signal.emit(*args)
    except RuntimeError as exc:
        logger.warning("Dropping late task signal%s: %s", f" ({context})" if context else "", exc)
