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

    def warm_disk_cache(
        self,
        path: Path,
        *,
        limits,
        document_cache_key: str,
        allow_persistent_cache: bool,
        chunk_size: int,
        is_cancelled,
    ) -> None:  # noqa: ANN001
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
