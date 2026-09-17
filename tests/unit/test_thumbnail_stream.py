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
    on_failure: Callable[[Exception], None]
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
        handle: TaskHandle[object] = TaskHandle(name, status=TaskStatus.RUNNING)
        self.tasks.append(_PendingStreamTask(handle, callback, on_item, on_success, on_failure, priority))
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


def test_thumbnail_stream_finishes_one_batch_then_uses_latest_viewport() -> None:
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

    assert tasks.tasks[0].handle.status == TaskStatus.RUNNING
    assert controller.active_indices == (5,)
    assert len(tasks.tasks) == 1

    tasks.run(0)
    assert ready == []
    assert controller.active_indices == (10,)
    assert len(tasks.tasks) == 2

    tasks.run(1)
    assert ready == [(10, b"page-10")]
    assert requested == [(5,), (10,)]


def test_overlap_keeps_work_and_does_not_redeliver_unchanged_pages() -> None:
    tasks = _ManualTaskService()
    controller = _controller(tasks)
    ready = []
    controller.thumbnail_ready.connect(lambda index, _data: ready.append(index))
    controller.set_source("book", 100, (100, 142), lambda indices, emit: [
        emit(ThumbnailStreamItem(index, b"image")) for index in indices
    ])
    controller.set_interest((4, 5, 6), (3, 7))
    controller.set_interest((5, 6, 7), (4, 8))
    tasks.run(0)
    assert ready == [5]
    assert controller.active_indices == (6,)
    controller.set_interest((6, 7, 8), (5, 9))
    assert ready == [5]
    assert len(tasks.tasks) == 2
    tasks.run(1)
    assert ready == [5, 6]
    # Leaving and re-entering interest must redeliver cached data to a view
    # that may have evicted its decoded image in the meantime.
    controller.set_interest((30,), ())
    controller.set_interest((5,), ())
    assert ready == [5, 6, 5]


def test_queue_tracks_direction_and_does_not_accumulate_workers() -> None:
    tasks = _ManualTaskService()
    controller = _controller(tasks)
    controller.set_source("book", 100, (100, 142), lambda indices, emit: [
        emit(ThumbnailStreamItem(index, b"image")) for index in indices
    ])
    controller.set_interest((4, 5, 6), (3, 7))
    for index in range(10, 30):
        controller.set_interest((index,), (index - 1, index + 1))
    assert len(tasks.tasks) == 1
    tasks.run(0)
    assert controller.active_indices == (29,)
    tasks.run(1)
    assert controller.active_indices == (30,)  # forward prefetch
    controller.set_interest((20,), (19, 21))
    tasks.run(2)
    assert controller.active_indices == (20,)
    tasks.run(3)
    assert controller.active_indices == (19,)  # reverse prefetch


def test_source_switch_rejects_old_results_and_captures_old_loader() -> None:
    tasks = _ManualTaskService()
    controller = _controller(tasks)
    ready, calls = [], []
    controller.thumbnail_ready.connect(lambda index, data: ready.append(data))
    def loader(name):
        def load(indices, emit):
            calls.append(name)
            for index in indices:
                emit(ThumbnailStreamItem(index, name.encode()))
        return load
    controller.set_source("old", 10, (100, 142), loader("old"))
    controller.set_interest((0,))
    controller.set_source("new", 10, (200, 284), loader("new"))
    controller.set_interest((0,))
    tasks.run(0)
    assert calls == ["old"] and ready == []
    tasks.run(1)
    assert ready == [b"new"]


def test_refresh_and_release_preserve_redelivery_contract() -> None:
    tasks = _ManualTaskService()
    controller = _controller(tasks)
    ready = []
    controller.thumbnail_ready.connect(lambda index, data: ready.append(index))
    controller.set_source("book", 10, (100, 142), lambda indices, emit: [
        emit(ThumbnailStreamItem(index, b"image")) for index in indices
    ])
    controller.set_interest((0,))
    tasks.run(0)
    controller.set_interest((0,))
    assert ready == [0]
    controller.refresh()
    assert ready == [0, 0]
    controller.release_interest()
    controller.set_interest((0,))
    assert ready == [0, 0, 0]


def test_failed_inflight_batch_continues_with_latest_demand() -> None:
    tasks = _ManualTaskService()
    controller = _controller(tasks)
    failed = []
    controller.thumbnail_failed.connect(failed.append)
    controller.set_source("book", 100, (100, 142), lambda *_: None)
    controller.set_interest((1,))
    controller.set_interest((40,), (39, 41))
    tasks.tasks[0].on_failure(OSError("unreadable old page"))
    assert failed == []
    assert controller.active_indices == (40,)
    controller.release_interest()
    tasks.run(1)
    assert controller.active_indices == () and len(tasks.tasks) == 2


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
