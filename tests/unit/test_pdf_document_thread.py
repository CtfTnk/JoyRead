from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QThread
from PySide6.QtGui import QPainter, QPdfWriter

from joyread.app.reader_page_pipeline import ReaderPageRequest
from joyread.infrastructure.pdf_document_thread import (
    PdfDocumentThread,
    PdfThreadError,
    pdf_thread,
    shutdown_pdf_thread,
)
from joyread.infrastructure.pdf_image_service import PdfImageService


def _write_pdf(path: Path, pages: int = 3) -> None:
    writer = QPdfWriter(str(path))
    painter = QPainter(writer)
    for index in range(pages):
        if index:
            writer.newPage()
        painter.drawText(40, 80, f"page {index + 1}")
    painter.end()


def test_work_runs_on_the_pdf_thread_not_the_caller(qtbot) -> None:  # noqa: ANN001, ARG001
    worker = PdfDocumentThread()
    try:
        caller = QThread.currentThread()
        ran_on = worker.call(QThread.currentThread)

        assert ran_on is not caller
        assert worker.running
    finally:
        worker.shutdown()


def test_every_call_uses_the_same_thread(qtbot) -> None:  # noqa: ANN001, ARG001
    """Affinity only holds if one thread owns every document operation."""

    worker = PdfDocumentThread()
    try:
        threads = {worker.call(QThread.currentThread) for _ in range(5)}
        assert len(threads) == 1
    finally:
        worker.shutdown()


def test_exceptions_surface_on_the_calling_thread(qtbot) -> None:  # noqa: ANN001, ARG001
    worker = PdfDocumentThread()

    def boom() -> None:
        raise ValueError("render failed")

    try:
        try:
            worker.call(boom)
        except ValueError as exc:
            assert str(exc) == "render failed"
        else:  # pragma: no cover - the error must not be swallowed
            raise AssertionError("The PDF thread must re-raise on the caller.")
    finally:
        worker.shutdown()


def test_a_stalled_job_times_out_instead_of_hanging_the_caller(qtbot) -> None:  # noqa: ANN001, ARG001
    worker = PdfDocumentThread(timeout_seconds=0.2)
    release = threading.Event()
    try:
        try:
            worker.call(lambda: release.wait(30))
        except PdfThreadError:
            pass
        else:  # pragma: no cover - a wedged thread must not block forever
            raise AssertionError("A stalled PDF job must time out.")
    finally:
        release.set()
        worker.shutdown()


def test_posted_work_runs_in_order_behind_earlier_jobs(qtbot) -> None:  # noqa: ANN001, ARG001
    """Teardown queued after renders must not overtake them."""

    worker = PdfDocumentThread()
    order: list[str] = []
    finished = threading.Event()
    try:
        worker.call(lambda: None)  # a session always opens before it closes
        worker.post(lambda: order.append("first"))
        worker.post(lambda: order.append("second"))
        worker.post(lambda: (order.append("third"), finished.set()))
        assert finished.wait(10)
        assert order == ["first", "second", "third"]
    finally:
        worker.shutdown()


def test_posted_work_is_dropped_when_the_thread_is_stopped(qtbot) -> None:  # noqa: ANN001, ARG001
    """Teardown must not resurrect the thread during shutdown."""

    worker = PdfDocumentThread()
    worker.call(lambda: None)
    worker.shutdown()

    worker.post(lambda: None)

    assert not worker.running


def test_shutdown_is_idempotent_and_restartable(qtbot) -> None:  # noqa: ANN001, ARG001
    worker = PdfDocumentThread()
    try:
        worker.call(lambda: None)
        worker.shutdown()
        worker.shutdown()
        assert not worker.running

        assert worker.call(lambda: "restarted") == "restarted"
        assert worker.running
    finally:
        worker.shutdown()


def test_shutdown_hooks_run_on_the_owner_before_the_thread_stops(qtbot) -> None:  # noqa: ANN001, ARG001
    worker = PdfDocumentThread()
    owner = worker.call(QThread.currentThread)
    seen: list[QThread] = []
    worker.add_shutdown_hook(lambda: seen.append(QThread.currentThread()))

    worker.shutdown()

    assert seen == [owner]
    assert not worker.running


def test_shutdown_timeout_retains_the_running_thread_until_queued_work_drains(
    qtbot,  # noqa: ANN001, ARG001
) -> None:
    """A bounded shutdown must not destroy a QThread that is still running."""

    worker = PdfDocumentThread(shutdown_wait_ms=20)
    entered = threading.Event()
    release = threading.Event()
    owned_thread = None
    try:
        worker.call(lambda: None)
        owned_thread = worker._thread  # noqa: SLF001 - verify lifecycle ownership.

        def block() -> None:
            entered.set()
            release.wait(10)

        worker.post(block)
        assert entered.wait(2)

        worker.shutdown()

        assert worker.running
        assert worker._thread is owned_thread  # noqa: SLF001

        try:
            worker.call(lambda: None)
        except PdfThreadError:
            pass
        else:  # pragma: no cover - shutdown must seal the queue
            raise AssertionError("A stopping PDF thread must reject new work.")

        release.set()
        assert owned_thread is not None
        assert owned_thread.wait(2000)

        # The next call reaps the stopped wrappers and starts a fresh owner.
        assert worker.call(lambda: "restarted") == "restarted"
        assert worker._thread is not owned_thread  # noqa: SLF001
    finally:
        release.set()
        if owned_thread is not None:
            owned_thread.wait(2000)
        worker.shutdown()


def test_rendering_never_touches_the_document_from_another_thread(
    tmp_path: Path,
    qtbot,  # noqa: ANN001, ARG001
) -> None:
    """The whole point of the refactor: one owning thread for the document."""

    pdf_path = tmp_path / "affinity.pdf"
    _write_pdf(pdf_path, pages=3)
    session = PdfImageService().open(pdf_path)
    owner = pdf_thread().call(QThread.currentThread)
    seen: list[object] = []
    errors: list[str] = []

    original_render = session._document.render  # noqa: SLF001

    def recording_render(index, size):  # noqa: ANN001, ANN202
        seen.append(QThread.currentThread())
        return original_render(index, size)

    session._document.render = recording_render  # type: ignore[method-assign]  # noqa: SLF001

    def prepare(page: int) -> None:
        try:
            session.prepare_page(ReaderPageRequest(page, 400, 600, 1.0, 0))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    workers = [threading.Thread(target=prepare, args=(page,)) for page in range(3)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    session.close()

    assert errors == []
    assert len(seen) == 3
    assert all(thread is owner for thread in seen)


def test_close_does_not_block_the_caller(tmp_path: Path, qtbot) -> None:  # noqa: ANN001, ARG001
    """close() reaches us from Qt callbacks on the GUI thread."""

    pdf_path = tmp_path / "close.pdf"
    _write_pdf(pdf_path)
    session = PdfImageService().open(pdf_path)
    blocking = threading.Event()

    # Occupy the PDF thread so a blocking close would be visibly stuck.
    pdf_thread().post(lambda: blocking.wait(5))
    session.close()  # must return immediately, before the job above finishes

    assert not blocking.is_set()
    blocking.set()


def test_shutdown_releases_an_unclosed_session_on_the_document_owner(
    tmp_path: Path,
    qtbot,  # noqa: ANN001, ARG001
) -> None:
    """The finalizer covers a caller-side close that arrives after queue seal."""

    pdf_path = tmp_path / "shutdown.pdf"
    _write_pdf(pdf_path)
    session = PdfImageService().open(pdf_path)
    owner = pdf_thread().call(QThread.currentThread)
    document = session._document  # noqa: SLF001 - observe owner-thread disposal.
    closed_on: list[QThread] = []
    original_close = document.close

    def recording_close() -> None:
        closed_on.append(QThread.currentThread())
        original_close()

    document.close = recording_close  # type: ignore[method-assign]
    try:
        shutdown_pdf_thread()

        assert closed_on == [owner]
        assert not pdf_thread().running
    finally:
        session.close()
        shutdown_pdf_thread()


def test_a_closed_session_is_rejected_even_when_close_is_still_queued(
    tmp_path: Path,
    qtbot,  # noqa: ANN001, ARG001
) -> None:
    from joyread.core.reader.pdf import PdfReadError

    pdf_path = tmp_path / "queued.pdf"
    _write_pdf(pdf_path)
    session = PdfImageService().open(pdf_path)

    session.close()

    try:
        session.prepare_page(ReaderPageRequest(0, 400, 600, 1.0, 0))
    except PdfReadError:
        pass
    else:  # pragma: no cover - use-after-close must not render
        raise AssertionError("A closed session must not render.")
