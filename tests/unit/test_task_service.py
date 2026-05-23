import logging
from threading import Event

from joyread.core.services.task_service import TaskService, TaskStatus


def test_task_service_submit_runs_callback_on_background_pool(qtbot) -> None:
    service = TaskService(max_workers=1)
    results: list[str] = []

    handle = service.submit("work", lambda: "done", on_success=results.append)

    qtbot.waitUntil(lambda: handle.status == TaskStatus.COMPLETED, timeout=1000)
    assert handle.result == "done"
    assert results == ["done"]


def test_task_service_debug_logs_include_callback_name(qtbot, caplog) -> None:
    service = TaskService(max_workers=1)

    def work() -> str:
        return "done"

    with caplog.at_level(logging.DEBUG, logger="joyread.core.services.task_service"):
        handle = service.submit("trace", work)
        qtbot.waitUntil(lambda: handle.status == TaskStatus.COMPLETED, timeout=1000)

    messages = [record.getMessage() for record in caplog.records]
    assert any("Task trace-1 submitted callback=" in message and "work" in message for message in messages)
    assert any("Task trace-1 starting callback=" in message and "work" in message for message in messages)
    assert any("Task trace-1 completed callback=" in message and "work" in message for message in messages)
    service.shutdown(timeout_ms=10)


def test_task_service_submit_placeholder_remains_synchronous() -> None:
    service = TaskService(max_workers=1)

    handle = service.submit_placeholder("compat", lambda: 42)

    assert handle.status == TaskStatus.COMPLETED
    assert handle.result == 42


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


def test_cancelled_task_ignores_late_success_callback(qtbot) -> None:
    service = TaskService(max_workers=1)
    started = Event()
    release = Event()
    results: list[str] = []

    def slow_task() -> str:
        started.set()
        release.wait(timeout=1)
        return "done"

    handle = service.submit("late", slow_task, on_success=results.append)
    assert started.wait(timeout=1)

    handle.cancel()
    release.set()
    qtbot.waitUntil(lambda: handle._signals is None, timeout=1000)

    assert handle.status == TaskStatus.CANCELLED
    assert results == []
    service.shutdown(timeout_ms=10)
