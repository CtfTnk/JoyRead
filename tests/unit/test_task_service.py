from joyread.core.services.task_service import TaskService, TaskStatus


def test_task_service_submit_runs_callback_on_background_pool(qtbot) -> None:
    service = TaskService(max_workers=1)
    results: list[str] = []

    handle = service.submit("work", lambda: "done", on_success=results.append)

    qtbot.waitUntil(lambda: handle.status == TaskStatus.COMPLETED, timeout=1000)
    assert handle.result == "done"
    assert results == ["done"]


def test_task_service_submit_placeholder_remains_synchronous() -> None:
    service = TaskService(max_workers=1)

    handle = service.submit_placeholder("compat", lambda: 42)

    assert handle.status == TaskStatus.COMPLETED
    assert handle.result == 42
