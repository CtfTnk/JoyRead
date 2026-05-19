from __future__ import annotations

import inspect
from pathlib import Path

from joyread.app import bootstrap
from joyread.app.app_context import AppContext
from joyread.app.bootstrap import create_application
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
        library_service=None,  # type: ignore[arg-type]
        task_service=_RecordingTaskService(calls),  # type: ignore[arg-type]
        cache_service=None,  # type: ignore[arg-type]
        hash_service=None,  # type: ignore[arg-type]
        tag_service=None,  # type: ignore[arg-type]
        import_service=None,  # type: ignore[arg-type]
        export_service=None,  # type: ignore[arg-type]
        storage_migration_service=None,  # type: ignore[arg-type]
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
    qtbot.addWidget(window)

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
    qtbot.addWidget(window)

    assert app.quitOnLastWindowClosed()
    assert isinstance(window, ReaderWindow)
    assert not isinstance(window, MainWindow)

    window.close()
    context.close()


def test_native_file_dialogs_are_kept_for_in_app_picker_paths() -> None:
    main_window_source = inspect.getsource(main_window_module)
    bootstrap_source = inspect.getsource(bootstrap)

    assert "QFileDialog.getOpenFileName" in main_window_source
    assert "QFileDialog.getOpenFileNames" in main_window_source
    assert "QFileDialog.getExistingDirectory" in main_window_source
    assert "DontUseNativeDialog" not in main_window_source
    assert "AA_DontUseNativeDialogs" not in bootstrap_source
