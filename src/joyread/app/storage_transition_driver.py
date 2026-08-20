"""Drives a storage transition through quiescence without freezing the UI.

The policy -- what order, how long to wait, when to give up -- lives in
:mod:`joyread.app.storage_transition`. This is the part that needs Qt: a timer
to poll from the event loop, and a thread to run the disk phase on.

Polling rather than joining is not a stylistic choice. ``TaskService`` clears a
handle from its active set in a slot that runs on the GUI thread, so a caller
that blocked the GUI thread waiting for that set to empty would be waiting on
itself.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import StrEnum, auto
import logging
import threading
from time import monotonic

from PySide6.QtCore import QObject, QTimer
from PySide6.QtCore import Signal as QtSignal

from joyread.core.operation_context import OperationContext, bind_operation, create_operation
from joyread.app.storage_transition import (
    DRAIN_TIMEOUT_MS,
    FLUSH_TIMEOUT_MS,
    QuiesceOutcome,
    evaluate_drain,
)
from joyread.app.tasking import TaskHandle, TaskStatus
from joyread.infrastructure.logging import log_event

logger = logging.getLogger(__name__)


class _Phase(StrEnum):
    IDLE = auto()
    FLUSHING = auto()
    DRAINING = auto()
    MIGRATING = auto()


def _is_settled(handle: TaskHandle[object]) -> bool:
    return handle.status not in {TaskStatus.PENDING, TaskStatus.RUNNING}


class StorageTransitionController(QObject):
    """Run one storage transition, start to finish, off the GUI thread.

    Emits exactly one terminal signal per run: ``finished`` when the disk phase
    returned (successfully or with a captured error inside the transition),
    ``failed`` when it raised, or ``abandoned`` when work would not stop.
    """

    #: The disk phase returned; the receiver must call
    #: ``AppContext.finish_storage_transition`` on the GUI thread.
    finished = QtSignal(object)
    #: The disk phase raised before producing a transition.
    failed = QtSignal(object)
    #: Quiescence could not be reached; storage was never touched. Carries the
    #: number of tasks still running when the deadline passed.
    abandoned = QtSignal(int)

    def __init__(
        self,
        context: object,
        window_manager: object,
        *,
        poll_interval_ms: int = 50,
        flush_timeout_ms: int = FLUSH_TIMEOUT_MS,
        drain_timeout_ms: int = DRAIN_TIMEOUT_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._context = context
        self._window_manager = window_manager
        self._flush_timeout_ms = flush_timeout_ms
        self._drain_timeout_ms = drain_timeout_ms
        self._phase = _Phase.IDLE
        self._operation: Callable[[], object] | None = None
        self._operation_context: OperationContext | None = None
        self._operation_started_at = 0.0
        self._terminal_status = "finished"
        self._started_at = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(max(1, int(poll_interval_ms)))
        self._timer.timeout.connect(self._poll)

    @property
    def busy(self) -> bool:
        return self._phase is not _Phase.IDLE

    @property
    def window_manager(self) -> object:
        """The manager whose Readers this transition will flush and close."""

        return self._window_manager

    def start(self, operation: Callable[[], object]) -> bool:
        """Begin a confirmed transition. ``operation`` runs the disk phase.

        The caller is responsible for having confirmed with the user and closed
        any editor first; by the time this runs the decision is made.
        """

        if self.busy:
            logger.warning("A storage transition is already running")
            return False
        self._operation_context = create_operation("storage.transition", category="storage")
        self._operation_started_at = monotonic()
        self._terminal_status = "finished"
        with bind_operation(self._operation_context):
            log_event(
                logger,
                logging.INFO,
                "storage.transition.started",
                "Storage transition started",
                category="storage",
                status="started",
            )
        self._operation = operation
        # Flush before anything is cancelled, and before the Readers close --
        # `ReaderViewModel.cancel()` cancels the progress handle, so closing
        # first would discard the very write being waited on.
        self._enter(_Phase.FLUSHING)
        # A Reader with nothing dirty yields no handles, so check immediately
        # rather than idling for a whole tick.
        self._poll()
        return True

    def _collect_flush_handles(self) -> tuple[TaskHandle[object], ...]:
        return reader_flush_handles(getattr(self._window_manager, "reader_windows", ()))

    def _enter(self, phase: _Phase) -> None:
        self._phase = phase
        self._started_at = monotonic()
        if not self._timer.isActive():
            self._timer.start()

    def _elapsed_ms(self) -> int:
        return int((monotonic() - self._started_at) * 1000)

    def _poll(self) -> None:
        if self._phase is _Phase.FLUSHING:
            self._poll_flush()
        elif self._phase is _Phase.DRAINING:
            self._poll_drain()

    def _poll_flush(self) -> None:
        # Re-asked every tick rather than captured once. Reader settings saves
        # are deliberately serialized -- a queued one is only submitted when
        # its predecessor completes -- so a single snapshot would stop waiting
        # at the first write and leave the newest one unwritten. Asking again
        # is safe because both save paths deduplicate.
        outstanding = [
            handle for handle in self._collect_flush_handles() if not _is_settled(handle)
        ]
        if outstanding and self._elapsed_ms() < self._flush_timeout_ms:
            return
        if outstanding:
            # Proceeding is the lesser evil: these are small progress writes,
            # and refusing the transition the user confirmed because one of
            # them is slow would be worse than losing the newest resume point.
            logger.warning(
                "Proceeding with %d reader write(s) unflushed after %d ms",
                len(outstanding),
                self._flush_timeout_ms,
            )
        # Quiesce first, then close. Closing a Reader does not release its
        # document synchronously -- it submits the close as a task -- so
        # closing first put that task straight into the set the quiesce then
        # cancelled, and the archive session was never closed at all. Sealed
        # first, the submission is refused and the pipeline closes the document
        # inline instead, which is what guarantees no handle is still open on
        # the storage root when the disk phase starts.
        self._context.quiesce_for_storage_transition()
        close_readers = getattr(self._window_manager, "close_all_readers", None)
        if callable(close_readers):
            close_readers()
        self._enter(_Phase.DRAINING)
        self._poll_drain()

    def _poll_drain(self) -> None:
        pending = self._context.storage_transition_pending_tasks()
        progress = evaluate_drain(
            pending,
            self._elapsed_ms(),
            timeout_ms=self._drain_timeout_ms,
        )
        if progress.outcome is QuiesceOutcome.WAITING:
            return
        if progress.outcome is QuiesceOutcome.TIMED_OUT:
            self._stop()
            self._context.abandon_storage_transition()
            with bind_operation(self._operation_context):
                log_event(
                    logger,
                    logging.WARNING,
                    "storage.transition.abandoned",
                    "Storage transition abandoned while draining background work",
                    category="storage",
                    status="timed_out",
                    count=progress.pending_tasks,
                    duration_ms=round((monotonic() - self._operation_started_at) * 1000.0, 3),
                )
            self._operation_context = None
            self.abandoned.emit(progress.pending_tasks)
            return
        self._stop()
        self._migrate()

    def _stop(self) -> None:
        self._timer.stop()
        self._phase = _Phase.IDLE

    def acknowledge(self) -> None:
        """Report that the GUI side has finished rebuilding.

        Until this is called the controller stays busy, so a second transition
        cannot start while the first still holds its maintenance lease and its
        services are half-rebuilt. The worker thread must not clear the phase
        itself: the terminal signal is queued, so the GUI-side handler has not
        run yet at the moment the worker would clear it.
        """

        with bind_operation(self._operation_context):
            log_event(
                logger,
                logging.INFO if self._terminal_status == "finished" else logging.WARNING,
                "storage.transition.finished",
                "Storage transition lifecycle finished",
                category="storage",
                status=self._terminal_status,
                duration_ms=round((monotonic() - self._operation_started_at) * 1000.0, 3),
            )
        self._operation_context = None
        self._phase = _Phase.IDLE

    def _migrate(self) -> None:
        operation = self._operation
        self._operation = None
        if operation is None:  # pragma: no cover - guarded by `busy`.
            return
        self._phase = _Phase.MIGRATING
        self._context.commit_storage_transition()
        operation_context = self._operation_context

        def run() -> None:
            # Deliberately not on the task service: it is quiesced, which is
            # the guarantee that nothing else can touch storage while this
            # runs. Qt queues these emissions back to the GUI thread, and the
            # phase stays MIGRATING until `acknowledge` says the rebuild is
            # done -- clearing it here would open a window where the first
            # transition owns the lease but a second one is accepted.
            with bind_operation(operation_context):
                try:
                    transition = operation()
                except Exception as error:  # noqa: BLE001 - reported to the user.
                    self._terminal_status = "failed"
                    log_event(
                        logger,
                        logging.ERROR,
                        "storage.transition.disk.failed",
                        "Storage transition disk phase failed",
                        category="storage",
                        status="failed",
                        error_type=type(error).__name__,
                        reason=str(error),
                        exc_info=True,
                    )
                    self.failed.emit(error)
                    return
                log_event(
                    logger,
                    logging.INFO,
                    "storage.transition.disk.finished",
                    "Storage transition disk phase finished",
                    category="storage",
                    status="finished",
                )
                self.finished.emit(transition)

        threading.Thread(
            target=run,
            name="joyread-storage-transition",
            daemon=True,
        ).start()


def reader_flush_handles(
    windows: Sequence[object],
) -> tuple[TaskHandle[object], ...]:
    """Ask every Reader to push out its durable writes, and return the handles.

    Reader kinds with no durable per-page state -- EPUB today -- have nothing
    to flush and contribute no handles.
    """

    handles: list[TaskHandle[object]] = []
    for window in windows:
        viewmodel = getattr(window, "viewmodel", None)
        flush = getattr(viewmodel, "flush_pending_writes", None)
        if not callable(flush):
            continue
        try:
            handles.extend(flush())
        except Exception:
            # Isolated per window on purpose. One Reader that cannot flush must
            # not cost the others theirs: a loop-level guard would discard
            # every handle collected so far, and the transition would stop
            # waiting for writes it told the user it would save.
            logger.exception("Reader flush failed during a storage transition")
    return tuple(handles)
