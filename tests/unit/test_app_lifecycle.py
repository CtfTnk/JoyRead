from __future__ import annotations

import inspect
import logging
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFileOpenEvent
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow, QPushButton

from joyread.app import bootstrap
from joyread.app.app_context import AppContext, StorageTransition
from joyread.app.bootstrap import create_application
from joyread.app.launch.file_open_router import FileOpenRouter
from joyread.app.launch.coordinator import LaunchCoordinator
from joyread.app.launch.intent import LaunchIntent
from joyread.core.file_types import SUPPORTED_READER_EXTENSIONS
from joyread.core.services.storage_recovery_service import StorageRecoveryCancelled
from joyread.infrastructure.logging.logging_service import shutdown_logging
from joyread.ui.dialogs.storage_recovery_dialog import StorageRecoveryDialog
from joyread.ui.views import main_window as main_window_module
from joyread.ui.views.main_window import MainWindow
from joyread.ui.views.reader_window import ReaderWindow


@pytest.fixture(autouse=True)
def _shutdown_logging_runtime_after_each_test():
    """create_application() starts the async logging runtime's writer thread.

    Nothing in this test file calls app.exec(), so Qt's aboutToQuit -- the
    only production trigger for shutdown_logging() -- never fires. Without
    this, a test that calls create_application() leaves a live writer thread
    and a replaced root logger behind for every later test in the process.
    shutdown_logging() is a no-op when the runtime was never started, so this
    is harmless for tests that don't touch logging at all.
    """

    yield
    shutdown_logging(timeout_seconds=2.0)


class _ManualLaunchGate:
    """A launch gate the test releases explicitly, standing in for the platform."""

    def __init__(self) -> None:
        self._callback = None

    def when_ready(self, callback) -> None:  # noqa: ANN001
        self._callback = callback

    def release(self) -> None:
        callback, self._callback = self._callback, None
        if callback is not None:
            callback()


class _RecordingWindowSink:
    """Minimal LaunchWindowSink that records what the coordinator asked for."""

    def __init__(self, qtbot) -> None:  # noqa: ANN001
        self._qtbot = qtbot
        self.main_windows: list[QMainWindow] = []
        self.reader_windows: list[QMainWindow] = []
        self.opened_paths: list[Path] = []

    def show_library(self) -> QMainWindow:
        window = QMainWindow()
        self._qtbot.addWidget(window)
        self.main_windows.append(window)
        return window

    def open_files(self, paths) -> tuple[QMainWindow, ...]:  # noqa: ANN001
        windows: list[QMainWindow] = []
        for path in paths:
            self.opened_paths.append(path)
            window = QMainWindow()
            self._qtbot.addWidget(window)
            self.reader_windows.append(window)
            windows.append(window)
        return tuple(windows)


class _RecordingTaskService:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def shutdown(self) -> None:
        self._calls.append("task")


class _RecordingDatabase:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def close(self) -> None:
        self._calls.append("database")


class _FailingTaskService(_RecordingTaskService):
    def shutdown(self) -> None:
        self._calls.append("task")
        raise RuntimeError("task shutdown failed")


def _close_test_context(calls: list[str], task_service=None) -> AppContext:  # noqa: ANN001
    return AppContext(
        config=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        settings_store=None,  # type: ignore[arg-type]
        paths=None,  # type: ignore[arg-type]
        resources=None,  # type: ignore[arg-type]
        database_interpreter=_RecordingDatabase(calls),  # type: ignore[arg-type]
        book_repository=None,  # type: ignore[arg-type]
        tag_repository=None,  # type: ignore[arg-type]
        archive_extraction_pool=None,  # type: ignore[arg-type]
        archive_image_service=None,  # type: ignore[arg-type]
        reader_session_service=None,  # type: ignore[arg-type]
        pdf_image_service=None,  # type: ignore[arg-type]
        library_service=None,  # type: ignore[arg-type]
        task_service=task_service or _RecordingTaskService(calls),  # type: ignore[arg-type]
        cache_service=None,  # type: ignore[arg-type]
        hash_service=None,  # type: ignore[arg-type]
        tag_service=None,  # type: ignore[arg-type]
        import_service=None,  # type: ignore[arg-type]
        library_maintenance_coordinator=None,  # type: ignore[arg-type]
        library_maintenance_service=None,  # type: ignore[arg-type]
        export_service=None,  # type: ignore[arg-type]
        storage_migration_service=None,  # type: ignore[arg-type]
        storage_validation_service=None,  # type: ignore[arg-type]
        storage_recovery_service=None,  # type: ignore[arg-type]
        thumbnail_service=None,  # type: ignore[arg-type]
        hidden_space_service=None,  # type: ignore[arg-type]
        main_window_viewmodel=None,  # type: ignore[arg-type]
        shelf_viewmodel=None,  # type: ignore[arg-type]
        settings_viewmodel=None,  # type: ignore[arg-type]
        tag_management_viewmodel=None,  # type: ignore[arg-type]
    )


def test_app_context_closes_tasks_before_database() -> None:
    calls: list[str] = []
    context = _close_test_context(calls)

    context.close()

    assert calls == ["task", "database"]


def test_app_context_logs_component_failure_and_finishes_remaining_shutdown(
    caplog,
) -> None:
    calls: list[str] = []
    context = _close_test_context(calls, _FailingTaskService(calls))

    with caplog.at_level(logging.INFO, logger="joyread.app.app_context"):
        context.close()

    assert calls == ["task", "database"]
    assert any(
        getattr(record, "event", None) == "app_context.component_close.failed"
        and getattr(record, "component", None) == "task_service"
        for record in caplog.records
    )
    terminal = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "app_context.close.finished"
    )
    assert terminal.status == "completed_with_errors"


def test_direct_external_open_uses_reader_window_without_file_dialog(qtbot, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    source = tmp_path / "direct.cbz"
    source.write_bytes(b"")

    def fail_file_dialog(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("Direct external open must not invoke QFileDialog.")

    monkeypatch.setattr(main_window_module.QFileDialog, "getOpenFileName", fail_file_dialog)

    app, context, window = create_application(["joyread", str(source)])

    assert app.quitOnLastWindowClosed()
    assert isinstance(window, ReaderWindow)
    assert not isinstance(window, MainWindow)

    window.close()
    context.close()


def test_direct_external_open_accepts_pdf(qtbot, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    source = tmp_path / "direct.pdf"
    source.write_bytes(b"%PDF")

    app, context, window = create_application(["joyread", str(source)])

    assert app.quitOnLastWindowClosed()
    assert isinstance(window, ReaderWindow)
    assert not isinstance(window, MainWindow)

    window.close()
    context.close()


def test_direct_external_open_does_not_accept_epub(qtbot, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    source = tmp_path / "direct.epub"
    source.write_bytes(b"")

    app, context, window = create_application(["joyread", str(source)])

    assert app.quitOnLastWindowClosed()
    assert isinstance(window, MainWindow)
    assert ".epub" not in SUPPORTED_READER_EXTENSIONS

    window.close()
    context.close()


def test_file_open_event_is_queued_then_dispatched_without_router_ownership(qtbot, tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication(["test"])
    previous_router = getattr(app, "_joyread_file_open_router", None)
    if isinstance(previous_router, FileOpenRouter):
        previous_router.dispose()

    source = tmp_path / "finder-open.cbz"
    source.write_bytes(b"")
    router = FileOpenRouter(app, SUPPORTED_READER_EXTENSIONS)

    queued_event = QFileOpenEvent(str(source))
    assert QApplication.sendEvent(app, queued_event)
    assert router.take_pending_path() == source

    opened_paths: list[Path] = []
    opened_windows: list[QMainWindow] = []

    def open_file(path: Path) -> None:
        opened_paths.append(path)
        window = QMainWindow()
        qtbot.addWidget(window)
        opened_windows.append(window)

    router.set_open_handler(open_file)
    live_event = QFileOpenEvent(str(source))
    assert QApplication.sendEvent(app, live_event)

    assert opened_paths == [source]
    assert opened_windows[0].isWindow()

    opened_windows[0].close()
    router.dispose()


def test_startup_file_open_event_creates_reader_without_main_window(qtbot, tmp_path: Path) -> None:
    """A document delivered before the gate resolves must replace the Library.

    This is the macOS cold-launch shape: argv is empty, and the document only
    arrives once Qt is running. Holding the gate open reproduces that ordering
    deterministically, with no timer to race.
    """

    app = QApplication.instance() or QApplication(["test"])
    previous_router = getattr(app, "_joyread_file_open_router", None)
    if isinstance(previous_router, FileOpenRouter):
        previous_router.dispose()

    source = tmp_path / "finder-cold-start.cbz"
    source.write_bytes(b"")
    sink = _RecordingWindowSink(qtbot)
    gate = _ManualLaunchGate()

    router = FileOpenRouter(app, SUPPORTED_READER_EXTENSIONS)
    coordinator = LaunchCoordinator(windows=sink, gate=gate)
    coordinator.start()
    router.set_open_handler(coordinator.open_file)

    assert QApplication.sendEvent(app, QFileOpenEvent(str(source)))
    assert not coordinator.ready
    assert sink.main_windows == []
    assert sink.reader_windows == []

    gate.release()

    assert sink.opened_paths == [source]
    assert sink.main_windows == []
    assert coordinator.initial_window is sink.reader_windows[0]
    assert coordinator.ready

    sink.reader_windows[0].close()
    router.dispose()
    coordinator.deleteLater()


def test_startup_shows_library_when_gate_resolves_with_no_document(qtbot) -> None:
    sink = _RecordingWindowSink(qtbot)
    gate = _ManualLaunchGate()
    coordinator = LaunchCoordinator(windows=sink, gate=gate)

    coordinator.start()
    assert sink.main_windows == []

    gate.release()

    assert coordinator.initial_window is sink.main_windows[0]
    assert sink.reader_windows == []
    assert coordinator.ready

    sink.main_windows[0].close()
    coordinator.deleteLater()


def test_run_exits_cleanly_when_storage_recovery_is_closed(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str] | None] = []

    def cancel_startup(environment):  # noqa: ANN001
        calls.append(environment.argv)
        raise StorageRecoveryCancelled

    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap, "_build_primary_runtime", cancel_startup)
    monkeypatch.setattr(
        bootstrap.SingleInstanceBroker,
        "start",
        lambda _broker, _intent: bootstrap.InstanceRole.PRIMARY,
    )

    assert bootstrap.run(["joyread"]) == 0
    assert calls == [["joyread"]]


def test_secondary_run_forwards_before_app_context_is_created(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "forwarded.cbz"
    source.write_bytes(b"")
    forwarded: list[LaunchIntent] = []
    disposed: list[bool] = []

    class SecondaryBroker:
        def __init__(self, *_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        def start(self, intent_factory):  # noqa: ANN001, ANN201
            forwarded.append(intent_factory())
            return bootstrap.InstanceRole.SECONDARY

        def dispose(self) -> None:
            disposed.append(True)

    def fail_primary_runtime(_environment):  # noqa: ANN001
        raise AssertionError("A secondary process must not create AppContext.")

    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(bootstrap, "SingleInstanceBroker", SecondaryBroker)
    monkeypatch.setattr(bootstrap, "_build_primary_runtime", fail_primary_runtime)

    assert bootstrap.run(["joyread", str(source), str(tmp_path / "ignored.epub")]) == 0
    assert forwarded == [LaunchIntent.open_files((source,))]
    assert disposed == [True]
    assert not (tmp_path / "runtime" / "JoyRead-Library").exists()


def test_secondary_forwards_a_document_that_arrives_after_arbitration(
    qtbot, tmp_path: Path
) -> None:
    """A macOS secondary must wait for Finder rather than forwarding SHOW_LIBRARY.

    Models the real cold-launch ordering: argv is empty, the document is
    delivered once an event loop runs, and only then does the platform report
    that the launch is complete. Resolving from argv alone silently downgrades
    "open this manga" to "show the Library".
    """

    source = tmp_path / "late-finder-document.cbz"
    source.write_bytes(b"")

    class _StubApplication:
        """Stands in for JoyReadApplication's startup document buffer."""

        def __init__(self) -> None:
            self._paths: tuple[Path, ...] = ()

        def deliver(self, path: Path) -> None:
            self._paths = (*self._paths, path)

        def take_startup_file_open_paths(self) -> tuple[Path, ...]:
            paths, self._paths = self._paths, ()
            return paths

    application = _StubApplication()

    class _DeferredGate:
        """Resolves only once an event loop runs, like the macOS gate."""

        def when_ready(self, callback) -> None:  # noqa: ANN001
            def deliver_then_signal() -> None:
                application.deliver(source)  # QFileOpenEvent
                callback()  # NSApplicationDidFinishLaunching
            QTimer.singleShot(0, deliver_then_signal)

    environment = bootstrap._StartupEnvironment(
        argv=["joyread"],
        app=application,  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
        settings_store=None,  # type: ignore[arg-type]
        gate=_DeferredGate(),
        startup_intent=None,
    )

    intent = bootstrap._resolve_secondary_intent(environment)

    assert intent == LaunchIntent.open_files((source,))


def test_secondary_forwards_show_library_for_a_plain_launch(qtbot) -> None:
    class _ImmediateGate:
        def when_ready(self, callback) -> None:  # noqa: ANN001
            callback()

    environment = bootstrap._StartupEnvironment(
        argv=["joyread"],
        app=QApplication.instance(),  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
        settings_store=None,  # type: ignore[arg-type]
        gate=_ImmediateGate(),
        startup_intent=None,
    )

    assert bootstrap._resolve_secondary_intent(environment) == LaunchIntent.show_library()


def test_storage_recovery_dialog_close_rejects_and_width_is_stable(qtbot) -> None:
    QApplication.instance() or QApplication(["test"])
    dialog = StorageRecoveryDialog("/missing/JoyRead", "No database found.")
    qtbot.addWidget(dialog)

    assert dialog.minimumWidth() >= 500
    assert sorted(button.text() for button in dialog.findChildren(QPushButton)) == [
        "Initialize",
        "Select",
    ]

    QTimer.singleShot(0, dialog.close)

    assert dialog.exec() == QDialog.DialogCode.Rejected


def test_launch_window_is_centered_on_available_screen(qtbot) -> None:
    QApplication.instance() or QApplication(["test"])
    window = QMainWindow()
    qtbot.addWidget(window)
    window.resize(320, 200)

    bootstrap.center_window_on_launch(window)

    screen = window.screen() or QApplication.primaryScreen()
    assert screen is not None
    screen_center = screen.availableGeometry().center()
    window_center = window.frameGeometry().center()

    assert abs(window_center.x() - screen_center.x()) <= 1
    assert abs(window_center.y() - screen_center.y()) <= 1


def test_native_file_dialogs_are_kept_for_in_app_picker_paths() -> None:
    main_window_source = inspect.getsource(main_window_module)
    bootstrap_source = inspect.getsource(bootstrap)

    assert "QFileDialog.getOpenFileName" in main_window_source
    assert "QFileDialog.getOpenFileNames" in main_window_source
    assert "QFileDialog.getExistingDirectory" in main_window_source
    assert "DontUseNativeDialog" not in main_window_source
    assert "AA_DontUseNativeDialogs" not in bootstrap_source


class _QuiesceRecordingTaskService:
    def __init__(self, calls: list[str], pending: int = 0) -> None:
        self._calls = calls
        self._pending = pending

    def quiesce(self) -> int:
        self._calls.append("task-quiesce")
        return self._pending

    def pending_task_count(self) -> int:
        return self._pending

    def resume(self) -> None:
        self._calls.append("task-resume")

    def shutdown(self) -> None:  # pragma: no cover - not exercised here.
        self._calls.append("task")


class _RecordingWarmupCoordinator:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def close(self) -> None:
        self._calls.append("warmup")

    def reset(self) -> None:
        self._calls.append("warmup-reset")


class _RecordingThumbnailService:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def close(self) -> None:
        self._calls.append("thumbnails")


def _quiesce_context(calls: list[str], pending: int = 0) -> AppContext:
    fields = {name: None for name in AppContext.__dataclass_fields__}
    fields.update(
        task_service=_QuiesceRecordingTaskService(calls, pending),
        archive_warmup_coordinator=_RecordingWarmupCoordinator(calls),
        thumbnail_service=_RecordingThumbnailService(calls),
    )
    return AppContext(**fields)  # type: ignore[arg-type]


def test_quiesce_stops_warmup_before_cancelling_tasks() -> None:
    """Warmup owns the extractor subprocess, so its cancellation has to reach
    the child before anything else is torn down around it."""

    calls: list[str] = []

    _quiesce_context(calls).quiesce_for_storage_transition()

    assert calls == ["warmup", "task-quiesce"]


def test_quiesce_leaves_the_thumbnail_service_usable_until_commit() -> None:
    """`ThumbnailService.close()` is terminal, so a drain that times out must
    not have run it -- otherwise abandoning would leave a dead service."""

    calls: list[str] = []
    context = _quiesce_context(calls)

    context.quiesce_for_storage_transition()
    assert "thumbnails" not in calls

    context.commit_storage_transition()
    assert "thumbnails" in calls
    assert context.storage_rebuild_required is True, (
        "every outcome past the commit point has to rebuild, even one that "
        "never touched storage"
    )


def test_committing_resets_the_warmup_coordinator_rather_than_muting_it() -> None:
    """The drain cancelled the warmup task, which suppresses the callback that
    would clear `_active_key`. Left set, it blocks every later warmup."""

    calls: list[str] = []
    context = _quiesce_context(calls)

    context.quiesce_for_storage_transition()
    assert calls == ["warmup", "task-quiesce"]

    context.commit_storage_transition()
    assert "warmup-reset" in calls


def test_abandoning_a_transition_resumes_without_closing_anything() -> None:
    calls: list[str] = []
    context = _quiesce_context(calls, pending=2)

    context.quiesce_for_storage_transition()
    context.abandon_storage_transition()

    assert calls == ["warmup", "task-quiesce", "warmup-reset", "task-resume"]
    assert "thumbnails" not in calls, "an abandoned transition must be fully reversible"


def test_abandoning_a_transition_leaves_warmup_able_to_run_again() -> None:
    """The abandon path reaches the same stuck coordinator as the commit path.

    `close()` withdraws consumers and the quiesce then cancels the running
    task, suppressing the callback that clears `_active_key`. Without a reset
    here, a drain that timed out would silently kill archive warmup for the
    rest of the session -- while leaving everything else working, so nothing
    would point at the cause.
    """

    calls: list[str] = []
    context = _quiesce_context(calls, pending=2)

    context.quiesce_for_storage_transition()
    context.abandon_storage_transition()

    assert "warmup-reset" in calls


class _RecordingLease:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def release(self) -> None:
        self._calls.append("lease-released")


def test_a_rejected_select_still_rebuilds_what_the_commit_closed() -> None:
    """`reload_required` answers "did storage change", which is not the same
    question as "does this need rebuilding".

    A Select rejected by validation changes nothing on disk and reports
    False -- but the commit phase already closed the thumbnail service for
    good, so resuming without a rebuild leaves the app holding a dead one.
    """

    calls: list[str] = []
    context = _quiesce_context(calls)
    rebuilt: list[str] = []
    context.reload_storage_from_settings = lambda: rebuilt.append("rebuilt")  # type: ignore[method-assign]

    context.quiesce_for_storage_transition()
    context.commit_storage_transition()
    rejected = StorageTransition(
        "storage-select",
        _RecordingLease(calls),  # type: ignore[arg-type]
        result=None,
        reload_required=False,
    )
    context.finish_storage_transition(rejected)

    assert rebuilt == ["rebuilt"], "a post-commit outcome must rebuild"
    assert context.storage_rebuild_required is False, "the flag must not leak into the next run"
    assert "lease-released" in calls


def test_an_uncommitted_transition_does_not_force_a_rebuild() -> None:
    """Nothing was closed, so nothing needs reconstructing."""

    calls: list[str] = []
    context = _quiesce_context(calls)
    rebuilt: list[str] = []
    context.reload_storage_from_settings = lambda: rebuilt.append("rebuilt")  # type: ignore[method-assign]

    context.finish_storage_transition(
        StorageTransition(
            "storage-select",
            _RecordingLease(calls),  # type: ignore[arg-type]
            reload_required=False,
        )
    )

    assert rebuilt == []
