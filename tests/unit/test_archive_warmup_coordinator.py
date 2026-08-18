"""Application-scope archive warmup coordination tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from joyread.app.archive_warmup_coordinator import ArchiveWarmupCoordinator
from joyread.app.tasking import TaskHandle, TaskPriority, TaskStatus


@dataclass
class _PendingTask:
    handle: TaskHandle[None]
    work: object
    on_success: object
    priority: TaskPriority | int


class _ManualTaskService:
    def __init__(self) -> None:
        self.tasks: list[_PendingTask] = []

    def submit(self, name, callback, *, on_success, on_failure, priority):  # noqa: ANN001
        del on_failure
        handle: TaskHandle[None] = TaskHandle(name, status=TaskStatus.RUNNING)
        self.tasks.append(_PendingTask(handle, callback, on_success, priority))
        return handle

    def run(self, index: int) -> None:
        task = self.tasks[index]
        task.work()  # type: ignore[operator]
        task.handle.status = TaskStatus.COMPLETED
        task.on_success(None)  # type: ignore[operator]


class _FakeSessionService:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, int, int, int, bool, str, bool]] = []
        self.passwords: list[str | None] = []

    def warm_disk_cache(
        self,
        path: Path,
        *,
        limits,
        document_cache_key: str,
        allow_persistent_cache: bool,
        chunk_size: int,
        is_cancelled,
        password: str | None = None,
    ) -> None:  # noqa: ANN001
        self.passwords.append(password)
        self.calls.append(
            (
                path,
                limits.nested_archive_max_depth,
                limits.global_file_max_depth,
                chunk_size,
                bool(is_cancelled()),
                document_cache_key,
                allow_persistent_cache,
            )
        )


def test_archive_warmup_deduplicates_consumers_and_runs_one_source_at_a_time(tmp_path: Path) -> None:
    first = tmp_path / "first.cbr"
    second = tmp_path / "second.cb7"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    tasks = _ManualTaskService()
    sessions = _FakeSessionService()
    coordinator = ArchiveWarmupCoordinator(
        sessions,  # type: ignore[arg-type]
        tasks,  # type: ignore[arg-type]
    )
    ready: list[str] = []

    coordinator.acquire(
        first,
        "detail",
        document_cache_key="file:first",
        allow_persistent_cache=True,
        nested_depth=2,
        global_depth=100,
        on_ready=lambda: ready.append("detail"),
    )
    coordinator.acquire(
        first,
        "reader",
        document_cache_key="file:first",
        allow_persistent_cache=True,
        nested_depth=2,
        global_depth=100,
        on_ready=lambda: ready.append("reader"),
    )
    coordinator.acquire(
        second,
        "editor",
        document_cache_key="file:second",
        allow_persistent_cache=True,
        nested_depth=2,
        global_depth=100,
        on_ready=lambda: ready.append("editor"),
    )

    assert len(tasks.tasks) == 1
    assert tasks.tasks[0].priority == TaskPriority.BACKGROUND

    tasks.run(0)

    assert sessions.calls == [(first, 2, 100, 8, False, "file:first", True)]
    assert ready == ["detail", "reader"]
    assert len(tasks.tasks) == 2

    coordinator.release("editor")
    tasks.run(1)

    assert sessions.calls[-1] == (second, 2, 100, 8, True, "file:second", True)
    assert ready == ["detail", "reader"]


def test_archive_warmup_invalidation_waits_for_active_worker_before_replacement(tmp_path: Path) -> None:
    first = tmp_path / "first.cbr"
    second = tmp_path / "second.cb7"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    tasks = _ManualTaskService()
    sessions = _FakeSessionService()
    coordinator = ArchiveWarmupCoordinator(
        sessions,  # type: ignore[arg-type]
        tasks,  # type: ignore[arg-type]
    )
    ready: list[str] = []

    coordinator.acquire(first, "old", on_ready=lambda: ready.append("old"))
    coordinator.invalidate()
    coordinator.acquire(second, "new", on_ready=lambda: ready.append("new"))

    assert len(tasks.tasks) == 1
    tasks.run(0)
    first_cache_key = sessions.calls[0][5]
    assert sessions.calls == [(first, 2, 100, 8, True, first_cache_key, False)]
    assert first_cache_key.startswith("session:")
    assert ready == []
    assert len(tasks.tasks) == 2

    tasks.run(1)
    second_cache_key = sessions.calls[-1][5]
    assert sessions.calls[-1] == (second, 2, 100, 8, False, second_cache_key, False)
    assert second_cache_key.startswith("session:")
    assert ready == ["new"]


def test_reset_releases_a_warmup_whose_task_never_reported_back(tmp_path: Path) -> None:
    """A storage transition cancels the running warmup at the task-service
    level, which suppresses both its success and failure callbacks. `_finish`
    therefore never runs and `_active_key` stays set -- after which
    `_start_next` returns immediately forever and no later warmup can start.
    """

    archive = tmp_path / "book.cb7"
    archive.write_bytes(b"book")
    tasks = _ManualTaskService()
    sessions = _FakeSessionService()
    coordinator = ArchiveWarmupCoordinator(sessions, tasks)  # type: ignore[arg-type]

    coordinator.acquire(archive, "reader-1", on_ready=lambda: None)
    assert len(tasks.tasks) == 1

    # What a quiesce does: consumers withdrawn, then the task cancelled so its
    # callbacks never fire.
    coordinator.close()
    tasks.tasks[0].handle.status = TaskStatus.CANCELLED

    coordinator.reset()

    coordinator.acquire(archive, "reader-2", on_ready=lambda: None)
    assert len(tasks.tasks) == 2, "a warmup after the transition must actually start"


def test_replacing_the_session_service_drops_warmups_for_retired_storage(tmp_path: Path) -> None:
    archive = tmp_path / "book.cb7"
    archive.write_bytes(b"book")
    tasks = _ManualTaskService()
    coordinator = ArchiveWarmupCoordinator(_FakeSessionService(), tasks)  # type: ignore[arg-type]

    coordinator.acquire(archive, "reader-1", on_ready=lambda: None)
    tasks.tasks[0].handle.status = TaskStatus.CANCELLED
    coordinator.replace_session_service(_FakeSessionService())  # type: ignore[arg-type]

    coordinator.acquire(archive, "reader-2", on_ready=lambda: None)
    assert len(tasks.tasks) == 2


class _CancelProbeSessionService:
    """Captures each worker's cancellation predicate for later inspection."""

    def __init__(self) -> None:
        self.probes: list[object] = []

    def warm_disk_cache(self, path, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.probes.append(kwargs.get("is_cancelled"))


def test_a_reset_worker_stays_cancelled_when_the_same_book_is_reopened(tmp_path: Path) -> None:
    """The key is derived from the document and the limits snapshot, so
    reopening the same book produces the same key. A purely key-based
    cancellation test let the *new* state answer for the old worker: it saw
    consumers again and resumed, running a second whole-document extraction
    alongside the one that had just started.
    """

    archive = tmp_path / "book.cb7"
    archive.write_bytes(b"book")
    tasks = _ManualTaskService()
    sessions = _CancelProbeSessionService()
    coordinator = ArchiveWarmupCoordinator(sessions, tasks)  # type: ignore[arg-type]

    coordinator.acquire(archive, "reader-1", document_cache_key="file:same", on_ready=lambda: None)
    tasks.tasks[0].work()  # type: ignore[operator]
    probe = sessions.probes[0]
    assert probe() is False, "a wanted job must not report itself cancelled"

    # A storage transition: consumers withdrawn, task cancelled, then reset.
    coordinator.close()
    tasks.tasks[0].handle.status = TaskStatus.CANCELLED
    coordinator.reset()
    assert probe() is True

    coordinator.acquire(archive, "reader-2", document_cache_key="file:same", on_ready=lambda: None)

    assert probe() is True, "the retired worker must not be revived by a new request"


def test_a_retired_worker_keeps_the_slot_until_it_exits(tmp_path: Path) -> None:
    """Otherwise the reset overlaps two whole-document extractions."""

    archive = tmp_path / "book.cb7"
    archive.write_bytes(b"book")
    tasks = _ManualTaskService()
    coordinator = ArchiveWarmupCoordinator(_FakeSessionService(), tasks)  # type: ignore[arg-type]

    coordinator.acquire(archive, "reader-1", document_cache_key="file:same", on_ready=lambda: None)
    state = coordinator._states[coordinator._active_key]  # noqa: SLF001
    state.started.set()  # the worker is running and has not returned

    coordinator.close()
    tasks.tasks[0].handle.status = TaskStatus.CANCELLED
    coordinator.reset()

    coordinator.acquire(archive, "reader-2", document_cache_key="file:same", on_ready=lambda: None)
    assert len(tasks.tasks) == 1, "a second extraction must not start while the first runs"

    state.exited.set()
    coordinator.acquire(archive, "reader-3", document_cache_key="file:same", on_ready=lambda: None)

    assert len(tasks.tasks) == 2, "the queued warmup runs once the slot is free"


def test_a_worker_that_never_started_does_not_hold_the_slot(tmp_path: Path) -> None:
    """A task cancelled before its runnable ran has no worker to wait for."""

    archive = tmp_path / "book.cb7"
    archive.write_bytes(b"book")
    tasks = _ManualTaskService()
    coordinator = ArchiveWarmupCoordinator(_FakeSessionService(), tasks)  # type: ignore[arg-type]

    coordinator.acquire(archive, "reader-1", document_cache_key="file:same", on_ready=lambda: None)
    tasks.tasks[0].handle.status = TaskStatus.CANCELLED  # never entered work()
    coordinator.reset()

    coordinator.acquire(archive, "reader-2", document_cache_key="file:same", on_ready=lambda: None)

    assert len(tasks.tasks) == 2


def test_warmup_carries_the_password_for_an_encrypted_document(tmp_path: Path) -> None:
    """Warmup opens its own session, so an encrypted archive that is not handed
    the password warms nothing -- leaving the format that gains most from
    pre-conversion as the only one never pre-converted."""

    source = tmp_path / "secret.zip"
    source.write_bytes(b"secret")
    tasks = _ManualTaskService()
    sessions = _FakeSessionService()
    coordinator = ArchiveWarmupCoordinator(
        sessions,  # type: ignore[arg-type]
        tasks,  # type: ignore[arg-type]
    )

    coordinator.acquire(
        source,
        "reader",
        document_cache_key="file:secret",
        allow_persistent_cache=True,
        password="hunter2",
        on_ready=lambda: None,
    )
    tasks.run(0)

    assert sessions.passwords == ["hunter2"]


def test_warmup_sends_no_password_for_an_unencrypted_document(tmp_path: Path) -> None:
    source = tmp_path / "plain.cb7"
    source.write_bytes(b"plain")
    tasks = _ManualTaskService()
    sessions = _FakeSessionService()
    coordinator = ArchiveWarmupCoordinator(
        sessions,  # type: ignore[arg-type]
        tasks,  # type: ignore[arg-type]
    )

    coordinator.acquire(
        source,
        "reader",
        document_cache_key="file:plain",
        allow_persistent_cache=True,
        on_ready=lambda: None,
    )
    tasks.run(0)

    assert sessions.passwords == [None]
