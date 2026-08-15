"""A storage transition must not run against a live archive conversion.

`reload_storage_from_settings()` replaces the extraction pool and rebuilds the
archive reading stack. Before quiescence, a background bulk conversion could be
half-way through writing into the pool being replaced -- holding a build marker
on an object about to be dropped, then publishing into a pool nobody will read
again. These tests pin the guarantee that the conversion is stopped, and proven
stopped, before any of that happens.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from threading import Event

import py7zr
from PIL import Image

from joyread.app.archive_warmup_coordinator import ArchiveWarmupCoordinator
from joyread.app.storage_transition_driver import StorageTransitionController
from joyread.core.archive import ArchiveImageService, ArchiveOpenLimits
from joyread.core.reader.session_service import ReaderSessionService
from joyread.core.services.archive_extraction_pool import ArchiveExtractionPool
from joyread.infrastructure.qt_task_service import TaskService


def _png_bytes(size: tuple[int, int]) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, "#4477aa").save(buffer, format="PNG")
    return buffer.getvalue()


def _write_7z(path: Path, pages: int = 6) -> None:
    with py7zr.SevenZipFile(path, "w") as archive:
        for index in range(pages):
            archive.writestr(_png_bytes((60 + index, 40)), f"{index:03d}.png")


class _StallingSessionService(ReaderSessionService):
    """A warmup that blocks until released, standing in for a big conversion."""

    def __init__(self, archive_image_service: ArchiveImageService) -> None:
        super().__init__(archive_image_service)
        self.entered = Event()
        self.release = Event()
        self.cancelled_while_running = False

    def warm_disk_cache(self, path, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.entered.set()
        self.release.wait(5)
        is_cancelled = kwargs.get("is_cancelled")
        # The real conversion polls this between groups and gives up. Recording
        # it proves the quiesce actually reached the running work rather than
        # merely waiting for it to end on its own.
        if callable(is_cancelled):
            self.cancelled_while_running = bool(is_cancelled())
        return None


class _Context:
    """The slice of AppContext a transition drives, over real services."""

    def __init__(self, task_service: TaskService, coordinator: ArchiveWarmupCoordinator) -> None:
        self.task_service = task_service
        self.coordinator = coordinator
        self.committed = False
        self.resumed = False

    def quiesce_for_storage_transition(self) -> int:
        self.coordinator.close()
        return self.task_service.quiesce()

    def storage_transition_pending_tasks(self) -> int:
        return self.task_service.pending_task_count()

    def commit_storage_transition(self) -> None:
        self.committed = True

    def abandon_storage_transition(self) -> None:
        self.task_service.resume()

    def resume_after_storage_transition(self) -> None:
        self.resumed = True
        self.task_service.resume()


class _WindowManager:
    reader_windows = ()

    def close_all_readers(self) -> int:
        return 0


def test_migration_waits_for_a_running_conversion_to_actually_stop(
    qtbot,  # noqa: ANN001
    tmp_path: Path,
) -> None:
    """The disk phase must not begin while work still holds the old pool.

    Without the drain, the migration would run concurrently with a conversion
    still writing into the pool that is about to be replaced.
    """

    archive = tmp_path / "book.7z"
    _write_7z(archive)
    pool = ArchiveExtractionPool(tmp_path / "pool", 64 * 1024 * 1024)
    session_service = _StallingSessionService(ArchiveImageService(extraction_pool=pool))
    task_service = TaskService(max_workers=2)
    coordinator = ArchiveWarmupCoordinator(session_service, task_service)
    context = _Context(task_service, coordinator)
    controller = StorageTransitionController(
        context, _WindowManager(), poll_interval_ms=5, drain_timeout_ms=5000
    )
    migrated: list[str] = []
    controller.finished.connect(migrated.append)

    coordinator.acquire(
        archive,
        "reader-1",
        limits=ArchiveOpenLimits(),
        document_cache_key="file:quiesce",
        allow_persistent_cache=True,
        on_ready=lambda: None,
    )
    assert session_service.entered.wait(5), "the warmup never started"

    try:
        controller.start(lambda: "migrated")

        # The conversion is still holding the pool, so nothing may migrate.
        qtbot.wait(80)
        assert migrated == [], "migration began while a conversion was still running"
        assert context.committed is False, "nothing terminal may run before the drain"

        session_service.release.set()
        qtbot.waitUntil(lambda: bool(migrated), timeout=5000)
    finally:
        session_service.release.set()
        controller.deleteLater()
        task_service.shutdown()

    assert migrated == ["migrated"]
    assert context.committed is True
    assert task_service.pending_task_count() == 0, "the drain must be real, not assumed"
    assert session_service.cancelled_while_running, (
        "the quiesce must cancel running work, not just wait for it"
    )


def test_a_conversion_that_will_not_stop_leaves_storage_untouched(
    qtbot,  # noqa: ANN001
    tmp_path: Path,
) -> None:
    """Abandoning is the correct outcome: migrating without proven quiescence
    is exactly the hazard this exists to prevent."""

    archive = tmp_path / "book.7z"
    _write_7z(archive)
    pool = ArchiveExtractionPool(tmp_path / "pool", 64 * 1024 * 1024)
    session_service = _StallingSessionService(ArchiveImageService(extraction_pool=pool))
    task_service = TaskService(max_workers=2)
    coordinator = ArchiveWarmupCoordinator(session_service, task_service)
    context = _Context(task_service, coordinator)
    controller = StorageTransitionController(
        context, _WindowManager(), poll_interval_ms=5, drain_timeout_ms=40
    )
    migrated: list[str] = []
    abandoned: list[int] = []
    controller.finished.connect(migrated.append)
    controller.abandoned.connect(abandoned.append)

    coordinator.acquire(
        archive,
        "reader-1",
        limits=ArchiveOpenLimits(),
        document_cache_key="file:quiesce",
        allow_persistent_cache=True,
        on_ready=lambda: None,
    )
    assert session_service.entered.wait(5)

    try:
        controller.start(lambda: migrated.append("migrated"))
        qtbot.waitUntil(lambda: bool(abandoned), timeout=5000)
    finally:
        session_service.release.set()
        controller.deleteLater()
        task_service.shutdown()

    assert migrated == [], "storage must not be touched"
    assert context.committed is False, "no terminal teardown on the abandon path"
    assert abandoned and abandoned[0] >= 1
