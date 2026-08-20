"""Qt PDF adapter with viewport-sized worker rendering.

Rendering goes through ``QPdfPageRenderer`` in ``MultiThreaded`` mode, not a
direct ``QPdfDocument.render()`` call, and that choice is load-bearing rather
than stylistic. ``document.render()`` is a synchronous call into PDFium, and
the CPython binding does not release the GIL around it -- measured holding it
for the full render, 117-169 ms on a 244 MB image-heavy PDF, which stalls
*every* Python thread for that span, including the GUI thread's own event
processing. This was true even with rendering already dispatched to the
dedicated PDF thread below: that thread satisfies Qt's affinity contract, a
different requirement from not holding the GIL, and solving the first did not
solve the second.

``QPdfPageRenderer.requestPage()`` returns immediately -- confirmed under
1 ms -- and the actual decode happens on Qt's own internal worker, entirely in
C++, never re-entering Python until the result is ready. The calling thread
waits on a ``threading.Event`` for that result, and `Event.wait()` releases
the GIL for the duration of the wait, which is what actually fixes the stall:
confirmed by the same heartbeat measurement showing p95 15 ms under render
load that previously produced 169 ms gaps.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from io import BytesIO
from pathlib import Path
from threading import Event, RLock
from time import perf_counter
from uuid import uuid4

import shiboken6
from PIL import Image, ImageChops
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize
from PySide6.QtGui import QImage
from PySide6.QtPdf import QPdfDocument, QPdfPageRenderer

from joyread.app.reader_page_pipeline import PreparedReaderPage, ReaderPageRequest
from joyread.core.diagnostics import reader_perf_enabled, reader_perf_event
from joyread.core.operation_context import bind_operation, create_operation
from joyread.core.reader.models import ReaderPageImage
from joyread.core.reader.pdf import (
    PDF_EXTENSIONS,
    PdfEmptyError,
    PdfError,
    PdfOpenError,
    PdfPasswordUnsupportedError,
    PdfReadError,
    PdfValidationResult,
)
from joyread.infrastructure.pdf_document_thread import (
    PDF_CALL_TIMEOUT_SECONDS,
    PdfThreadError,
    pdf_thread,
)


logger = logging.getLogger(__name__)

PDF_RENDER_MAX_LONG_EDGE = 4096
_PDF_FALLBACK_PAGE_SIZE = (612, 792)
_PDF_WHITE_MARGIN_THRESHOLD = 248
_PDF_ALPHA_MARGIN_THRESHOLD = 16
_PDF_CROP_PADDING_RATIO = 0.018
_PDF_MIN_CROP_KEEP_RATIO = 0.55
_PDF_MAX_CROP_KEEP_RATIO = 0.985


class _PdfRenderCompletion:
    """Bridges one async ``pageRendered`` signal back to a waiting caller.

    Resolved on the PDF thread (inside the renderer's signal handler) and
    waited on from the calling thread (a task worker in production). The
    ``Event`` is what makes the wait cheap: ``wait()`` releases the GIL for
    its duration, which is the entire point -- see the module docstring.
    """

    __slots__ = ("_done", "_result", "_error")

    def __init__(self) -> None:
        self._done = Event()
        self._result: QImage | None = None
        self._error: BaseException | None = None

    def resolve(self, result: QImage) -> None:
        self._result = result
        self._done.set()

    def reject(self, error: BaseException) -> None:
        self._error = error
        self._done.set()

    def wait(self, timeout: float) -> QImage:
        if not self._done.wait(timeout):
            raise PdfThreadError(f"PDF render did not complete within {timeout:g}s.")
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class _AsyncPageRenderer:
    """One ``QPdfPageRenderer`` per session, touched only on the PDF thread.

    ``requestPage()`` assigns its own request id and returns it synchronously;
    that id is the only thing correlating a later ``pageRendered`` signal back
    to the completion a caller is waiting on. Nothing else identifies which
    request a signal answers.
    """

    def __init__(self, document: QPdfDocument) -> None:
        # Constructed inside a closure already running on the PDF thread (see
        # PdfImageService.open), so this gets that thread's affinity the same
        # way the document does -- no explicit moveToThread needed.
        self._renderer = QPdfPageRenderer()
        self._renderer.setDocument(document)
        self._renderer.setRenderMode(QPdfPageRenderer.RenderMode.MultiThreaded)
        self._renderer.pageRendered.connect(self._on_page_rendered)
        self._pending: dict[int, _PdfRenderCompletion] = {}
        # Tracked so a PDF-thread shutdown that arrives before the owning
        # session ever calls close() can still fail this renderer's pending
        # completions -- otherwise a caller stranded mid-wait would block
        # until the 120s timeout instead of failing with the thread gone.
        _live_renderers[id(self)] = self

    def request_page(self, page_index: int, size: QSize) -> tuple[int, _PdfRenderCompletion]:
        """Enqueue a render and return its id and completion. Must run on the PDF thread."""

        completion = _PdfRenderCompletion()
        request_id = self._renderer.requestPage(page_index, size)
        self._pending[request_id] = completion
        return request_id, completion

    def discard(self, request_id: int) -> None:
        """Drop a pending completion the caller gave up waiting on.

        Without this, a request whose ``pageRendered`` never arrives (the
        signal genuinely lost, rather than the session closing -- ``dispose()``
        already covers that case) leaves its completion in ``_pending`` for
        the renderer's entire remaining lifetime. Must run on the PDF thread,
        same as every other method here; a late ``pageRendered`` for this id
        finds nothing and is a no-op, same as today's post-``dispose()`` case.
        """

        self._pending.pop(request_id, None)

    def _on_page_rendered(
        self, page_index: int, image_size: QSize, image: QImage, options: object, request_id: int
    ) -> None:
        del page_index, image_size, options
        completion = self._pending.pop(request_id, None)
        if completion is None:
            # Already resolved by dispose() (session closed mid-render), or a
            # stale id from a renderer that no longer exists. Either way there
            # is no one left to hand this result to.
            return
        completion.resolve(image)

    def dispose(self) -> None:
        """Detach the document and fail anything still outstanding.

        Must run on the PDF thread, same as every other method here. Failing
        pending completions here, rather than leaving them for a
        ``pageRendered`` that may never come once the document is gone, is
        what keeps a caller's ``wait()`` bounded by this call instead of by
        the timeout.
        """

        _live_renderers.pop(id(self), None)
        pending, self._pending = self._pending, {}
        self._renderer.setDocument(None)
        self._renderer.pageRendered.disconnect(self._on_page_rendered)
        self._renderer.deleteLater()
        for completion in pending.values():
            completion.reject(PdfReadError("PDF session is closed."))


# Touched only on the PDF thread, same custody rule as _live_documents below.
_live_renderers: dict[int, "_AsyncPageRenderer"] = {}


class PdfImageSession:
    """Render pages from one ``QPdfDocument`` held open for the session.

    The document is deliberately loaded once and kept. ``QPdfDocument.load()``
    retains roughly one file's worth of memory that ``close()`` does not give
    back, so re-loading per page leaked about a file-size per page turn and
    also spent ~45 percent of each page's time re-parsing the container.

    The document never leaves the PDF thread. Every load, render-request, and
    disposal is marshalled there by
    :mod:`joyread.infrastructure.pdf_document_thread`, which satisfies Qt's
    thread-affinity contract, serialises submission order, and lets discarded
    documents actually be deleted. The render itself does not run *on* that
    thread -- see :class:`_AsyncPageRenderer` and the module docstring.
    """

    def __init__(
        self,
        path: Path,
        dimensions: tuple[tuple[int, int], ...],
        *,
        document: QPdfDocument,
        renderer: _AsyncPageRenderer,
        normalize_margins: bool = False,
        document_id: str | None = None,
    ) -> None:
        self._path = path
        self._dimensions = dimensions
        self._normalize_margins = normalize_margins
        self._state_lock = RLock()
        self._closed = False
        self._document: QPdfDocument | None = document
        self._renderer: _AsyncPageRenderer | None = renderer
        self._document_id = document_id or uuid4().hex
        self.current_index = 0

    @property
    def page_count(self) -> int:
        return len(self._dimensions)

    @property
    def document_id(self) -> str:
        return self._document_id

    @property
    def index_range(self) -> range:
        return range(self.page_count)

    def is_valid_index(self, index: int) -> bool:
        return 0 <= index < self.page_count

    def get_dimensions(self, index: int) -> tuple[int, int] | None:
        return self._dimensions[index] if self.is_valid_index(index) else None

    def prepare_page(self, request: ReaderPageRequest, _decoder=None) -> PreparedReaderPage[QImage]:  # noqa: ANN001
        if not self.is_valid_index(request.page_index):
            raise PdfReadError(f"Invalid PDF page {request.page_index + 1}.")
        perf_enabled = reader_perf_enabled()
        total_started = perf_counter() if perf_enabled else 0.0
        target = _fit_render_size(
            self._dimensions[request.page_index],
            (request.target_width, request.target_height),
        )

        def start_render() -> tuple[_AsyncPageRenderer, int, _PdfRenderCompletion]:
            # Runs on the PDF thread, and only enqueues -- it must return fast,
            # since this call itself still blocks whichever thread is waiting
            # on it below. The liveness check belongs here, not in the caller:
            # disposal is queued on this same thread, so checking here is what
            # makes "closed" and "render requested" mutually ordered.
            with self._state_lock:
                renderer = self._renderer
                if self._closed or renderer is None:
                    raise PdfReadError("PDF session is closed.")
                request_id, completion = renderer.request_page(request.page_index, QSize(*target))
                return renderer, request_id, completion

        started = perf_counter()
        renderer, request_id, completion = pdf_thread().call(start_render)
        # completion.wait() gets what's left of one PDF_CALL_TIMEOUT_SECONDS
        # budget, not a fresh one -- call() above already spent some of it
        # getting the request onto a possibly-backlogged PDF thread, and the
        # constant's own purpose is a single ceiling on "a wedged or
        # torn-down PDF thread", not two ceilings stacked to double it.
        remaining = max(0.0, PDF_CALL_TIMEOUT_SECONDS - (perf_counter() - started))
        try:
            # The actual decode happens on Qt's own worker, not here, and this
            # wait releases the GIL for its duration -- see the module docstring.
            image = completion.wait(remaining)
        except PdfThreadError:
            # A `pageRendered` that never arrives (not a `dispose()` reject,
            # which already pops this entry) would otherwise leak this
            # request's slot in `_pending` for the renderer's remaining life.
            pdf_thread().post(lambda: renderer.discard(request_id))
            raise
        render_ms = (perf_counter() - started) * 1000.0 if perf_enabled else 0.0
        if image.isNull():
            raise PdfReadError(f"Could not render PDF page {request.page_index + 1}.")
        image.setDevicePixelRatio(max(1.0, request.device_pixel_ratio))
        copy_started = perf_counter() if perf_enabled else 0.0
        prepared_frame = image.copy()
        copy_ms = (perf_counter() - copy_started) * 1000.0 if perf_enabled else 0.0
        if perf_enabled:
            reader_perf_event(
                "pdf.prepare",
                page=request.page_index,
                generation=request.generation,
                target=(request.target_width, request.target_height),
                rendered=(image.width(), image.height()),
                frame_bytes=image.bytesPerLine() * image.height(),
                render_ms=round(render_ms, 3),
                copy_ms=round(copy_ms, 3),
                total_ms=round((perf_counter() - total_started) * 1000.0, 3),
            )
        return PreparedReaderPage(
            page_index=request.page_index,
            frame=prepared_frame,
            source_dimensions=self._dimensions[request.page_index],
            rendered_dimensions=(image.width(), image.height()),
            generation=request.generation,
        )

    def prepare_thumbnail_pages(
        self,
        page_indices: Iterable[int],
        size: tuple[int, int],
    ) -> list[PreparedReaderPage[QImage] | None]:
        """Render bounded worker frames without a PNG encode/decode roundtrip.

        One page failing to render (a corrupt page, a timed-out request) must
        not cost the caller every other page in the batch -- it goes into the
        result as ``None``, the same signal already used for an out-of-range
        index, so the caller's existing "fall back for the missing ones" path
        handles it without needing to know why a page came back empty.
        """

        target_width = max(1, int(size[0])) * 2
        target_height = max(1, int(size[1])) * 2
        results: list[PreparedReaderPage[QImage] | None] = []
        for page_index in page_indices:
            if not self.is_valid_index(page_index):
                results.append(None)
                continue
            try:
                results.append(
                    self.prepare_page(
                        ReaderPageRequest(
                            page_index,
                            target_width,
                            target_height,
                            1.0,
                            0,
                        )
                    )
                )
            except (PdfError, PdfThreadError) as exc:
                logger.debug("Direct thumbnail render failed page=%d: %s", page_index, exc)
                results.append(None)
        return results

    def read_page(self, page_index: int):  # noqa: ANN201 - direct preparation is preferred.
        del page_index
        raise PdfReadError("PDF pages must be rendered for a target viewport.")

    def get_page(self, index: int) -> ReaderPageImage | None:
        pages = self.get_pages((index,))
        return pages[0] if pages else None

    def get_pages(self, indices: Iterable[int]) -> list[ReaderPageImage | None]:
        results: list[ReaderPageImage | None] = []
        for index in indices:
            if not self.is_valid_index(index):
                results.append(None)
                continue
            width, height = _fit_render_size(self._dimensions[index], (2048, 2048))
            request = ReaderPageRequest(index, width, height, 1.0, 0)
            prepared = self.prepare_page(request)
            payload, _rendered_dimensions = _qimage_png(prepared.frame)
            dimensions = self._dimensions[index]
            if self._normalize_margins:
                payload, dimensions = _normalize_rendered_pdf_png(payload)
            results.append(ReaderPageImage(index, payload, dimensions))
        return results

    def seek(self, index: int) -> bool:
        if not self.is_valid_index(index):
            return False
        with self._state_lock:
            self.current_index = index
        return True

    def close(self) -> None:
        """Release the document and renderer without blocking the caller.

        ``close()`` reaches us from several places, including Qt callbacks on
        the GUI thread, so it must never wait on rendering. Disposal is queued
        behind whatever is already on the PDF thread's queue, but an
        in-flight *render* is no longer one of those things -- it runs on
        Qt's own worker, decoupled from this thread's queue -- so FIFO
        ordering alone cannot bound it the way it did before. Instead
        ``_AsyncPageRenderer.dispose()`` explicitly fails every completion
        still outstanding, which is what keeps a caller's ``wait()`` bounded
        by this call rather than by the timeout.
        """

        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        logger.info(
            "PDF session close requested",
            extra={
                "event": "pdf.session.close.requested",
                "category": "pdf",
                "status": "started",
                "document_id": self._document_id,
                "page_count": self.page_count,
            },
        )

        def dispose() -> None:
            with self._state_lock:
                document, self._document = self._document, None
                renderer, self._renderer = self._renderer, None
            if renderer is not None:
                renderer.dispose()
            if document is None:
                logger.info(
                    "PDF session close finished",
                    extra={
                        "event": "pdf.session.close.finished",
                        "category": "pdf",
                        "status": "finished",
                        "document_id": self._document_id,
                    },
                )
                return
            _dispose_document(document)
            logger.info(
                "PDF session close finished",
                extra={
                    "event": "pdf.session.close.finished",
                    "category": "pdf",
                    "status": "finished",
                    "document_id": self._document_id,
                },
            )

        pdf_thread().post(dispose)


class PdfImageService:
    def __init__(self, *, normalize_margins: bool = False) -> None:
        self._normalize_margins = normalize_margins

    def open(self, path: str | Path) -> PdfImageSession:
        operation = create_operation("pdf.document.open", category="pdf")
        started = perf_counter()
        with bind_operation(operation):
            logger.info(
                "PDF document open started",
                extra={
                    "event": "pdf.document.open.started",
                    "category": "pdf",
                    "status": "started",
                },
            )
            try:
                session = self._open_bound(path)
            except Exception as exc:
                controlled = isinstance(exc, PdfError)
                logger.log(
                    logging.WARNING if controlled else logging.ERROR,
                    "PDF document open failed",
                    exc_info=None if controlled else True,
                    extra={
                        "event": "pdf.document.open.failed",
                        "category": "pdf",
                        "status": "failed",
                        "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                        "error_type": type(exc).__name__,
                        "reason": str(exc),
                    },
                )
                raise
            logger.info(
                "PDF document open finished",
                extra={
                    "event": "pdf.document.open.finished",
                    "category": "pdf",
                    "status": "finished",
                    "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                    "document_id": session.document_id,
                    "page_count": session.page_count,
                },
            )
            return session

    def _open_bound(self, path: str | Path) -> PdfImageSession:
        """Open a PDF while the public operation context is already bound."""

        source = Path(path)
        _validate_source(source)
        perf_enabled = reader_perf_enabled()
        started = perf_counter() if perf_enabled else 0.0

        def load() -> tuple[QPdfDocument, _AsyncPageRenderer, tuple[tuple[int, int], ...]]:
            # Runs on the PDF thread; the document is handed to the session
            # rather than closed, so a Reader parses its container once. The
            # renderer is built here too -- it needs the same thread affinity
            # as the document it renders.
            document = _retain_document(_load_document(source))
            try:
                page_count = document.pageCount()
                if page_count <= 0:
                    raise PdfEmptyError(f"No pages found in PDF: {source}")
                measured = tuple(
                    _source_dimensions(document, index) for index in range(page_count)
                )
                renderer = _AsyncPageRenderer(document)
            except BaseException:
                # Covers KeyboardInterrupt too. The exception is re-raised, so
                # nothing is masked; this only stops a half-open document from
                # outliving the failure.
                _dispose_document(document)
                raise
            return document, renderer, measured

        document, renderer, dimensions = pdf_thread().call(load)
        if perf_enabled:
            reader_perf_event(
                "pdf.open",
                page_count=len(dimensions),
                elapsed_ms=round((perf_counter() - started) * 1000.0, 3),
            )
        try:
            return PdfImageSession(
                source,
                dimensions,
                document=document,
                renderer=renderer,
                normalize_margins=self._normalize_margins,
                document_id=uuid4().hex,
            )
        except BaseException:
            # Nothing owns the document or renderer until the session exists,
            # so a failure constructing it would otherwise strand both.
            def dispose_both() -> None:
                renderer.dispose()
                _dispose_document(document)

            pdf_thread().post(dispose_both)
            raise

    def validate_pdf(self, path: str | Path) -> PdfValidationResult:
        return self.probe_pdf(path)

    def probe_pdf(self, path: str | Path) -> PdfValidationResult:
        source = Path(path)
        try:
            _validate_source(source)
            page_count = _probe_page_count(source)
            if page_count <= 0:
                raise PdfEmptyError(f"No pages found in PDF: {source}")
        except (PdfError, OSError) as exc:
            return PdfValidationResult(False, str(exc), error_type=type(exc).__name__)
        return PdfValidationResult(True, "PDF container contains page content.", page_count)


def _validate_source(source: Path) -> None:
    if not source.exists():
        raise PdfOpenError(f"PDF does not exist: {source}")
    if not source.is_file():
        raise PdfOpenError(f"PDF path is not a file: {source}")
    if source.suffix.lower() not in PDF_EXTENSIONS:
        raise PdfOpenError(f"Unsupported PDF format: {source.suffix or source.name}")


def _dispose_document(document: QPdfDocument) -> None:
    """Release a document. Must run on the PDF thread."""

    _live_documents.pop(id(document), None)
    if not shiboken6.isValid(document):
        return
    document.close()
    document.deleteLater()


# Touched only on the PDF thread, which serialises access, so these need no lock.
_live_documents: dict[int, QPdfDocument] = {}
_probe_document: QPdfDocument | None = None


def _retain_document(document: QPdfDocument) -> QPdfDocument:
    """Keep owner-thread custody until normal disposal or thread shutdown."""

    key = id(document)
    _live_documents[key] = document
    document.destroyed.connect(lambda *_args, key=key: _live_documents.pop(key, None))
    return document


def _release_all_documents() -> None:
    """Shutdown hook. Release every renderer and document before the owner
    thread exits.

    Renderers first: disposing one fails any completion still outstanding,
    which is what stops a caller's `wait()` from riding out the full timeout
    against a thread that is no longer there to answer it. Then the documents
    those renderers pointed at.
    """

    global _probe_document
    renderers = tuple(_live_renderers.values())
    documents = tuple(_live_documents.values())
    _probe_document = None
    for renderer in renderers:
        renderer.dispose()
    for document in documents:
        _dispose_document(document)


def _probe_page_count(source: Path) -> int:
    """Count pages using one reused container.

    Import preflight validates whole folders at a time, and a fresh document
    per probe retains roughly a file-size apiece that neither ``close()``,
    ``deleteLater()`` nor an immediate C++ delete gives back -- measured at
    +37 MB per probe against +0.10 MB when the container is reused. So the
    document is kept, which bounds the cost at one file rather than letting it
    grow with the batch, and released when the PDF thread stops.
    """

    global _probe_document
    worker = pdf_thread()

    def probe() -> int:
        global _probe_document
        if _probe_document is None:
            _probe_document = _retain_document(QPdfDocument())
        document = _probe_document
        try:
            error = document.load(str(source))
            if error != QPdfDocument.Error.None_:
                _raise_for_load_error(error, source)
            return document.pageCount()
        finally:
            document.close()

    return worker.call(probe)


def _load_document(path: Path) -> QPdfDocument:
    document = QPdfDocument()
    error = document.load(str(path))
    if error == QPdfDocument.Error.None_:
        return document
    document.close()
    _raise_for_load_error(error, path)
    raise PdfOpenError(f"Could not open PDF: {path}")  # pragma: no cover - unreachable


def _raise_for_load_error(error: QPdfDocument.Error, path: Path) -> None:
    if error == QPdfDocument.Error.FileNotFound:
        raise PdfOpenError(f"PDF does not exist: {path}")
    if error == QPdfDocument.Error.InvalidFileFormat:
        raise PdfOpenError(f"Invalid PDF file: {path}")
    if error in {QPdfDocument.Error.IncorrectPassword, QPdfDocument.Error.UnsupportedSecurityScheme}:
        raise PdfPasswordUnsupportedError(f"Password-protected PDF files are not supported yet: {path}")
    raise PdfOpenError(f"Could not open PDF: {path} ({error.name})")


# Registration does not start the thread. The hook persists across restartable
# shutdowns and protects documents whose caller-side close arrives too late.
pdf_thread().add_shutdown_hook(_release_all_documents)


def _source_dimensions(document: QPdfDocument, page_index: int) -> tuple[int, int]:
    size = document.pagePointSize(page_index)
    if size.width() <= 0 or size.height() <= 0:
        return _PDF_FALLBACK_PAGE_SIZE
    return max(1, round(size.width())), max(1, round(size.height()))


def _fit_render_size(source: tuple[int, int], target: tuple[int, int]) -> tuple[int, int]:
    width, height = source
    target_width, target_height = target
    scale = min(target_width / width, target_height / height)
    rendered = (max(1, round(width * scale)), max(1, round(height * scale)))
    long_edge = max(rendered)
    if long_edge <= PDF_RENDER_MAX_LONG_EDGE:
        return rendered
    clamp = PDF_RENDER_MAX_LONG_EDGE / long_edge
    return max(1, round(rendered[0] * clamp)), max(1, round(rendered[1] * clamp))


def _qimage_png(image: QImage) -> tuple[bytes, tuple[int, int]]:
    byte_array = QByteArray()
    buffer = QBuffer(byte_array)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        if not image.save(buffer, "PNG"):
            raise PdfReadError("Could not encode rendered PDF page.")
    finally:
        buffer.close()
    return bytes(byte_array), (image.width(), image.height())


def _normalize_rendered_pdf_png(payload: bytes) -> tuple[bytes, tuple[int, int]]:
    try:
        with Image.open(BytesIO(payload)) as source:
            image = source.convert("RGBA")
    except OSError:
        return payload, _PDF_FALLBACK_PAGE_SIZE
    bbox = _content_bbox(image)
    if bbox is None or not _should_crop(image.size, bbox):
        return payload, image.size
    width, height = image.size
    padding = max(4, round(min(width, height) * _PDF_CROP_PADDING_RATIO))
    left, top, right, bottom = bbox
    cropped = image.crop((max(0, left - padding), max(0, top - padding), min(width, right + padding), min(height, bottom + padding)))
    output = BytesIO()
    cropped.save(output, format="PNG")
    return output.getvalue(), cropped.size


def _content_bbox(image: Image.Image) -> tuple[int, int, int, int] | None:
    rgba = image.convert("RGBA")
    ink = rgba.convert("L").point(lambda value: 255 if value < _PDF_WHITE_MARGIN_THRESHOLD else 0)
    alpha = rgba.getchannel("A").point(lambda value: 255 if value > _PDF_ALPHA_MARGIN_THRESHOLD else 0)
    return ImageChops.multiply(ink, alpha).getbbox()


def _should_crop(size: tuple[int, int], bbox: tuple[int, int, int, int]) -> bool:
    width, height = size
    left, top, right, bottom = bbox
    width_ratio = max(0, right - left) / width
    height_ratio = max(0, bottom - top) / height
    return (
        width_ratio >= _PDF_MIN_CROP_KEEP_RATIO
        and height_ratio >= _PDF_MIN_CROP_KEEP_RATIO
        and (width_ratio <= _PDF_MAX_CROP_KEEP_RATIO or height_ratio <= _PDF_MAX_CROP_KEEP_RATIO)
    )
