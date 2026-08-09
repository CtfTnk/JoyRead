"""Focused tests for viewport-driven thumbnail scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from joyread.core.services.cache_service import SharedThumbnailCache
from joyread.app.tasking import TaskHandle, TaskPriority, TaskStatus
from joyread.ui.viewmodels.thumbnail_stream import ThumbnailStreamController, ThumbnailStreamItem


@dataclass
class _PendingStreamTask:
    handle: TaskHandle[object]
    work: Callable[[Callable[[ThumbnailStreamItem], None]], None]
    on_item: Callable[[ThumbnailStreamItem], None]
    on_success: Callable[[object], None]
    priority: TaskPriority | int


class _ManualTaskService:
    def __init__(self) -> None:
        self.tasks: list[_PendingStreamTask] = []

    def submit_stream(
        self,
        name: str,
        callback,
        *,
        on_item,
        on_success,
        on_failure,
        priority,
    ) -> TaskHandle[object]:  # noqa: ANN001 - mirrors TaskService test double.
        del on_failure
        handle: TaskHandle[object] = TaskHandle(name, status=TaskStatus.RUNNING)
        self.tasks.append(_PendingStreamTask(handle, callback, on_item, on_success, priority))
        return handle

    def run(self, index: int) -> None:
        task = self.tasks[index]
        task.work(task.on_item)
        if task.handle.status != TaskStatus.CANCELLED:
            task.handle.status = TaskStatus.COMPLETED
        # Deliberately deliver completion even after cancellation. The stream
        # generation token, not this cooperative fake, must reject late work.
        task.on_success(None)


def _controller(task_service: _ManualTaskService) -> ThumbnailStreamController:
    cache = SharedThumbnailCache(max_bytes=1024)
    return ThumbnailStreamController(
        task_service,  # type: ignore[arg-type]
        cache.issue_client("test"),
        task_name="thumbnail",
    )


def test_thumbnail_stream_prioritizes_center_and_drops_old_viewport_results() -> None:
    tasks = _ManualTaskService()
    controller = _controller(tasks)
    requested: list[tuple[int, ...]] = []
    ready: list[tuple[int, bytes]] = []

    def loader(indices: tuple[int, ...], emit) -> None:  # noqa: ANN001
        requested.append(indices)
        for page_index in indices:
            emit(ThumbnailStreamItem(page_index, f"page-{page_index}".encode()))

    controller.thumbnail_ready.connect(lambda page_index, data: ready.append((page_index, data)))
    controller.set_source("book", 20, (100, 142), loader)
    controller.set_interest((4, 5, 6), (3, 7))

    assert controller.active_indices == (5,)
    assert len(tasks.tasks) == 1
    assert tasks.tasks[0].priority == TaskPriority.HIGH

    controller.set_interest((10,), (9, 11))

    assert tasks.tasks[0].handle.status == TaskStatus.CANCELLED
    assert controller.active_indices == (10,)
    assert len(tasks.tasks) == 2

    tasks.run(0)
    assert ready == []

    tasks.run(1)
    assert ready == [(10, b"page-10")]
    assert requested == [(5,), (10,)]


def test_thumbnail_stream_batches_cold_archive_io_but_emits_each_item() -> None:
    tasks = _ManualTaskService()
    controller = _controller(tasks)
    ready: list[int] = []

    def loader(indices: tuple[int, ...], emit) -> None:  # noqa: ANN001
        for page_index in indices:
            emit(ThumbnailStreamItem(page_index, bytes([page_index])))

    controller.thumbnail_ready.connect(lambda page_index, _data: ready.append(page_index))
    controller.set_source("cold-book", 20, (100, 142), loader, batch_size_for=lambda _index: 8)
    controller.set_interest((2, 3, 4), (1, 5, 6, 7, 8, 9))

    assert len(tasks.tasks) == 1
    assert controller.active_indices == (3, 2, 4, 1, 5, 6, 7, 8)

    tasks.run(0)

    assert ready == [3, 2, 4, 1, 5, 6, 7, 8]
    assert len(tasks.tasks) == 2
    assert controller.active_indices == (9,)


def test_thumbnail_stream_uses_memory_bounded_batch_planner() -> None:
    tasks = _ManualTaskService()
    controller = _controller(tasks)
    planned_candidates: list[tuple[int, ...]] = []

    def planner(candidates: tuple[int, ...]) -> tuple[int, ...]:
        planned_candidates.append(candidates)
        return candidates[:2]

    controller.set_source(
        "bounded-book",
        20,
        (100, 142),
        lambda indices, emit: [emit(ThumbnailStreamItem(index, b"page")) for index in indices],
        batch_size_for=lambda _index: 8,
        batch_planner=planner,
    )
    controller.set_interest((2, 3, 4), (1, 5, 6, 7, 8, 9))

    assert planned_candidates == [(3, 2, 4, 1, 5, 6, 7, 8)]
    assert controller.active_indices == (3, 2)
