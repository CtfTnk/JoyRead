from __future__ import annotations

from dataclasses import dataclass

import pytest

from joyread.app.reader_page_scheduler import ReaderPageScheduler
from joyread.app.tasking import TaskHandle, TaskStatus
from joyread.app.thumbnail_stream import ThumbnailStreamController, ThumbnailStreamItem
from joyread.core.services.cache_service import SharedThumbnailCache, ThumbnailCacheKey


@dataclass
class Attempt:
    handle: TaskHandle
    work: object
    ready: object
    failed: object
    finished: object


class Executor:
    def __init__(self):
        self.tasks = []
        self.streams = []

    def submit(self, name, work, *, on_success, on_failure, on_finished, priority):
        handle = TaskHandle(name, status=TaskStatus.RUNNING)
        self.tasks.append(Attempt(handle, work, on_success, on_failure, on_finished))
        return handle

    def submit_stream(self, name, work, *, on_item, on_success, on_failure, priority):
        handle = TaskHandle(name, status=TaskStatus.RUNNING)
        self.streams.append((work, on_item, on_success, handle))
        return handle

    def complete(self, index=0, *, late=None):
        attempt = self.tasks[index]
        try:
            if late is not None:
                attempt.ready(late)
            elif attempt.handle.status != TaskStatus.CANCELLED:
                attempt.ready(attempt.work())
                attempt.handle.status = TaskStatus.COMPLETED
        finally:
            attempt.finished()


def setup(*, capacity=4):
    executor, scheduler, cache = Executor(), ReaderPageScheduler(capacity=capacity), SharedThumbnailCache(4096)
    client = cache.issue_client('topic')
    controller = ThumbnailStreamController(executor, client, task_name='test',
                                          random_page_scheduler=scheduler, random_page_executor=executor)
    delivered = []
    controller.thumbnail_ready.connect(lambda page, data: delivered.append((page, data)))
    return controller, executor, scheduler, client, delivered


def loader(indices, emit, cancelled=None):
    for index in indices:
        emit(ThumbnailStreamItem(index, f'page{index}'.encode()))


def test_random_pages_use_shared_pool_and_solid_pages_keep_one_serial_stream():
    controller, executor, scheduler, cache, delivered = setup()
    controller.set_source('mixed', 8, (10, 20), loader, allows_parallel=lambda index: index % 2 == 0)
    controller.set_interest((0, 1, 2, 3), (4, 5))
    assert len(executor.tasks) == 2 and len(executor.streams) == 1
    assert set(controller.active_indices) == {0, 1, 2}
    work, emit, done, handle = executor.streams[0]
    work(emit)
    handle.status = TaskStatus.COMPLETED
    done(None)
    assert len(executor.streams) == 2
    executor.complete(0)
    executor.complete(1)
    executor.complete(2)
    assert {index for index, _ in delivered} >= {0, 1, 2, 4}


@pytest.mark.parametrize('capability', [None, lambda _index: False])
def test_unknown_pdf_and_solid_never_enter_shared_pool(capability):
    controller, executor, scheduler, _, _ = setup()
    controller.set_source('legacy', 8, (10, 20), loader, allows_parallel=capability)
    controller.set_interest((0, 1, 2))
    assert not executor.tasks and len(executor.streams) == 1 and scheduler.active_count == 0


def test_scroll_retires_obsolete_requests_without_releasing_slots_early():
    controller, executor, scheduler, cache, delivered = setup()
    controller.set_source('book', 20, (10, 20), loader, allows_parallel=lambda _: True)
    controller.set_interest((0, 1, 2))
    controller.set_interest((8, 9), (10,))
    assert scheduler.active_count == 2 and len(executor.tasks) == 2
    executor.complete(0, late=(ThumbnailStreamItem(1, b'stale'),))
    assert delivered == [] and cache.get(ThumbnailCacheKey('book', 1, 10, 20)) is None
    executor.complete(1)
    executor.complete(2)
    executor.complete(3)
    assert delivered == [(8, b'page8'), (9, b'page9')]


@pytest.mark.parametrize('change', ['source', 'size', 'promote', 'release'])
def test_identity_changes_prevent_late_cache_writes(change):
    controller, executor, scheduler, cache, delivered = setup(capacity=1)
    controller.set_source('old', 5, (10, 20), loader, allows_parallel=lambda _: True)
    controller.set_interest((0,))
    if change == 'release':
        controller.release_interest()
    elif change == 'promote':
        controller.promote_source('new')
    else:
        controller.set_source('new' if change == 'source' else 'old', 5,
                              (20, 40) if change == 'size' else (10, 20), loader,
                              allows_parallel=lambda _: True)
        controller.set_interest((0,))
    executor.complete(0, late=(ThumbnailStreamItem(0, b'old'),))
    assert delivered == [] and cache.get(ThumbnailCacheKey('old', 0, 10, 20)) is None
    if change != 'release':
        executor.complete(1)
        assert delivered == [(0, b'page0')]
    assert scheduler.active_count == 0


def test_preview_promoted_to_topic_uses_topic_loader_and_cached_redelivery():
    controller, executor, _, _, delivered = setup(capacity=1)
    controller.set_source('book', 5, (10, 20), loader, allows_parallel=lambda _: True)
    controller.set_preview(2, lambda *_: None)
    controller.set_interest((2,))
    executor.complete(0, late=())
    executor.complete(1)
    assert delivered == [(2, b'page2')]
    controller.refresh()
    assert delivered == [(2, b'page2'), (2, b'page2')]
    assert len(executor.tasks) == 2


def test_overlapping_scroll_keeps_same_page_attempt():
    controller, executor, _, _, delivered = setup(capacity=1)
    controller.set_source('book', 5, (10, 20), loader, allows_parallel=lambda _: True)
    controller.set_interest((1,))
    controller.set_interest((1, 2))
    assert len(executor.tasks) == 1 and executor.tasks[0].handle.status == TaskStatus.RUNNING
    executor.complete(0)
    executor.complete(1)
    assert delivered == [(1, b'page1'), (2, b'page2')]
