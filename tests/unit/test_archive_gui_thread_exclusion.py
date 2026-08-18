"""Archive decompression must never run on the thread that owns the GUI.

Every other guarantee in the P2 cache work is enforced by a test; this one was
only ever observed by the perf harness (``decompression_on_gui_thread: false``),
which nothing in CI runs. A refactor that made a read inline -- dropping the
executor hop in the page pipeline, or calling ``warm_disk_cache`` directly from
the coordinator -- would freeze the window for as long as a solid archive takes
to decompress, and no suite would notice.

These tests use the real ``TaskService``, the real pipeline and coordinator, and
a real 7z archive, so what they observe is genuine decompression rather than a
fake standing in for it.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from threading import get_ident

import py7zr
from PIL import Image

from joyread.app.archive_warmup_coordinator import ArchiveWarmupCoordinator
from joyread.app.reader_page_pipeline import (
    EncodedPageFrameDecoder,
    PreparedReaderPage,
    ReaderPagePipeline,
    SessionReaderDocumentSource,
)
from joyread.core.archive import ArchiveImageService, ArchiveOpenLimits
from joyread.core.reader.session_service import ReaderSessionService
from joyread.core.services.archive_extraction_pool import ArchiveExtractionPool
from joyread.infrastructure.qt_task_service import TaskService

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_bytes(size: tuple[int, int]) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, "#4477aa").save(buffer, format="PNG")
    return buffer.getvalue()


def _write_7z(path: Path, pages: int = 6) -> None:
    with py7zr.SevenZipFile(path, "w") as archive:
        for index in range(pages):
            archive.writestr(_png_bytes((60 + index, 40)), f"{index:03d}.png")


class _FrameCache:
    def __init__(self) -> None:
        self.values: dict[tuple[int, int, int], PreparedReaderPage[bytes]] = {}

    def get_suitable(self, page_index: int, target_width: int, target_height: int):  # noqa: ANN201
        return self.values.get((page_index, target_width, target_height))

    def put(self, page_index: int, value, target_width: int, target_height: int) -> None:  # noqa: ANN001
        self.values[(page_index, target_width, target_height)] = value

    def clear(self) -> int:
        count = len(self.values)
        self.values.clear()
        return count


class _ThreadRecordingSource(SessionReaderDocumentSource):
    """Report which thread the archive session was actually read on."""

    def __init__(self, session: object) -> None:
        super().__init__(session)
        self.read_threads: list[int] = []

    def read_pages(self, page_indices: tuple[int, ...]):  # noqa: ANN201
        # Recorded on entry rather than on return: the read is synchronous, so
        # this is the thread the decompression runs on, and a read that raises
        # still reports where it was attempted.
        self.read_threads.append(get_ident())
        return super().read_pages(page_indices)


def test_archive_page_reads_never_run_on_the_gui_thread(qtbot, tmp_path: Path) -> None:  # noqa: ANN001, ARG001
    archive = tmp_path / "book.7z"
    _write_7z(archive)
    service = ArchiveImageService(page_cache_dir=tmp_path / "cache")
    session = service.open(archive, document_cache_key="file:threading")
    source = _ThreadRecordingSource(session)
    executor = TaskService(max_workers=1)
    ready: list[PreparedReaderPage[bytes]] = []
    failed: list[tuple[int, Exception, int]] = []
    pipeline = ReaderPagePipeline(
        executor,
        EncodedPageFrameDecoder(),
        _FrameCache(),
        on_ready=ready.append,
        on_failed=lambda index, error, generation: failed.append((index, error, generation)),
    )
    gui_thread = get_ident()

    try:
        pipeline.set_source(source, generation=1)
        pipeline.request(
            (0, 1),
            (2,),
            target_width=800,
            target_height=600,
            device_pixel_ratio=1.0,
            generation=1,
        )
        qtbot.waitUntil(lambda: len(ready) >= 2, timeout=5000)
    finally:
        pipeline.cancel(clear_cache=True)
        executor.shutdown()
        session.close()

    assert not failed, f"the real archive read failed: {failed}"
    assert source.read_threads, "the pipeline never reached the archive session"
    assert gui_thread not in source.read_threads, (
        "archive decompression ran on the GUI thread; a large solid archive "
        "would freeze the window for the whole extraction"
    )
    # Proves the recorded threads did real work: these bytes came out of the
    # 7z container on exactly those threads.
    assert all(page.frame.startswith(PNG_MAGIC) for page in ready)


class _ThreadRecordingSessionService(ReaderSessionService):
    """Report which thread whole-document warmup was performed on."""

    def __init__(self, archive_image_service: ArchiveImageService) -> None:
        super().__init__(archive_image_service)
        self.warm_threads: list[int] = []

    def warm_disk_cache(self, path, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.warm_threads.append(get_ident())
        return super().warm_disk_cache(path, **kwargs)


def test_whole_document_warmup_never_runs_on_the_gui_thread(qtbot, tmp_path: Path) -> None:  # noqa: ANN001, ARG001
    """The warmup path converts a whole archive, so it is the worse offender.

    ``acquire`` is called from the ViewModel on every layout pass; if it ever
    ran its conversion inline the reader would stall on the first page of any
    expensive archive.
    """

    archive = tmp_path / "book.7z"
    _write_7z(archive)
    pool = ArchiveExtractionPool(tmp_path / "pool", 64 * 1024 * 1024)
    session_service = _ThreadRecordingSessionService(
        ArchiveImageService(extraction_pool=pool)
    )
    executor = TaskService(max_workers=1)
    coordinator = ArchiveWarmupCoordinator(session_service, executor)
    ready_threads: list[int] = []
    gui_thread = get_ident()

    try:
        coordinator.acquire(
            archive,
            "reader-1",
            limits=ArchiveOpenLimits(),
            document_cache_key="file:threading",
            allow_persistent_cache=True,
            on_ready=lambda: ready_threads.append(get_ident()),
        )
        assert session_service.warm_threads == [], (
            "acquire() converted the document before returning to the caller"
        )
        qtbot.waitUntil(lambda: bool(ready_threads), timeout=10000)
    finally:
        coordinator.close()
        executor.shutdown()

    assert session_service.warm_threads, "warmup never ran"
    assert gui_thread not in session_service.warm_threads, (
        "whole-document conversion ran on the GUI thread"
    )
    # Real decompression, not just a call that returned early: these bytes were
    # produced by the conversion that ran on the worker thread above.
    assert pool.current_bytes > 0, "the warmup cached nothing, so it proved nothing"
    # The other half of the contract: work goes out to a worker, completion
    # comes back on the GUI thread, because `on_ready` touches Qt widgets.
    assert ready_threads == [gui_thread], (
        "warmup completion must be delivered on the GUI thread"
    )
