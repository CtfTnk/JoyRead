from __future__ import annotations

import inspect
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFileOpenEvent
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow, QPushButton

from joyread.app import bootstrap
from joyread.app.app_context import AppContext
from joyread.app.bootstrap import create_application
from joyread.app.file_open_router import FileOpenRouter
from joyread.app.launch_intent import LaunchIntent
from joyread.app.startup_window_coordinator import StartupWindowCoordinator
from joyread.core.file_types import SUPPORTED_READER_EXTENSIONS
from joyread.core.services.storage_recovery_service import StorageRecoveryCancelled
from joyread.ui.dialogs.storage_recovery_dialog import StorageRecoveryDialog
from joyread.ui.views import main_window as main_window_module
from joyread.ui.views.main_window import MainWindow
from joyread.ui.views.reader_window import ReaderWindow


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


def test_app_context_closes_tasks_before_database() -> None:
    calls: list[str] = []
    context = AppContext(
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
        task_service=_RecordingTaskService(calls),  # type: ignore[arg-type]
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

    context.close()

    assert calls == ["task", "database"]


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
    app = QApplication.instance() or QApplication(["test"])
    previous_router = getattr(app, "_joyread_file_open_router", None)
    if isinstance(previous_router, FileOpenRouter):
        previous_router.dispose()

    source = tmp_path / "finder-cold-start.cbz"
    source.write_bytes(b"")
    main_windows: list[QMainWindow] = []
    reader_windows: list[QMainWindow] = []
    opened_paths: list[Path] = []

    def create_main_window() -> QMainWindow:
        window = QMainWindow()
        qtbot.addWidget(window)
        main_windows.append(window)
        return window

    def create_reader_window(path: Path) -> QMainWindow:
        opened_paths.append(path)
        window = QMainWindow()
        qtbot.addWidget(window)
        reader_windows.append(window)
        return window

    router = FileOpenRouter(app, SUPPORTED_READER_EXTENSIONS)
    def open_files(paths) -> tuple[QMainWindow, ...]:  # noqa: ANN001
        return tuple(create_reader_window(path) for path in paths)

    coordinator = StartupWindowCoordinator(
        show_library=create_main_window,
        open_files=open_files,
        file_open_grace_ms=25,
    )
    coordinator.start()
    router.set_open_handler(coordinator.open_file)

    assert QApplication.sendEvent(app, QFileOpenEvent(str(source)))
    qtbot.wait(40)

    assert opened_paths == [source]
    assert main_windows == []
    assert coordinator.initial_window is reader_windows[0]
    assert coordinator.startup_settled

    reader_windows[0].close()
    router.dispose()
    coordinator.deleteLater()


def test_startup_coordinator_builds_main_after_file_open_grace(qtbot) -> None:
    main_windows: list[QMainWindow] = []

    def create_main_window() -> QMainWindow:
        window = QMainWindow()
        qtbot.addWidget(window)
        main_windows.append(window)
        return window

    coordinator = StartupWindowCoordinator(
        show_library=create_main_window,
        open_files=lambda _paths: (),
        file_open_grace_ms=10,
    )
    coordinator.start()
    qtbot.waitUntil(lambda: bool(main_windows), timeout=500)

    assert coordinator.initial_window is main_windows[0]
    assert coordinator.startup_settled

    main_windows[0].close()
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

        def start(self, intent: LaunchIntent):  # noqa: ANN201
            forwarded.append(intent)
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
