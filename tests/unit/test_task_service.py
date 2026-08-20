import logging
from threading import Event, get_ident

from joyread.core.operation_context import bind_operation, create_operation, current_operation
from joyread.app.tasking import TaskPriority, TaskStatus
from joyread.infrastructure.qt_task_service import TaskService


class _ImmediateThreadPool:
    def __init__(self) -> None:
        self.priorities: list[int] = []

    def setMaxThreadCount(self, _count: int) -> None:  # noqa: N802 - Qt-compatible fake.
        return

    def start(self, runnable, priority: int = 0) -> None:  # noqa: ANN001
        self.priorities.append(priority)
        runnable.run()

    def clear(self) -> None:
        return

    def waitForDone(self, _timeout_ms: int) -> bool:  # noqa: N802 - Qt-compatible fake.
        return True


def test_task_service_submit_runs_callback_on_background_pool(qtbot) -> None:
    service = TaskService(max_workers=1)
    results: list[str] = []

    handle = service.submit("work", lambda: "done", on_success=results.append)

    qtbot.waitUntil(lambda: handle.status == TaskStatus.COMPLETED, timeout=1000)
    assert handle.result == "done"
    assert results == ["done"]


def test_task_service_runs_work_off_gui_and_callbacks_on_gui_thread(qtbot) -> None:
    service = TaskService(max_workers=1)
    gui_thread = get_ident()
    worker_threads: list[int] = []
    callback_threads: list[int] = []

    handle = service.submit(
        "thread-affinity",
        lambda: worker_threads.append(get_ident()),
        on_success=lambda _result: callback_threads.append(get_ident()),
    )

    qtbot.waitUntil(lambda: handle.status == TaskStatus.COMPLETED, timeout=1000)
    assert worker_threads and worker_threads[0] != gui_thread
    assert callback_threads == [gui_thread]
    service.shutdown(timeout_ms=10)


def test_task_service_debug_logs_include_callback_name(qtbot, caplog) -> None:
    service = TaskService(max_workers=1)

    def work() -> str:
        return "done"

    with caplog.at_level(logging.DEBUG, logger="joyread.infrastructure.qt_task_service"):
        handle = service.submit("trace", work)
        qtbot.waitUntil(lambda: handle.status == TaskStatus.COMPLETED, timeout=1000)

    task_records = [record for record in caplog.records if getattr(record, "task_id", None) == "trace-1"]
    assert [getattr(record, "event", None) for record in task_records] == [
        "task.submitted",
        "task.worker.started",
        "task.worker.finished",
    ]
    assert all("work" in getattr(record, "callback", "") for record in task_records)
    service.shutdown(timeout_ms=10)


def test_task_service_submit_placeholder_remains_synchronous() -> None:
    service = TaskService(max_workers=1)

    handle = service.submit_placeholder("compat", lambda: 42)

    assert handle.status == TaskStatus.COMPLETED
    assert handle.result == 42


def test_task_service_streams_items_individually_at_requested_priority() -> None:
    pool = _ImmediateThreadPool()
    service = TaskService(max_workers=1, thread_pool=pool)  # type: ignore[arg-type]
    items: list[str] = []
    completed: list[str] = []

    def work(emit) -> str:  # noqa: ANN001
        emit("first")
        emit("second")
        return "done"

    handle = service.submit_stream(
        "stream",
        work,
        on_item=items.append,
        on_success=completed.append,
        priority=TaskPriority.HIGH,
    )

    assert pool.priorities == [int(TaskPriority.HIGH)]
    assert items == ["first", "second"]
    assert completed == ["done"]
    assert handle.status == TaskStatus.COMPLETED


def test_task_service_shutdown_cancels_active_and_queued_work(qtbot) -> None:
    service = TaskService(max_workers=1)
    started = Event()
    release = Event()
    results: list[str] = []

    def slow_task() -> str:
        started.set()
        release.wait(timeout=1)
        return "slow"

    running = service.submit("slow", slow_task, on_success=results.append)
    queued = service.submit("queued", lambda: "queued", on_success=results.append)
    assert started.wait(timeout=1)

    service.shutdown(timeout_ms=10)
    release.set()
    qtbot.wait(50)

    assert running.status == TaskStatus.CANCELLED
    assert queued.status == TaskStatus.CANCELLED
    assert results == []
    assert service.submit("late", lambda: "late").status == TaskStatus.CANCELLED


def test_cancelled_task_ignores_late_success_callback(qtbot, caplog) -> None:
    service = TaskService(max_workers=1)
    started = Event()
    release = Event()
    results: list[str] = []

    def slow_task() -> str:
        started.set()
        release.wait(timeout=1)
        return "done"

    with caplog.at_level(logging.DEBUG, logger="joyread.infrastructure.qt_task_service"):
        handle = service.submit("late", slow_task, on_success=results.append)
        assert started.wait(timeout=1)

        handle.cancel()
        release.set()
        qtbot.waitUntil(lambda: handle._signals is None, timeout=1000)

    assert handle.status == TaskStatus.CANCELLED
    assert results == []
    assert any(
        getattr(record, "event", None) == "task.worker.cancelled" for record in caplog.records
    )
    service.shutdown(timeout_ms=10)


def test_cancelled_task_discards_resource_result_on_worker(qtbot) -> None:
    service = TaskService(max_workers=1)
    started = Event()
    release = Event()
    discarded = Event()
    callback_threads: list[int] = []
    gui_thread = get_ident()

    def open_resource() -> object:
        started.set()
        release.wait(timeout=1)
        return object()

    def discard_resource(_resource: object) -> None:
        callback_threads.append(get_ident())
        discarded.set()

    handle = service.submit(
        "resource",
        open_resource,
        on_discard=discard_resource,
    )
    assert started.wait(timeout=1)

    handle.cancel()
    release.set()

    assert discarded.wait(timeout=1)
    assert callback_threads[0] != gui_thread
    qtbot.waitUntil(lambda: handle._signals is None, timeout=1000)
    service.shutdown(timeout_ms=10)


def test_quiesce_stops_new_work_and_resume_restores_it(qtbot) -> None:  # noqa: ANN001, ARG001
    """A storage transition needs the pool sealed, then usable again."""

    service = TaskService(max_workers=1)

    service.quiesce()
    refused = service.submit("after-quiesce", lambda: "work")
    assert refused.status == TaskStatus.CANCELLED, "quiesced work must not run"

    service.resume()
    results: list[str] = []
    handle = service.submit("after-resume", lambda: "work", on_success=results.append)
    qtbot.waitUntil(lambda: handle.status == TaskStatus.COMPLETED, timeout=1000)
    assert results == ["work"]
    service.shutdown()


def test_quiesce_cancels_running_work_without_joining(qtbot) -> None:  # noqa: ANN001, ARG001
    """Joining here would deadlock: handles are cleared on the GUI thread."""

    service = TaskService(max_workers=1)
    release = Event()
    started = Event()

    def blocked() -> str:
        started.set()
        release.wait(5)
        return "done"

    handle = service.submit("blocked", blocked)
    assert started.wait(2)

    pending = service.quiesce()

    assert handle.status == TaskStatus.CANCELLED
    assert pending >= 1, "the still-unwinding task must be reported, not hidden"
    release.set()
    qtbot.waitUntil(lambda: service.pending_task_count() == 0, timeout=2000)
    service.shutdown()


def test_shutdown_stays_terminal(qtbot) -> None:  # noqa: ANN001, ARG001
    """Quiesce is the reversible one; shutdown must not become reversible too."""

    service = TaskService(max_workers=1)
    service.shutdown()

    assert service.submit("after-shutdown", lambda: "work").status == TaskStatus.CANCELLED


def test_quiesce_does_not_strand_queued_tasks(qtbot) -> None:  # noqa: ANN001, ARG001
    """`QThreadPool.clear()` drops queued runnables before they can emit
    `finished`, so their handles stay registered forever and the drain never
    reaches zero -- every storage transition would then time out."""

    service = TaskService(max_workers=1)
    release = Event()
    started = Event()

    def blocked() -> str:
        started.set()
        release.wait(5)
        return "running"

    service.submit("running", blocked)
    assert started.wait(2)
    # Cannot start while the single worker is occupied, so this one is queued.
    service.submit("queued", lambda: "queued")

    service.quiesce()
    release.set()

    qtbot.waitUntil(lambda: service.pending_task_count() == 0, timeout=3000)
    service.shutdown()


def test_resume_cannot_reopen_a_shutdown_pool(qtbot) -> None:  # noqa: ANN001, ARG001
    """Shutdown runs after the services tasks depend on are gone, so letting
    resume reopen the pool would run work against torn-down state."""

    service = TaskService(max_workers=1)
    service.shutdown()

    service.resume()

    assert service.submit("after-resume", lambda: "work").status == TaskStatus.CANCELLED


def test_task_operation_context_reaches_worker_and_gui_callback(qtbot) -> None:
    service = TaskService(max_workers=1)
    parent = create_operation("reader.document.open", category="reader")
    seen: list[tuple[str | None, str | None]] = []

    def capture() -> str:
        operation = current_operation()
        seen.append((operation.operation_id if operation else None, operation.parent_operation_id if operation else None))
        return "done"

    def publish(_result: str) -> None:
        operation = current_operation()
        seen.append((operation.operation_id if operation else None, operation.parent_operation_id if operation else None))

    with bind_operation(parent):
        handle = service.submit("context", capture, on_success=publish)
    qtbot.waitUntil(lambda: handle.status == TaskStatus.COMPLETED, timeout=1000)

    assert handle.operation_context is not None
    assert seen == [
        (handle.operation_context.operation_id, parent.operation_id),
        (handle.operation_context.operation_id, parent.operation_id),
    ]
    service.shutdown(timeout_ms=10)


def test_task_service_contains_and_logs_success_callback_failure(qtbot, caplog) -> None:
    service = TaskService(max_workers=1)

    def fail_in_gui(_result: str) -> None:
        raise RuntimeError("GUI callback failed")

    with caplog.at_level(logging.ERROR, logger="joyread.infrastructure.qt_task_service"):
        handle = service.submit("callback-error", lambda: "done", on_success=fail_in_gui)
        qtbot.waitUntil(lambda: handle._signals is None, timeout=1000)

    assert handle.status == TaskStatus.FAILED
    assert isinstance(handle.error, RuntimeError)
    assert any(getattr(record, "event", None) == "task.ui_callback.failed" for record in caplog.records)
    service.shutdown(timeout_ms=10)


def test_task_service_contains_and_logs_stream_callback_failure(qtbot, caplog) -> None:
    service = TaskService(max_workers=1)

    def produce(emit) -> None:  # noqa: ANN001
        emit("item")

    def fail_in_gui(_item: str) -> None:
        raise RuntimeError("stream callback failed")

    with caplog.at_level(logging.ERROR, logger="joyread.infrastructure.qt_task_service"):
        handle = service.submit_stream("stream-error", produce, on_item=fail_in_gui)
        qtbot.waitUntil(lambda: handle._signals is None, timeout=1000)

    assert handle.status == TaskStatus.CANCELLED
    assert isinstance(handle.error, RuntimeError)
    assert any(getattr(record, "event", None) == "task.stream_callback.failed" for record in caplog.records)
    service.shutdown(timeout_ms=10)
