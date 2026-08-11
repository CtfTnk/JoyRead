from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMainWindow

from joyread.app.app_context import AppContext
from joyread.app.windows.manager import ApplicationWindowManager
from joyread.app.windows.requests import StandaloneReaderLauncher, StandaloneReaderRequest


class _ShelfViewModel:
    def __init__(self) -> None:
        self.progress: list[tuple[str, int, float]] = []

    def apply_reader_progress(self, book_uuid: str, page_index: int, percent: float) -> None:
        self.progress.append((book_uuid, page_index, percent))


class _FakeMainWindow(QMainWindow):
    closed = QtSignal()

    def __init__(self, launcher: StandaloneReaderLauncher) -> None:
        super().__init__()
        self.launcher = launcher
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.closed.emit()
        super().closeEvent(event)


class _FakeReaderWindow(QMainWindow):
    closed = QtSignal()
    progress_changed = QtSignal(str, int, float)

    def __init__(self, request: StandaloneReaderRequest) -> None:
        super().__init__()
        self.request = request
        self.seek_calls: list[int] = []
        self.viewmodel = SimpleNamespace(seek=self.seek_calls.append)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.closed.emit()
        super().closeEvent(event)


def _manager() -> tuple[ApplicationWindowManager, list[_FakeMainWindow], list[_FakeReaderWindow], _ShelfViewModel]:
    shelf = _ShelfViewModel()
    context = cast(AppContext, SimpleNamespace(shelf_viewmodel=shelf))
    mains: list[_FakeMainWindow] = []
    readers: list[_FakeReaderWindow] = []

    def create_main(launcher: StandaloneReaderLauncher) -> _FakeMainWindow:
        window = _FakeMainWindow(launcher)
        mains.append(window)
        return window

    def create_reader(request: StandaloneReaderRequest) -> _FakeReaderWindow:
        window = _FakeReaderWindow(request)
        readers.append(window)
        return window

    manager = ApplicationWindowManager(
        context,
        main_window_factory=create_main,
        reader_window_factory=create_reader,
    )
    return manager, mains, readers, shelf


def _close_windows(manager: ApplicationWindowManager, qtbot) -> None:
    main = manager.main_window
    if main is not None:
        main.close()
    for reader in manager.reader_windows:
        reader.close()
    qtbot.wait(0)


def test_show_library_focuses_existing_then_rebuilds_after_close(qtbot) -> None:
    manager, mains, _readers, _shelf = _manager()
    try:
        first = manager.show_library()
        first.showMinimized()

        assert manager.show_library() is first
        assert not first.isMinimized()
        assert len(mains) == 1

        first.close()
        qtbot.waitUntil(lambda: manager.main_window is None)
        second = manager.show_library()
        assert second is not first
        assert len(mains) == 2
    finally:
        _close_windows(manager, qtbot)


def test_open_files_reuses_same_path_and_keeps_distinct_readers(qtbot, tmp_path: Path) -> None:
    manager, _mains, readers, _shelf = _manager()
    first_path = tmp_path / "first.cbz"
    second_path = tmp_path / "second.pdf"
    try:
        first = manager.open_files((first_path,))[0]
        duplicate = manager.open_reader(
            StandaloneReaderRequest(first_path, start_page_index=7)
        )
        second = manager.open_files((second_path,))[0]

        assert duplicate is first
        assert cast(_FakeReaderWindow, first).seek_calls == [7]
        assert second is not first
        assert len(readers) == 2
        assert len(manager.reader_windows) == 2
    finally:
        _close_windows(manager, qtbot)


def test_closing_main_closes_the_readers_it_launched(qtbot, tmp_path: Path) -> None:
    """Readers the Library opened are part of the Library session."""

    manager, mains, _readers, _shelf = _manager()
    try:
        main = cast(_FakeMainWindow, manager.show_library())
        reader = main.launcher(StandaloneReaderRequest(tmp_path / "book.cbz"))
        assert reader in manager.reader_windows

        main.close()
        qtbot.waitUntil(lambda: manager.main_window is None)
        assert manager.reader_windows == ()

        manager.show_library()
        assert len(mains) == 2
    finally:
        _close_windows(manager, qtbot)


def test_closing_main_keeps_readers_the_operating_system_requested(
    qtbot, tmp_path: Path
) -> None:
    """"Open With" Readers are roots and must outlive the Library."""

    manager, _mains, _readers, _shelf = _manager()
    try:
        manager.show_library()
        reader = manager.open_files((tmp_path / "external.cbz",))[0]

        cast(_FakeMainWindow, manager.main_window).close()
        qtbot.waitUntil(lambda: manager.main_window is None)

        assert reader in manager.reader_windows
        assert reader.isVisible()
    finally:
        _close_windows(manager, qtbot)


def test_operating_system_request_promotes_a_library_owned_reader(
    qtbot, tmp_path: Path
) -> None:
    """Reopening an owned Reader through the OS detaches it from the Library."""

    manager, _mains, readers, _shelf = _manager()
    source = tmp_path / "book.cbz"
    try:
        main = cast(_FakeMainWindow, manager.show_library())
        owned = main.launcher(StandaloneReaderRequest(source))

        promoted = manager.open_files((source,))[0]
        assert promoted is owned
        assert len(readers) == 1

        main.close()
        qtbot.waitUntil(lambda: manager.main_window is None)

        assert owned in manager.reader_windows
        assert owned.isVisible()
    finally:
        _close_windows(manager, qtbot)


def test_reopen_focuses_most_recent_window_then_rebuilds_library(
    qtbot, tmp_path: Path
) -> None:
    manager, mains, _readers, _shelf = _manager()
    try:
        manager.show_library()
        reader = manager.open_files((tmp_path / "book.cbz",))[0]

        assert manager.handle_reopen() is reader
        assert len(mains) == 1

        reader.close()
        cast(_FakeMainWindow, manager.main_window).close()
        qtbot.waitUntil(lambda: not manager.has_windows)

        rebuilt = manager.handle_reopen()
        assert rebuilt is manager.main_window
        assert len(mains) == 2
    finally:
        _close_windows(manager, qtbot)


def test_reader_progress_is_forwarded_without_main_window(qtbot, tmp_path: Path) -> None:
    manager, _mains, _readers, shelf = _manager()
    try:
        reader = cast(_FakeReaderWindow, manager.open_files((tmp_path / "book.cbz",))[0])
        reader.progress_changed.emit("book-uuid", 9, 42.5)

        assert shelf.progress == [("book-uuid", 9, 42.5)]
    finally:
        _close_windows(manager, qtbot)
