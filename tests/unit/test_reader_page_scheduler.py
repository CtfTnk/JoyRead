from __future__ import annotations

from dataclasses import dataclass, replace

from joyread.app.reader_page_scheduler import PageDemand, ReaderPageScheduler
from joyread.app.tasking import TaskHandle, TaskPriority, TaskStatus


@dataclass
class Pending:
    handle: TaskHandle
    work: object
    ready: object
    failed: object
    finished: object
    priority: TaskPriority


class ManualExecutor:
    def __init__(self):
        self.tasks: list[Pending] = []

    def submit(self, name, work, *, on_success, on_failure, on_finished, priority):
        handle = TaskHandle(name, status=TaskStatus.RUNNING)
        self.tasks.append(Pending(handle, work, on_success, on_failure, on_finished, priority))
        return handle

    def finish(self, index=0):
        task = self.tasks[index]
        if task.handle.status != TaskStatus.CANCELLED:
            try:
                result = task.work()
            except Exception as error:
                task.failed(error)
            else:
                task.ready(result)
            task.handle.status = TaskStatus.COMPLETED
        task.finished()


def demand(key, output, priority=TaskPriority.LOW):
    return PageDemand(key, priority, lambda _cancelled: key, output.append, lambda error: output.append(error))


def test_global_limit_reader_limit_and_cancelled_attempts_stay_charged():
    scheduler, executor, output = ReaderPageScheduler(), ManualExecutor(), []
    a, b, c = object(), object(), object()
    scheduler.replace(a, executor, [demand(('a', i), output) for i in range(6)])
    scheduler.replace(b, executor, [demand(('b', i), output) for i in range(6)])
    scheduler.replace(c, executor, [demand(('c', 0), output, TaskPriority.CRITICAL)])
    assert len(executor.tasks) == scheduler.active_count == 4
    assert scheduler.pending_count == 9
    scheduler.cancel(a)
    assert scheduler.active_count == 4
    executor.tasks[0].ready(('a', 0))  # A late result cannot escape retirement.
    assert output == []
    executor.finish(0)
    assert len(executor.tasks) == 5 and scheduler.active_count == 4
    assert executor.tasks[4].priority == TaskPriority.CRITICAL
    executor.finish(4)
    assert output == [('c', 0)]
    scheduler.close()
    for index in (1, 2, 3):
        executor.finish(index)
    assert scheduler.active_count == scheduler.pending_count == 0


def test_pending_prefetch_promotes_and_demotes_without_duplicate_work():
    scheduler, executor, output, owner = ReaderPageScheduler(), ManualExecutor(), [], object()
    scheduler.replace(owner, executor, [demand(i, output) for i in range(5)])
    scheduler.replace(owner, executor, [
        demand(i, output, TaskPriority.CRITICAL if i == 4 else TaskPriority.LOW) for i in range(5)
    ])
    executor.finish(0)
    executor.finish(2)
    assert output == [0, 4]
    # Move the foreground to 3 while 2 is still pending: current live demand wins.
    scheduler.replace(owner, executor, [demand(2, output), demand(3, output, TaskPriority.CRITICAL)])
    # The previously dispatched 2 was retained; cancellation of 1 must drain.
    executor.finish(1)
    assert executor.tasks[-1].priority == TaskPriority.CRITICAL
    executor.finish(len(executor.tasks) - 1)
    assert output[-1] == 3


def test_equal_priority_readers_rotate_after_each_dispatch():
    scheduler = ReaderPageScheduler(capacity=1)
    executor, output, a, b = ManualExecutor(), [], object(), object()
    scheduler.replace(a, executor, [demand(('a', i), output) for i in range(3)])
    scheduler.replace(b, executor, [demand(('b', i), output) for i in range(3)])
    for index in range(6):
        executor.finish(index)
    assert output == [('a', 0), ('b', 0), ('a', 1), ('b', 1), ('a', 2), ('b', 2)]
    assert scheduler.active_count == scheduler.pending_count == 0


def test_withdrawn_foreground_priority_is_not_sticky():
    scheduler, executor, output, owner = ReaderPageScheduler(capacity=1), ManualExecutor(), [], object()
    scheduler.replace(owner, executor, [demand(0, output)])
    scheduler.replace(owner, executor, [demand(0, output), demand(1, output, TaskPriority.CRITICAL), demand(2, output)])
    # 0 was already admitted. Replace the pending priority snapshot while it runs.
    scheduler.replace(owner, executor, [demand(0, output), demand(1, output), demand(2, output, TaskPriority.CRITICAL)])
    executor.finish(0)
    executor.finish(1)
    executor.finish(2)
    assert output == [0, 2, 1]


def test_library_quiesce_preserves_external_work_and_resume_accepts_fresh_demand():
    scheduler, library, external, output = ReaderPageScheduler(), ManualExecutor(), ManualExecutor(), []
    a, b = object(), object()
    scheduler.replace(a, library, [demand(i, output) for i in range(4)])
    scheduler.replace(b, external, [demand('external', output)])
    scheduler.quiesce(library)
    scheduler.replace(a, library, [demand('sealed', output)])
    external.finish()
    library.finish(0)
    library.finish(1)
    assert output == ['external']
    assert scheduler.active_count == scheduler.pending_count == 0
    scheduler.resume(library)
    scheduler.replace(a, library, [demand('resumed', output)])
    library.finish(2)
    assert output[-1] == 'resumed'


def test_rejected_and_inline_executors_do_not_leak_slots_or_recurse():
    class Inline:
        def submit(self, name, work, *, on_success, on_failure, on_finished, priority):
            on_success(work())
            on_finished()
            return TaskHandle(name, status=TaskStatus.COMPLETED)

    class Reject:
        def submit(self, name, work, *, on_success, on_failure, on_finished, priority):
            on_finished()
            return TaskHandle(name, status=TaskStatus.CANCELLED)

    for executor in (Inline(), Reject()):
        scheduler, output = ReaderPageScheduler(), []
        scheduler.replace(object(), executor, [demand(i, output) for i in range(100)])
        assert scheduler.active_count == scheduler.pending_count == 0
        assert output == (list(range(100)) if isinstance(executor, Inline) else [])


def test_callback_failure_still_releases_admission():
    scheduler, executor, output, owner = ReaderPageScheduler(capacity=1), ManualExecutor(), [], object()
    def fail(_result):
        raise RuntimeError('callback')
    scheduler.replace(owner, executor, [PageDemand(0, TaskPriority.LOW, lambda _: 0, fail, fail), demand(1, output)])
    try:
        executor.tasks[0].ready(0)
    except RuntimeError:
        pass
    executor.tasks[0].finished()
    executor.finish(1)
    assert output == [1] and scheduler.active_count == 0


def test_thumbnails_leave_capacity_for_body_and_wait_behind_body_prefetch():
    scheduler, executor, output = ReaderPageScheduler(), ManualExecutor(), []
    thumbs = [object() for _ in range(3)]
    for index, owner in enumerate(thumbs):
        scheduler.replace(owner, executor, [replace(demand(f'thumb{index}', output, TaskPriority.THUMBNAIL), is_thumbnail=True)])
    assert len(executor.tasks) == scheduler.active_count == 2
    body = object()
    scheduler.replace(body, executor, [demand(f'body{index}', output) for index in range(3)])
    assert scheduler.active_count == 4
    executor.finish(0)
    # The body owner has two active attempts. Its pending third page still
    # prevents another thumbnail from taking the newly vacant global slot.
    assert len(executor.tasks) == 4 and scheduler.active_count == 3
    executor.finish(2)
    assert executor.tasks[4].priority == TaskPriority.LOW
    executor.finish(4)
    assert output == ['thumb0', 'body0', 'body2']
    assert executor.tasks[5].priority == TaskPriority.THUMBNAIL


def test_body_and_topic_share_the_same_reader_limit():
    scheduler, executor, output, group = ReaderPageScheduler(), ManualExecutor(), [], object()
    scheduler.replace(object(), executor, [replace(demand(f'thumb{i}', output, TaskPriority.THUMBNAIL),
                                                is_thumbnail=True, group=group) for i in range(3)])
    scheduler.replace(object(), executor, [replace(demand('body', output, TaskPriority.CRITICAL), group=group)])
    assert scheduler.active_count == len(executor.tasks) == 2
    executor.finish(0)
    assert executor.tasks[2].priority == TaskPriority.CRITICAL
    assert scheduler.active_count == 2
    executor.finish(2)
    assert output == ['thumb0', 'body']


def test_cancelling_waiting_body_unblocks_thumbnails_without_releasing_active_slots():
    scheduler, executor, output = ReaderPageScheduler(), ManualExecutor(), []
    body, thumb = object(), object()
    scheduler.replace(body, executor, [demand(i, output) for i in range(3)])
    scheduler.replace(thumb, executor, [replace(demand('thumb', output, TaskPriority.THUMBNAIL), is_thumbnail=True)])
    assert len(executor.tasks) == 2
    scheduler.cancel(body)
    assert scheduler.active_count == 3 and len(executor.tasks) == 3
    executor.finish(2)
    assert output == ['thumb']
    executor.finish(0)
    executor.finish(1)
    assert scheduler.active_count == 0
