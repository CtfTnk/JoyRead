"""Choose the first top-level window without eagerly constructing the shelf."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
import weakref

from PySide6.QtCore import QObject, QTimer, Signal as QtSignal
from PySide6.QtWidgets import QMainWindow

from joyread.app.launch_intent import LaunchAction, LaunchIntent


ShowLibrary = Callable[[], QMainWindow]
OpenFiles = Callable[[Iterable[Path]], tuple[QMainWindow, ...]]


class StartupWindowCoordinator(QObject):
    """Let a startup file/IPC intent win before falling back to the shelf.

    This object only owns the one-time startup race. Ongoing top-level window
    ownership and reuse belong to ``ApplicationWindowManager``.
    """

    settled = QtSignal()

    def __init__(
        self,
        *,
        show_library: ShowLibrary,
        open_files: OpenFiles,
        file_open_grace_ms: int,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._show_library_callback = show_library
        self._open_files_callback = open_files
        self._file_open_grace_ms = max(0, file_open_grace_ms)
        self._startup_settled = False
        self._started = False
        self._initial_window_ref: weakref.ReferenceType[QMainWindow] | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._show_library)

    @property
    def initial_window(self) -> QMainWindow | None:
        return self._initial_window_ref() if self._initial_window_ref is not None else None

    @property
    def startup_settled(self) -> bool:
        return self._startup_settled

    def start(self, initial_intent: LaunchIntent | None = None) -> QMainWindow | None:
        if self._started:
            raise RuntimeError("StartupWindowCoordinator.start() may only be called once.")
        self._started = True
        if self._startup_settled:
            return self.initial_window
        if initial_intent is not None:
            windows = self.handle_intent(initial_intent)
            return windows[0] if windows else None
        if self._file_open_grace_ms == 0:
            return self._show_library()
        self._timer.start(self._file_open_grace_ms)
        return None

    def handle_intent(self, intent: LaunchIntent) -> tuple[QMainWindow, ...]:
        is_startup_request = not self._startup_settled
        if is_startup_request:
            self._timer.stop()
            self._startup_settled = True

        if intent.action == LaunchAction.SHOW_LIBRARY:
            windows = (self._show_library_callback(),)
        else:
            windows = self._open_files_callback(intent.paths)
            if is_startup_request and not windows:
                windows = (self._show_library_callback(),)

        if is_startup_request:
            self._initial_window_ref = weakref.ref(windows[0])
            self.settled.emit()
        return windows

    def open_file(self, path: Path) -> None:
        windows = self.handle_intent(LaunchIntent.open_files((path,)))
        if not windows:
            raise RuntimeError(f"Supported file request did not create a reader: {path}")

    def _show_library(self) -> QMainWindow:
        if self._startup_settled:
            window = self.initial_window
            if window is None:
                raise RuntimeError("Startup settled without a live initial window.")
            return window
        windows = self.handle_intent(LaunchIntent.show_library())
        return windows[0]
