"""Qt thread-pool adapter for application task contracts."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from itertools import count
from typing import TypeVar

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal as QtSignal

from joyread.core.operation_context import bind_operation, create_operation
from joyread.app.tasking import TaskHandle, TaskPriority, TaskStatus
from joyread.infrastructure.logging import describe_callback, log_event


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
        failure_is_handled: bool = False,
    ) -> None:
        super().__init__()
        self._handle = handle
        self._callback = callback
        self._signals = signals
        self._on_discard = on_discard
        self._failure_is_handled = failure_is_handled

    def run(self) -> None:
        callback_label = self._handle.callback_label or "<unknown>"
        with bind_operation(self._handle.operation_context):
            if self._handle.status == TaskStatus.CANCELLED:
                self._log_cancelled(callback_label, duration_ms=0.0)
                _safe_emit(
                    self._signals.finished,
                    context=f"task={self._handle.task_id} callback={callback_label}",
                )
                return
            log_event(
                logger,
                logging.DEBUG,
                "task.worker.started",
                "Background task worker started",
                category="task",
                status="started",
                task_id=self._handle.task_id,
                callback=callback_label,
            )
            start = time.perf_counter()
            try:
                result = self._callback()
            except Exception as exc:  # pragma: no cover - callback-specific failures.
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                if self._handle.status != TaskStatus.CANCELLED:
                    # A registered failure callback owns business-level severity
                    # and presentation. Tasks without one have no other boundary,
                    # so retain the traceback here instead of silently losing it.
                    failure_kind = getattr(exc, "task_failure_kind", "unexpected")
                    if not self._failure_is_handled:
                        level = logging.ERROR
                    elif failure_kind == "expected":
                        level = logging.INFO
                    elif failure_kind == "cancelled":
                        level = logging.INFO
                    elif failure_kind == "controlled":
                        level = logging.WARNING
                    else:
                        level = logging.ERROR
                    log_event(
                        logger,
                        level,
                        "task.worker.failed",
                        "Background task worker failed",
                        category="task",
                        status="cancelled" if failure_kind == "cancelled" else "failed",
                        task_id=self._handle.task_id,
                        callback=callback_label,
                        duration_ms=round(elapsed_ms, 3),
                        error_type=type(exc).__name__,
                        reason=str(exc),
                        # Expected/controlled failures are already described by
                        # their typed error. Unexpected failures keep the worker
                        # traceback even when a UI callback will present them.
                        exc_info=failure_kind == "unexpected",
                    )
                    _safe_emit(
                        self._signals.failed,
                        exc,
                        context=f"task={self._handle.task_id} callback={callback_label}",
                    )
                else:
                    self._log_cancelled(callback_label, duration_ms=elapsed_ms)
                _safe_emit(
                    self._signals.finished,
                    context=f"task={self._handle.task_id} callback={callback_label}",
                )
                return
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if self._handle.status != TaskStatus.CANCELLED:
                log_event(
                    logger,
                    logging.DEBUG,
                    "task.worker.finished",
                    "Background task worker finished",
                    category="task",
                    status="finished",
                    task_id=self._handle.task_id,
                    callback=callback_label,
                    duration_ms=round(elapsed_ms, 3),
                )
                _safe_emit(
                    self._signals.completed,
                    result,
                    context=f"task={self._handle.task_id} callback={callback_label}",
                )
            elif self._on_discard is not None:
                try:
                    self._on_discard(result)
                except Exception as exc:  # Cleanup must not strand TaskService state.
                    log_event(
                        logger,
                        logging.WARNING,
                        "task.discard_cleanup.failed",
                        "Discard cleanup failed",
                        category="task",
                        status="failed",
                        task_id=self._handle.task_id,
                        error_type=type(exc).__name__,
                        exc_info=True,
                    )
                self._log_cancelled(callback_label, duration_ms=elapsed_ms)
            else:
                self._log_cancelled(callback_label, duration_ms=elapsed_ms)
            _safe_emit(
                self._signals.finished,
                context=f"task={self._handle.task_id} callback={callback_label}",
            )

    def _log_cancelled(self, callback_label: str, *, duration_ms: float) -> None:
        log_event(
            logger,
            logging.DEBUG,
            "task.worker.cancelled",
            "Background task worker cancelled",
            category="task",
            status="cancelled",
            task_id=self._handle.task_id,
            callback=callback_label,
            duration_ms=round(duration_ms, 3),
        )


class TaskService:
    """Execute application tasks on ``QThreadPool`` and marshal results by signal."""

    def __init__(self, max_workers: int, thread_pool: QThreadPool | None = None) -> None:
        self.max_workers = max_workers
        self._ids = count(1)
        self._pool = thread_pool or QThreadPool()
        self._pool.setMaxThreadCount(max_workers)
        self._active_signals: set[_TaskSignals] = set()
        self._active_handles: dict[str, TaskHandle[object]] = {}
        # Terminal teardown and a reversible storage-transition quiesce both
        # stop submission, but only the second one can be undone.
        self._shutting_down = False
        self._quiesced = False

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
        operation = create_operation(f"task.{name}", category="task")
        handle: TaskHandle[T] = TaskHandle(
            task_id=f"{name}-{next(self._ids)}",
            callback_label=callback_label,
            operation_context=operation,
        )
        if self._shutting_down or self._quiesced:
            handle.status = TaskStatus.CANCELLED
            return handle
        with bind_operation(operation):
            log_event(
                logger,
                logging.DEBUG,
                "task.submitted",
                "Background task submitted",
                category="task",
                status="started",
                task_id=handle.task_id,
                callback=callback_label,
                priority=int(priority),
            )
        signals = signals or _TaskSignals()
        handle._signals = signals
        self._active_signals.add(signals)
        self._active_handles[handle.task_id] = handle  # type: ignore[assignment]

        def complete(result: object) -> None:
            with bind_operation(handle.operation_context):
                if handle.status == TaskStatus.CANCELLED:
                    return
                handle.result = result  # type: ignore[assignment]
                handle.status = TaskStatus.COMPLETED
                if on_success is not None:
                    _invoke_ui_callback(handle, "success", on_success, result)

        def fail(error: object) -> None:
            with bind_operation(handle.operation_context):
                if handle.status == TaskStatus.CANCELLED:
                    return
                handle.error = error if isinstance(error, Exception) else Exception(str(error))
                handle.status = TaskStatus.FAILED
                if on_failure is not None:
                    _invoke_ui_callback(handle, "failure", on_failure, handle.error)

        def cleanup() -> None:
            with bind_operation(handle.operation_context):
                self._active_signals.discard(signals)
                self._active_handles.pop(handle.task_id, None)
                handle._signals = None

        signals.completed.connect(complete)
        signals.failed.connect(fail)
        signals.finished.connect(cleanup)
        if on_item is not None:
            signals.item.connect(
                lambda item: (
                    _invoke_stream_callback(handle, on_item, item)
                    if handle.status != TaskStatus.CANCELLED
                    else None
                )
            )
        handle.status = TaskStatus.RUNNING
        self._pool.start(
            _Runnable(
                handle,
                callback,
                signals,
                on_discard,
                failure_is_handled=on_failure is not None,
            ),
            int(priority),
        )
        return handle

    def quiesce(self) -> int:
        """Stop accepting work and cancel what is running, reversibly.

        Returns the number of tasks still to unwind. This does **not** join:
        a handle leaves ``_active_handles`` from its ``finished`` slot, which
        runs on the GUI thread, so a caller that blocked here waiting for the
        count to reach zero would deadlock against the very event loop that
        clears it. Callers poll :meth:`pending_task_count` from the event loop
        instead.

        The queued runnables are deliberately **not** cleared out of the pool.
        ``QThreadPool.clear()`` drops them before they can run, so they never
        reach ``_Runnable.run()``, never emit ``finished``, and their handles
        stay registered forever -- leaving a drain that can never reach zero.
        Letting a cancelled runnable start instead costs nothing: it sees the
        cancelled status, emits ``finished``, and returns without calling its
        callback.

        Unlike :meth:`shutdown` this is undone by :meth:`resume`, so it suits a
        storage transition, after which the application keeps running.
        """

        self._quiesced = True
        for handle in list(self._active_handles.values()):
            handle.cancel()
        pending = len(self._active_handles)
        logger.info("TaskService quiesced with %d task(s) unwinding", pending)
        return pending

    def pending_task_count(self) -> int:
        """How many submitted tasks have not yet reported completion."""

        return len(self._active_handles)

    def resume(self) -> None:
        """Accept work again after a quiesce.

        A shutdown is terminal and stays that way: reopening a pool whose
        services have already been torn down would let work run against them.
        """

        if self._shutting_down:
            logger.warning("Ignoring resume after shutdown")
            return
        self._quiesced = False
        logger.info("TaskService resumed")

    def shutdown(self, timeout_ms: int = 1500) -> None:
        # Terminal, and separately tracked so `resume` cannot undo it. Clearing
        # the pool is safe here precisely because nothing afterwards needs the
        # drain to reach zero -- the handles are force-cleared below.
        self._shutting_down = True
        self.quiesce()
        self._pool.clear()
        drained = self._pool.waitForDone(max(0, int(timeout_ms)))
        pending = len(self._active_handles)
        for handle in list(self._active_handles.values()):
            handle.cancel()
            handle._signals = None
        self._active_handles.clear()
        self._active_signals.clear()
        log_event(
            logger,
            logging.INFO if drained else logging.WARNING,
            "task.service.shutdown",
            "TaskService shutdown finished" if drained else "TaskService shutdown timed out",
            category="task",
            status="finished" if drained else "timed_out",
            count=pending,
        )

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
            log_event(
                logger,
                logging.ERROR,
                "task.placeholder.failed",
                "Synchronous compatibility task failed",
                category="task",
                status="failed",
                task_id=handle.task_id,
                error_type=type(exc).__name__,
                exc_info=True,
            )
        return handle


def _safe_emit(signal, *args: object, context: str = "") -> None:  # noqa: ANN001
    try:
        signal.emit(*args)
    except RuntimeError as exc:
        logger.warning("Dropping late task signal%s: %s", f" ({context})" if context else "", exc)


def _invoke_ui_callback(
    handle: TaskHandle[object],
    callback_kind: str,
    callback: Callable[[object], None],
    value: object,
) -> None:
    try:
        callback(value)
    except Exception as exc:
        if callback_kind != "failure":
            handle.error = exc
        handle.status = TaskStatus.FAILED
        log_event(
            logger,
            logging.ERROR,
            "task.ui_callback.failed",
            "Task GUI callback failed",
            category="task",
            status="failed",
            task_id=handle.task_id,
            outcome=callback_kind,
            error_type=type(exc).__name__,
            exc_info=True,
        )


def _invoke_stream_callback(
    handle: TaskHandle[object],
    callback: Callable[[object], None],
    item: object,
) -> None:
    with bind_operation(handle.operation_context):
        try:
            callback(item)
        except Exception as exc:
            handle.error = exc
            handle.cancel()
            log_event(
                logger,
                logging.ERROR,
                "task.stream_callback.failed",
                "Task stream-item GUI callback failed",
                category="task",
                status="failed",
                task_id=handle.task_id,
                error_type=type(exc).__name__,
                exc_info=True,
            )
