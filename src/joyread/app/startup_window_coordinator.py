"""Choose the first top-level window without eagerly constructing the shelf."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QMainWindow


WindowFactory = Callable[[], QMainWindow]
ReaderWindowFactory = Callable[[Path], QMainWindow]
WindowPresenter = Callable[[QMainWindow], None]


class StartupWindowCoordinator(QObject):
    """Defer the shelf briefly so a macOS open-document event can win.

    Finder delivers ``QFileOpenEvent`` only after Qt's event loop starts. If
    the shelf is built synchronously first, a direct document launch pays for
    both MainWindow and ReaderWindow. The coordinator waits for that startup
    event, but switches to normal multi-window behavior once the initial
    window has been selected.
    """

    def __init__(
        self,
        *,
        create_main_window: WindowFactory,
        create_reader_window: ReaderWindowFactory,
        present_window: WindowPresenter,
        file_open_grace_ms: int,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._create_main_window = create_main_window
        self._create_reader_window = create_reader_window
        self._present_window = present_window
        self._file_open_grace_ms = max(0, file_open_grace_ms)
        self._startup_settled = False
        self._started = False
        self._initial_window: QMainWindow | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._show_main_window)

    @property
    def initial_window(self) -> QMainWindow | None:
        return self._initial_window

    @property
    def startup_settled(self) -> bool:
        return self._startup_settled

    def start(self, direct_path: Path | None = None) -> QMainWindow | None:
        if self._started:
            raise RuntimeError("StartupWindowCoordinator.start() may only be called once.")
        self._started = True

        # A queued QFileOpenEvent may have reached ``open_file`` when the
        # router handler was connected, before this method is called.
        if self._startup_settled:
            return self._initial_window
        if direct_path is not None:
            return self._show_initial_reader(direct_path)
        if self._file_open_grace_ms == 0:
            return self._show_main_window()
        self._timer.start(self._file_open_grace_ms)
        return None

    def open_file(self, path: Path) -> QMainWindow:
        is_startup_request = not self._startup_settled
        if is_startup_request:
            self._timer.stop()
            self._startup_settled = True

        window = self._create_reader_window(path)
        if is_startup_request:
            self._retain_initial_window(window)
        self._present_window(window)
        return window

    def _show_initial_reader(self, path: Path) -> QMainWindow:
        self._startup_settled = True
        window = self._create_reader_window(path)
        self._retain_initial_window(window)
        self._present_window(window)
        return window

    def _show_main_window(self) -> QMainWindow:
        if self._startup_settled:
            if self._initial_window is None:
                raise RuntimeError("Startup settled without an initial window.")
            return self._initial_window
        self._startup_settled = True
        window = self._create_main_window()
        self._retain_initial_window(window)
        self._present_window(window)
        return window

    def _retain_initial_window(self, window: QMainWindow) -> None:
        self._initial_window = window
        key = id(window)
        window.destroyed.connect(
            lambda _object=None, window_key=key: self._forget_initial_window(window_key)
        )

    def _forget_initial_window(self, window_key: int) -> None:
        if self._initial_window is not None and id(self._initial_window) == window_key:
            self._initial_window = None

