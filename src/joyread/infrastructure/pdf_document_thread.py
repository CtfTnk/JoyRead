"""The one thread allowed to touch a ``QPdfDocument``.

``QPdfDocument`` is a ``QObject``. Qt guarantees reentrancy for distinct
instances, not for one instance shared across threads, and thread affinity
governs anything the object routes through the event system. Qt underlines the
point by shipping ``QPdfPageRenderer`` with an explicit ``MultiThreaded`` mode:
background rendering is expected to go through a renderer that owns its thread,
not through ad-hoc calls into a document from whichever pool worker got the
task.

So every document lives here. Creation, rendering, and disposal all run on one
long-lived ``QThread``, which gives three things at once:

* affinity is satisfied by construction -- no document is ever touched from a
  thread other than the one that created it;
* the thread runs an event loop, so ``deleteLater()`` is actually processed and
  a discarded document's memory is genuinely released;
* work is serialised in submission order, so a close queued behind renders
  cannot free a document out from under them.

Callers keep their synchronous port: :meth:`PdfDocumentThread.call` blocks its
caller, so production code invokes it from a task worker rather than the GUI
thread. :meth:`PdfDocumentThread.post` is fire-and-forget for teardown.
"""

from __future__ import annotations

import atexit
import logging
from collections.abc import Callable
from contextvars import Context, copy_context
from threading import Event, RLock
from typing import Any

from PySide6.QtCore import QObject, QThread, Qt, Signal as QtSignal, Slot


logger = logging.getLogger(__name__)

# A single page render is milliseconds. This bound only exists so a wedged or
# torn-down PDF thread surfaces as an error instead of hanging a pool worker.
PDF_CALL_TIMEOUT_SECONDS = 120.0
_SHUTDOWN_WAIT_MS = 5000


class PdfThreadError(RuntimeError):
    """Raised when the PDF thread cannot run or complete a request."""


class _PdfJob:
    """One unit of work plus the caller's completion handshake."""

    __slots__ = ("_call", "_context", "_done", "_result", "_error", "_report_unobserved")

    def __init__(self, call: Callable[[], Any], *, report_unobserved: bool = False) -> None:
        self._call = call
        self._context: Context = copy_context()
        self._done = Event()
        self._result: Any = None
        self._error: BaseException | None = None
        self._report_unobserved = report_unobserved

    def run(self) -> None:
        self._context.run(self._run_in_context)

    def _run_in_context(self) -> None:
        try:
            self._result = self._call()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread.
            self._error = exc
            if self._report_unobserved:
                logger.error(
                    "Asynchronous PDF-thread job failed",
                    exc_info=(type(exc), exc, exc.__traceback__),
                    extra={
                        "event": "pdf.thread.post.failed",
                        "category": "pdf",
                        "status": "failed",
                        "error_type": type(exc).__name__,
                    },
                )
        finally:
            self._done.set()

    def wait(self, timeout: float) -> Any:
        if not self._done.wait(timeout):
            raise PdfThreadError(f"PDF operation did not complete within {timeout:g}s.")
        if self._error is not None:
            raise self._error
        return self._result


class _PdfDispatcher(QObject):
    """Runs jobs on whichever thread this object lives on."""

    submitted = QtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        # Queued explicitly: emitting from a pool worker must hand the job to
        # the PDF thread rather than running it in the caller.
        self.submitted.connect(self._execute, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _execute(self, job: _PdfJob) -> None:
        job.run()


class PdfDocumentThread:
    """Owns the PDF thread and marshals work onto it."""

    def __init__(
        self,
        *,
        timeout_seconds: float = PDF_CALL_TIMEOUT_SECONDS,
        shutdown_wait_ms: int = _SHUTDOWN_WAIT_MS,
    ) -> None:
        self._timeout = timeout_seconds
        self._shutdown_wait_ms = max(0, int(shutdown_wait_ms))
        self._lock = RLock()
        self._thread: QThread | None = None
        self._dispatcher: _PdfDispatcher | None = None
        self._stopping = False
        self._shutdown_job: _PdfJob | None = None
        self._atexit_registered = False
        self._shutdown_hooks: list[Callable[[], None]] = []

    def add_shutdown_hook(self, hook: Callable[[], None]) -> None:
        """Register teardown that must run *on* the PDF thread before it stops.

        Anything cached on this thread for reuse -- a long-lived document, say
        -- has to be released here, or it would outlive the thread that owns it.
        """

        with self._lock:
            self._shutdown_hooks.append(hook)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.isRunning()

    def call(self, work: Callable[[], Any]) -> Any:
        """Run ``work`` on the PDF thread and return its result.

        Blocks the calling thread. Production call sites must already be on a
        task worker; this method must not be invoked from the GUI thread.
        """

        job = self._submit(work, start=True)
        if job is None:
            # Reached once shutdown has sealed the queue: the thread is still
            # draining accepted work and must not take on more.
            raise PdfThreadError("The PDF thread is shutting down.")
        return job.wait(self._timeout)

    def post(self, work: Callable[[], None]) -> None:
        """Queue ``work`` without waiting, for teardown paths.

        Ordering still holds: anything already queued runs first, so a posted
        close lands after the renders it must not disturb. If the thread is
        already stopped there is nothing left to release, so the work is
        dropped rather than resurrecting the thread during shutdown.
        """

        if self._submit(work, start=False) is None:
            logger.debug("Dropping PDF teardown work; the PDF thread is stopped or stopping")

    def shutdown(self) -> None:
        """Stop the thread. A later :meth:`call` starts a fresh one."""

        with self._lock:
            self._reap_stopped_locked()
            thread = self._thread
            dispatcher = self._dispatcher
            if thread is None or dispatcher is None:
                return
            if not self._stopping:
                self._stopping = True
                logger.info(
                    "PDF document thread shutdown started",
                    extra={
                        "event": "pdf.thread.shutdown.started",
                        "category": "pdf",
                        "status": "started",
                    },
                )
                hooks = tuple(self._shutdown_hooks)

                def finish_on_owner() -> None:
                    try:
                        for hook in hooks:
                            try:
                                hook()
                            except BaseException:  # Cleanup must not strand the QThread.
                                logger.exception("PDF thread shutdown hook failed")
                    finally:
                        # Both calls happen on the owning thread. deleteLater()
                        # is therefore serviced by this event loop as it exits.
                        dispatcher.deleteLater()
                        thread.quit()

                self._shutdown_job = _PdfJob(finish_on_owner, report_unobserved=True)
                # Emit while holding the lifecycle lock. Every accepted job is
                # therefore queued before this finalizer, and later submissions
                # observe _stopping and are rejected.
                dispatcher.submitted.emit(self._shutdown_job)

        if QThread.currentThread() is thread:
            # The queued finalizer runs after the current PDF job returns.
            return
        if not thread.wait(self._shutdown_wait_ms):
            # A running QThread must remain strongly owned. Dropping the last
            # wrapper here can abort the process with
            # "QThread: Destroyed while thread is still running".
            logger.warning(
                "PDF thread did not stop within %d ms; retaining it until its queued work drains",
                self._shutdown_wait_ms,
                extra={
                    "event": "pdf.thread.shutdown.timed_out",
                    "category": "pdf",
                    "status": "timed_out",
                    "duration_ms": float(self._shutdown_wait_ms),
                },
            )
            return
        with self._lock:
            self._reap_stopped_locked()
        logger.info(
            "PDF document thread shutdown finished",
            extra={
                "event": "pdf.thread.shutdown.finished",
                "category": "pdf",
                "status": "finished",
            },
        )

    def _submit(self, work: Callable[[], Any], *, start: bool) -> _PdfJob | None:
        job = _PdfJob(work, report_unobserved=not start)
        with self._lock:
            self._reap_stopped_locked()
            if self._stopping:
                return None
            dispatcher = self._ensure_started() if start else self._dispatcher
            if dispatcher is None:
                return None
            # Serialise submission with shutdown so the finalizer cannot
            # overtake work that has already been accepted.
            dispatcher.submitted.emit(job)
        return job

    def _ensure_started(self) -> _PdfDispatcher:
        if self._dispatcher is not None:
            return self._dispatcher
        thread = QThread()
        thread.setObjectName("JoyReadPdf")
        dispatcher = _PdfDispatcher()
        dispatcher.moveToThread(thread)
        # The default QThread.run() enters an event loop, which is what makes
        # queued jobs and deleteLater work on this thread.
        thread.start()
        self._thread = thread
        self._dispatcher = dispatcher
        if not self._atexit_registered:
            # AppContext.close() is the normal path. This only covers callers
            # that use the service without one -- tests, scripts -- so a live
            # QThread is never torn down by interpreter shutdown.
            atexit.register(self.shutdown)
            self._atexit_registered = True
        logger.info(
            "PDF document thread started",
            extra={
                "event": "pdf.thread.started",
                "category": "pdf",
                "status": "finished",
            },
        )
        return dispatcher

    def _reap_stopped_locked(self) -> None:
        """Release wrappers only after Qt confirms their thread has stopped."""

        thread = self._thread
        if thread is None or thread.isRunning():
            return
        self._thread = None
        self._dispatcher = None
        self._shutdown_job = None
        self._stopping = False


_pdf_thread = PdfDocumentThread()


def pdf_thread() -> PdfDocumentThread:
    return _pdf_thread


def shutdown_pdf_thread() -> None:
    """Stop the shared PDF thread. Safe to call when it never started."""

    _pdf_thread.shutdown()
