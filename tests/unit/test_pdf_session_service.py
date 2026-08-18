from __future__ import annotations

import threading
import time
from io import BytesIO
from pathlib import Path

from PIL import Image
from PIL import ImageDraw
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter, QPdfWriter

from joyread.app.reader_page_pipeline import ReaderPageRequest
from joyread.app.tasking import TaskPriority
from joyread.core.archive import ArchiveImageService
from joyread.core.reader import ReaderSessionService
from joyread.core.reader.pdf import PdfReadError
from joyread.infrastructure import pdf_image_service as pdf_module
from joyread.infrastructure.pdf_document_thread import PdfThreadError, pdf_thread
from joyread.infrastructure.pdf_image_service import PdfImageService, _normalize_rendered_pdf_png


def _write_pdf(path: Path, pages: int = 1) -> None:
    writer = QPdfWriter(str(path))
    painter = QPainter(writer)
    for index in range(pages):
        if index:
            writer.newPage()
        painter.drawText(40, 80, f"JoyRead PDF page {index + 1}")
    painter.end()


def _write_image_heavy_pdf(path: Path, pages: int = 1, side: int = 4000) -> None:
    """A page slow enough to measure, cheap enough for a test suite.

    A 4000x4000 embedded raster measures ~50ms to render on this class of
    hardware -- long enough to give a fine-grained heartbeat several samples
    to catch a stall, short enough that the test still runs in well under a
    second. Solid colour keeps the resulting file under 500KB even at 4000px.
    """

    image = QImage(side, side, QImage.Format.Format_RGB32)
    writer = QPdfWriter(str(path))
    painter = QPainter(writer)
    for index in range(pages):
        if index:
            writer.newPage()
        image.fill(0x100000 * (index + 1))
        painter.drawImage(QRectF(0, 0, writer.width(), writer.height()), image)
    painter.end()


def test_pdf_image_service_opens_counts_and_renders_pages(tmp_path: Path, qtbot) -> None:  # noqa: ARG001
    pdf_path = tmp_path / "sample.pdf"
    _write_pdf(pdf_path, pages=2)

    session = PdfImageService().open(pdf_path)
    first_page = session.get_page(0)

    assert session.page_count == 2
    assert session.get_dimensions(0) is not None
    assert first_page is not None
    assert first_page.page_index == 0
    with Image.open(BytesIO(first_page.image_bytes)) as image:
        assert image.width > 0
        assert image.height > 0


def test_pdf_image_service_preserves_page_box_by_default(
    tmp_path: Path,
    qtbot,  # noqa: ANN001, ARG001
    monkeypatch,
) -> None:
    pdf_path = tmp_path / "sample.pdf"
    _write_pdf(pdf_path)

    def fail_if_called(_payload: bytes) -> tuple[bytes, tuple[int, int]]:
        raise AssertionError("PDF margin normalization should be opt-in.")

    monkeypatch.setattr(pdf_module, "_normalize_rendered_pdf_png", fail_if_called)

    session = PdfImageService().open(pdf_path)
    page = session.get_page(0)

    assert page is not None
    assert page.dimensions == session.get_dimensions(0)


def test_pdf_image_service_can_opt_into_margin_normalization(
    tmp_path: Path,
    qtbot,  # noqa: ANN001, ARG001
    monkeypatch,
) -> None:
    pdf_path = tmp_path / "sample.pdf"
    _write_pdf(pdf_path)
    calls: list[bytes] = []

    def normalize(payload: bytes) -> tuple[bytes, tuple[int, int]]:
        calls.append(payload)
        return payload, (123, 456)

    monkeypatch.setattr(pdf_module, "_normalize_rendered_pdf_png", normalize)

    session = PdfImageService(normalize_margins=True).open(pdf_path)
    page = session.get_page(0)

    assert calls
    assert page is not None
    assert page.dimensions == (123, 456)


def test_reader_session_service_dispatches_pdf_documents(tmp_path: Path, qtbot) -> None:  # noqa: ARG001
    pdf_path = tmp_path / "sample.pdf"
    _write_pdf(pdf_path)
    service = ReaderSessionService(ArchiveImageService(), PdfImageService())

    session = service.open_document(pdf_path)
    pages = service.load_pages(session, (0,))

    assert session.page_count == 1
    assert list(pages) == [0]


def test_pdf_prepare_page_renders_viewport_qimage_without_png_roundtrip(
    tmp_path: Path,
    qtbot,  # noqa: ANN001, ARG001
    monkeypatch,
) -> None:
    pdf_path = tmp_path / "viewport.pdf"
    _write_pdf(pdf_path)
    session = PdfImageService().open(pdf_path)
    monkeypatch.setattr(
        pdf_module,
        "_qimage_png",
        lambda _image: (_ for _ in ()).throw(AssertionError("production render encoded PNG")),
    )

    prepared = session.prepare_page(
        ReaderPageRequest(0, 1600, 1200, 2.0, 9, TaskPriority.CRITICAL)
    )

    assert isinstance(prepared.frame, QImage)
    assert prepared.generation == 9
    assert prepared.rendered_dimensions == (prepared.frame.width(), prepared.frame.height())
    assert prepared.frame.devicePixelRatio() == 2.0
    assert prepared.frame.width() <= 1600
    assert prepared.frame.height() <= 1200


def test_pdf_prepare_page_clamps_long_edge_to_4096(tmp_path: Path, qtbot) -> None:  # noqa: ARG001
    pdf_path = tmp_path / "large-viewport.pdf"
    _write_pdf(pdf_path)
    session = PdfImageService().open(pdf_path)

    prepared = session.prepare_page(ReaderPageRequest(0, 20_000, 20_000, 1.0, 1))

    assert max(prepared.rendered_dimensions) == 4096


def test_pdf_margin_normalization_crops_asymmetric_white_margins() -> None:
    source = Image.new("RGBA", (400, 600), "white")
    draw = ImageDraw.Draw(source)
    draw.rectangle((110, 50, 378, 558), fill="black")
    payload = _png_bytes(source)

    normalized, dimensions = _normalize_rendered_pdf_png(payload)

    assert dimensions[0] < 400
    assert dimensions[1] < 600
    with Image.open(BytesIO(normalized)) as image:
        assert image.size == dimensions


def test_pdf_margin_normalization_keeps_full_bleed_pages() -> None:
    source = Image.new("RGBA", (400, 600), "black")
    payload = _png_bytes(source)

    normalized, dimensions = _normalize_rendered_pdf_png(payload)

    assert dimensions == (400, 600)
    assert normalized == payload


def test_pdf_margin_normalization_avoids_tiny_content_overcrop() -> None:
    source = Image.new("RGBA", (400, 600), "white")
    draw = ImageDraw.Draw(source)
    draw.rectangle((180, 280, 220, 310), fill="black")
    payload = _png_bytes(source)

    normalized, dimensions = _normalize_rendered_pdf_png(payload)

    assert dimensions == (400, 600)
    assert normalized == payload


def test_a_session_loads_its_container_once_and_reuses_it(
    tmp_path: Path,
    qtbot,  # noqa: ANN001, ARG001
    monkeypatch,
) -> None:
    """Rendering must not re-open the PDF.

    This is the memory guard for the PDF path, expressed as the invariant
    rather than as an RSS threshold. Re-loading per page cost about 45 percent
    of each page's time, and for PDFs whose cross-reference table makes pdfium
    buffer the whole file it also retained a file-size per page turn -- 4.7 GB
    on a 35 MB book in measurement.

    An RSS assertion cannot cover this: only some real-world containers trigger
    the retention, and no PDF that can be generated in a test does, so the
    threshold form passes vacuously. Counting loads catches the regression
    exactly and costs nothing.
    """

    pdf_path = tmp_path / "sample.pdf"
    _write_pdf(pdf_path, pages=4)

    loads: list[str] = []
    real_load = pdf_module._load_document

    def counting_load(path: Path):  # noqa: ANN202
        loads.append(str(path))
        return real_load(path)

    monkeypatch.setattr(pdf_module, "_load_document", counting_load)

    session = PdfImageService().open(pdf_path)
    try:
        for page in range(4):
            session.prepare_page(ReaderPageRequest(page, 400, 600, 1.0, 0))
    finally:
        session.close()

    assert loads == [str(pdf_path)]


def test_a_closed_session_refuses_to_render(tmp_path: Path, qtbot) -> None:  # noqa: ANN001, ARG001
    pdf_path = tmp_path / "sample.pdf"
    _write_pdf(pdf_path)
    session = PdfImageService().open(pdf_path)

    session.close()
    session.close()  # idempotent

    try:
        session.prepare_page(ReaderPageRequest(0, 400, 600, 1.0, 0))
    except PdfReadError:
        pass
    else:  # pragma: no cover - guard against a use-after-close regression
        raise AssertionError("A closed PDF session must not render.")


def test_a_render_does_not_hold_the_gil_for_another_python_thread(
    tmp_path: Path, qtbot  # noqa: ANN001, ARG001
) -> None:
    """The actual defect this rebuild fixes.

    `QPdfDocument.render()` holds the GIL for its whole synchronous duration
    -- confirmed by direct measurement, 117-169ms stalls on a real 244MB PDF,
    reproduced even with rendering already on a dedicated worker thread, since
    thread affinity and GIL contention are different problems. `render()` no
    longer runs in this path at all (see the module docstring); this proves
    the invariant that fix depends on, rather than trusting a timing number
    that would be flaky in CI: while `prepare_page()` runs on a background
    thread, an unrelated Python thread on the *test's own* thread keeps
    ticking, rather than freezing for the render's duration.
    """

    pdf_path = tmp_path / "heavy.pdf"
    _write_image_heavy_pdf(pdf_path)
    session = PdfImageService().open(pdf_path)
    try:
        prepared: list[object] = []

        def render_in_background() -> None:
            prepared.append(session.prepare_page(ReaderPageRequest(0, 4000, 4000, 1.0, 0)))

        worker = threading.Thread(target=render_in_background, daemon=True)
        ticks: list[float] = []
        worker.start()
        started = time.perf_counter()
        while worker.is_alive() and time.perf_counter() - started < 5.0:
            ticks.append(time.perf_counter())
            time.sleep(0.002)
        worker.join(timeout=5.0)
        render_ms = (time.perf_counter() - started) * 1000.0

        assert prepared, "the render must have completed within the deadline"
        assert render_ms > 15.0, (
            f"the fixture rendered in {render_ms:.1f}ms -- too fast to prove "
            "anything about GIL contention; widen the fixture"
        )
        # A held GIL blocks this loop from resuming even to record a
        # timestamp -- not just from recording *close-together* ones -- so
        # too few ticks is itself the stall, not a fixture-speed problem.
        # A released GIL gives this loop several ticks per render at the 2ms
        # sleep granularity; require enough to make a real gap measurement.
        assert len(ticks) >= 4, (
            f"only {len(ticks)} tick(s) recorded during a {render_ms:.1f}ms render "
            "-- this thread was blocked almost the entire time, meaning the GIL "
            "was held across the render"
        )
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        # A held GIL would show as one gap approximately equal to the render
        # duration; a released one shows gaps close to the 2ms sleep. 20ms is
        # comfortably between the two and well under a real stall.
        assert max(gaps) < 0.020, (
            f"a background render blocked this thread for {max(gaps) * 1000:.1f}ms "
            "-- the GIL was held across the render"
        )
    finally:
        session.close()


def test_pending_renders_fail_promptly_when_the_session_closes(
    tmp_path: Path, qtbot  # noqa: ANN001, ARG001
) -> None:
    """Closing mid-render must not strand a waiting caller.

    Rendering is no longer a job on the PDF thread's own queue -- it runs on
    Qt's worker, decoupled from submission order -- so the old guarantee
    ("dispose is queued behind any render already submitted") no longer
    bounds this by itself. `_AsyncPageRenderer.dispose()` now fails pending
    completions explicitly; this is what that fix is for; without it this
    test hangs for the full 120s call timeout instead of failing in
    milliseconds.
    """

    pdf_path = tmp_path / "heavy.pdf"
    _write_image_heavy_pdf(pdf_path)
    session = PdfImageService().open(pdf_path)
    result: list[BaseException | None] = []
    started_render = threading.Event()

    def render_in_background() -> None:
        try:
            session.prepare_page(ReaderPageRequest(0, 4000, 4000, 1.0, 0))
        except BaseException as exc:  # noqa: BLE001
            result.append(exc)
        else:
            result.append(None)
        finally:
            started_render.set()

    worker = threading.Thread(target=render_in_background, daemon=True)
    worker.start()
    # No synchronisation point exists between "request submitted" and "still
    # rendering" other than a short sleep -- the fixture's ~50ms render is
    # what gives this a real window to land inside.
    time.sleep(0.01)
    session.close()
    worker.join(timeout=5.0)

    assert not worker.is_alive(), "closing mid-render left the caller hanging"
    assert result and isinstance(result[0], (PdfReadError, PdfThreadError))


def test_completions_correlate_by_request_id_not_arrival_order(
    tmp_path: Path, qtbot  # noqa: ANN001, ARG001
) -> None:
    """Request-id correlation is the whole safety net for the async path.

    A real Qt round trip completes requests in submission order closely
    enough that a naive "hand results out in the order they arrive" bug would
    pass unnoticed in a timing-based test almost every run -- confirmed by
    running such a version of this test five times against a deliberately
    broken correlation with no failure. So this drives the actual mechanism
    directly: three requests are enqueued, then `pageRendered` is delivered
    for them *out of submission order*, and each completion must still
    resolve to the page its own request id was for.
    """

    pdf_path = tmp_path / "multi.pdf"
    _write_pdf(pdf_path, pages=3)
    session = PdfImageService().open(pdf_path)
    try:
        renderer = session._renderer  # noqa: SLF001

        def enqueue_all() -> list[tuple[int, object]]:
            from PySide6.QtCore import QSize

            return [
                (page, renderer.request_page(page, QSize(100, 100))[1]) for page in range(3)
            ]

        submissions = pdf_thread().call(enqueue_all)
        request_ids = [
            request_id
            for request_id in renderer._pending  # noqa: SLF001
        ]
        assert len(request_ids) == 3, "all three requests must still be outstanding"

        # Fabricate delivery out of submission order: last id first, tagged
        # with an image only that id's page could plausibly have produced.
        def deliver_out_of_order() -> None:
            from PySide6.QtCore import QSize
            from PySide6.QtGui import QImage

            for offset, request_id in enumerate(reversed(request_ids)):
                tag = QImage(10 + offset, 10, QImage.Format.Format_RGB32)
                renderer._on_page_rendered(0, QSize(10, 10), tag, None, request_id)  # noqa: SLF001

        pdf_thread().call(deliver_out_of_order)

        for expected_width, (page, completion) in zip((12, 11, 10), submissions):
            image = completion.wait(2.0)
            assert image.width() == expected_width, (
                f"page {page}'s completion resolved to the wrong delivery"
            )
    finally:
        session.close()


def test_a_render_that_times_out_does_not_leak_its_pending_entry(
    tmp_path: Path,
    qtbot,  # noqa: ANN001, ARG001
    monkeypatch,
) -> None:
    """A request whose ``pageRendered`` never arrives must not squat in
    ``_pending`` for the renderer's remaining lifetime.

    Simulates the lost-signal case by registering a pending completion that
    nothing will ever resolve, then shrinking the timeout so the test does
    not have to wait out the real 120s bound. Before the ``discard()`` call
    this guards, the entry below stayed in ``_pending`` forever.
    """

    pdf_path = tmp_path / "sample.pdf"
    _write_pdf(pdf_path)
    monkeypatch.setattr(pdf_module, "PDF_CALL_TIMEOUT_SECONDS", 0.05)
    session = PdfImageService().open(pdf_path)
    try:

        def never_resolves(self, page_index, size):  # noqa: ANN001, ARG001
            completion = pdf_module._PdfRenderCompletion()  # noqa: SLF001
            request_id = -1
            self._pending[request_id] = completion  # noqa: SLF001
            return request_id, completion

        monkeypatch.setattr(pdf_module._AsyncPageRenderer, "request_page", never_resolves)  # noqa: SLF001

        try:
            session.prepare_page(ReaderPageRequest(0, 400, 600, 1.0, 0))
        except PdfThreadError:
            pass
        else:  # pragma: no cover - guard against a leak-test regression
            raise AssertionError("expected the forced timeout to raise PdfThreadError")

        renderer = session._renderer  # noqa: SLF001
        pending_ids = pdf_thread().call(lambda: list(renderer._pending))  # noqa: SLF001
        assert -1 not in pending_ids, "a timed-out request must not linger in _pending"
    finally:
        session.close()


def test_page_rendered_is_handled_on_the_pdf_thread(
    tmp_path: Path,
    qtbot,  # noqa: ANN001, ARG001
    monkeypatch,
) -> None:
    """``_pending`` is unlocked, which is only safe if ``pageRendered`` and
    ``dispose()`` never run concurrently -- both must land on the PDF thread.
    ``dispose()`` gets there by construction (``pdf_thread().post(...)``);
    this proves the signal handler does too, for a *real* Qt round trip
    rather than the direct/manual delivery
    ``test_completions_correlate_by_request_id_not_arrival_order`` uses.
    """

    pdf_path = tmp_path / "sample.pdf"
    _write_pdf(pdf_path)

    handler_threads: list[int] = []
    real_handler = pdf_module._AsyncPageRenderer._on_page_rendered  # noqa: SLF001

    def spy(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        handler_threads.append(threading.get_ident())
        return real_handler(self, *args, **kwargs)

    monkeypatch.setattr(pdf_module._AsyncPageRenderer, "_on_page_rendered", spy)  # noqa: SLF001

    session = PdfImageService().open(pdf_path)
    try:
        session.prepare_page(ReaderPageRequest(0, 400, 600, 1.0, 0))
    finally:
        session.close()

    pdf_thread_ident = pdf_thread().call(threading.get_ident)
    assert handler_threads, "pageRendered must have been delivered"
    assert handler_threads[0] == pdf_thread_ident, (
        "pageRendered ran on a different thread than the PDF thread -- "
        "_pending is unlocked and assumes this never happens"
    )


def test_prepare_thumbnail_pages_isolates_a_failing_page(
    tmp_path: Path,
    qtbot,  # noqa: ANN001, ARG001
    monkeypatch,
) -> None:
    """One page failing to render must not blank the whole thumbnail batch."""

    pdf_path = tmp_path / "sample.pdf"
    _write_pdf(pdf_path, pages=3)
    session = PdfImageService().open(pdf_path)
    try:
        real_prepare_page = pdf_module.PdfImageSession.prepare_page

        def flaky_prepare_page(self, request, _decoder=None):  # noqa: ANN001
            if request.page_index == 1:
                raise PdfReadError("synthetic failure for page 1")
            return real_prepare_page(self, request, _decoder)

        monkeypatch.setattr(pdf_module.PdfImageSession, "prepare_page", flaky_prepare_page)

        results = session.prepare_thumbnail_pages((0, 1, 2), (100, 100))

        assert results[0] is not None
        assert results[1] is None, "a failing page must come back as None, not abort the batch"
        assert results[2] is not None
    finally:
        session.close()


def _png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
