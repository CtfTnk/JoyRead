"""Background task service for UI-safe work dispatch."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from itertools import count
from typing import Callable, Generic, TypeVar

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal as QtSignal

from joyread.infrastructure.logging import describe_callback

T = TypeVar("T")
I = TypeVar("I")

logger = logging.getLogger(__name__)


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskPriority(IntEnum):
    """Relative QThreadPool priorities used by interactive workflows."""

    BACKGROUND = -100
    LOW = -10
    NORMAL = 0
    HIGH = 10
    CRITICAL = 100


@dataclass
class TaskHandle(Generic[T]):
    """Mutable status object shared between the submitter and QRunnable.

    Cancellation is cooperative. The worker checks the status before emitting
    results, and receiver callbacks check it again before mutating ViewModel
    state.
    """

    task_id: str
    callback_label: str = ""
    status: TaskStatus = TaskStatus.PENDING
    result: T | None = None
    error: Exception | None = None
    _signals: QObject | None = field(default=None, repr=False)

    def cancel(self) -> None:
        if self.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            self.status = TaskStatus.CANCELLED


class _TaskSignals(QObject):
    item = QtSignal(object)
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
        callback_label = self._handle.callback_label or "<unknown>"
        if self._handle.status == TaskStatus.CANCELLED:
            logger.debug(
                "Task %s skipped callback=%s (cancelled before run)",
                self._handle.task_id,
                callback_label,
            )
            _safe_emit(self._signals.finished, context=f"task={self._handle.task_id} callback={callback_label}")
            return
        logger.debug("Task %s starting callback=%s", self._handle.task_id, callback_label)
        start = time.perf_counter()
        try:
            result = self._callback()
        except Exception as exc:  # pragma: no cover - exact task failures are callback-specific.
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
                _safe_emit(
                    self._signals.failed,
                    exc,
                    context=f"task={self._handle.task_id} callback={callback_label}",
                )
            else:
                logger.debug(
                    "Task %s callback=%s raised after cancellation (suppressed): %s",
                    self._handle.task_id,
                    callback_label,
                    exc,
                )
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
            _safe_emit(
                self._signals.completed,
                result,
                context=f"task={self._handle.task_id} callback={callback_label}",
            )
        else:
            logger.debug(
                "Task %s completed callback=%s in %.0f ms but was cancelled; dropping result",
                self._handle.task_id,
                callback_label,
                elapsed_ms,
            )
        _safe_emit(self._signals.finished, context=f"task={self._handle.task_id} callback={callback_label}")


class TaskService:
    """Runs expensive service work off the Qt UI thread.

    ViewModels submit business/service callbacks here when work may touch disk,
    decode images, query SQLite indirectly, or perform other slow operations.
    ``on_success``/``on_failure`` callbacks are delivered through Qt signals so
    UI-facing state changes happen from the receiver side of the Qt event loop.
    """

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
        priority: TaskPriority | int = TaskPriority.NORMAL,
    ) -> TaskHandle[T]:
        return self._submit(
            name,
            callback,
            on_success=on_success,
            on_failure=on_failure,
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
        """Run one worker that can publish bounded intermediate results.

        Archive extraction may need to stay batched for efficiency, while the
        UI should paint each rendered thumbnail as soon as it is available.
        The progress signal bridges those requirements without creating one
        QRunnable per page.
        """

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
        on_item: Callable[[object], None] | None = None,
        priority: TaskPriority | int = TaskPriority.NORMAL,
        signals: _TaskSignals | None = None,
    ) -> TaskHandle[T]:
        callback_label = describe_callback(callback)
        success_label = describe_callback(on_success) if on_success is not None else None
        failure_label = describe_callback(on_failure) if on_failure is not None else None
        handle: TaskHandle[T] = TaskHandle(
            task_id=f"{name}-{next(self._ids)}",
            callback_label=callback_label,
        )
        if self._shutting_down:
            # Hand back a pre-cancelled handle rather than raising. Shutdown
            # happens while many ViewModels are still emitting submits in
            # response to teardown signals; making every caller handle a new
            # exception path during shutdown would be all-pain-no-gain.
            logger.debug(
                "Task %s rejected callback=%s: service shutting down",
                handle.task_id,
                callback_label,
            )
            handle.status = TaskStatus.CANCELLED
            return handle
        logger.debug(
            "Task %s submitted callback=%s on_success=%s on_failure=%s",
            handle.task_id,
            callback_label,
            success_label or "<none>",
            failure_label or "<none>",
        )
        signals = signals or _TaskSignals()
        handle._signals = signals
        self._active_signals.add(signals)
        self._active_handles[handle.task_id] = handle  # type: ignore[assignment]

        def complete(result: object) -> None:
            if handle.status == TaskStatus.CANCELLED:
                logger.debug(
                    "Task %s result callback=%s dropped on receiver thread (cancelled)",
                    handle.task_id,
                    callback_label,
                )
                return
            handle.result = result  # type: ignore[assignment]
            handle.status = TaskStatus.COMPLETED
            if on_success is not None:
                logger.debug("Task %s invoking on_success=%s", handle.task_id, success_label)
                on_success(result)  # type: ignore[arg-type]

        def fail(error: object) -> None:
            if handle.status == TaskStatus.CANCELLED:
                logger.debug(
                    "Task %s failure callback=%s dropped on receiver thread (cancelled)",
                    handle.task_id,
                    callback_label,
                )
                return
            handle.error = error if isinstance(error, Exception) else Exception(str(error))
            handle.status = TaskStatus.FAILED
            if on_failure is not None:
                logger.debug("Task %s invoking on_failure=%s", handle.task_id, failure_label)
                on_failure(handle.error)

        def cleanup() -> None:
            self._active_signals.discard(signals)
            self._active_handles.pop(handle.task_id, None)
            handle._signals = None

        signals.completed.connect(complete)
        signals.failed.connect(fail)
        signals.finished.connect(cleanup)
        if on_item is not None:
            signals.item.connect(
                lambda item: on_item(item) if handle.status != TaskStatus.CANCELLED else None
            )

        handle.status = TaskStatus.RUNNING
        self._pool.start(_Runnable(handle, callback, signals), int(priority))
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
        callback_label = describe_callback(callback) if callback is not None else "<none>"
        handle: TaskHandle[T] = TaskHandle(task_id=f"{name}-{next(self._ids)}", callback_label=callback_label)
        if callback is None:
            return handle
        logger.debug("Task %s placeholder running callback=%s", handle.task_id, callback_label)
        handle.status = TaskStatus.RUNNING
        try:
            handle.result = callback()
            handle.status = TaskStatus.COMPLETED
        except Exception as exc:  # pragma: no cover - defensive compatibility path.
            handle.error = exc
            handle.status = TaskStatus.FAILED
        return handle


def _safe_emit(signal, *args: object, context: str = "") -> None:  # noqa: ANN001
    try:
        signal.emit(*args)
    except RuntimeError as exc:
        # A window can be closed while a background QRunnable is finishing.
        # Dropping the late signal is safer than letting shutdown surface a Qt wrapper error.
        logger.warning("Dropping late task signal%s: %s", f" ({context})" if context else "", exc)
        return
