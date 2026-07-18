"""Application-scope ownership and reuse for JoyRead top-level windows."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import logging
from pathlib import Path

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QApplication, QMainWindow

from joyread.app.app_context import AppContext
from joyread.app.launch_intent import canonical_path_key, normalize_launch_paths
from joyread.core.file_types import EPUB_ACCESS_ENABLED, EPUB_EXTENSIONS
from joyread.app.window_requests import StandaloneReaderLauncher, StandaloneReaderRequest
from joyread.ui.views.main_window import MainWindow
from joyread.ui.views.novel_reader_window import NovelReaderWindow
from joyread.ui.views.reader_window import ReaderWindow


logger = logging.getLogger(__name__)

MainWindowFactory = Callable[[StandaloneReaderLauncher], QMainWindow]
ReaderWindowFactory = Callable[[StandaloneReaderRequest], QMainWindow]


class ApplicationWindowManager(QObject):
    """Own Main and standalone readers independently for one app process."""

    def __init__(
        self,
        context: AppContext,
        *,
        main_window_factory: MainWindowFactory | None = None,
        reader_window_factory: ReaderWindowFactory | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._context = context
        self._main_window_factory = main_window_factory or self._create_main_window
        self._reader_window_factory = reader_window_factory or self._create_reader_window
        self._main_window: QMainWindow | None = None
        self._reader_windows: dict[str, QMainWindow] = {}

    @property
    def main_window(self) -> QMainWindow | None:
        return self._main_window

    @property
    def reader_windows(self) -> tuple[QMainWindow, ...]:
        return tuple(self._reader_windows.values())

    def show_library(self) -> QMainWindow:
        window = self._main_window
        if window is not None:
            activate_window(window)
            return window

        window = self._main_window_factory(self.open_reader)
        self._main_window = window
        window_id = id(window)
        closed = getattr(window, "closed", None)
        if closed is not None and hasattr(closed, "connect"):
            closed.connect(lambda window_id=window_id: self._forget_main_window(window_id))
        window.destroyed.connect(
            lambda _object=None, window_id=window_id: self._forget_main_window(window_id)
        )
        present_new_window(window)
        logger.info("Library window shown")
        return window

    def open_files(self, paths: Iterable[str | Path]) -> tuple[QMainWindow, ...]:
        windows: list[QMainWindow] = []
        for path in normalize_launch_paths(paths):
            window = self.open_reader(
                StandaloneReaderRequest(path=path, title=path.stem)
            )
            if window is not None:
                windows.append(window)
        return tuple(windows)

    def open_reader(self, request: StandaloneReaderRequest) -> QMainWindow | None:
        paths = normalize_launch_paths((request.path,))
        if not paths:
            logger.warning("Ignoring unsupported standalone reader path=%s", request.path)
            return None
        source = paths[0]
        key = canonical_path_key(source)
        existing = self._reader_windows.get(key)
        if existing is not None:
            self._seek_existing_reader(existing, request.start_page_index)
            activate_window(existing)
            logger.info("Focused existing reader path=%s", source)
            return existing

        normalized_request = StandaloneReaderRequest(
            path=source,
            book=request.book,
            title=request.title,
            start_page_index=request.start_page_index,
        )
        window = self._reader_window_factory(normalized_request)
        self._reader_windows[key] = window
        window_id = id(window)
        closed = getattr(window, "closed", None)
        if closed is not None and hasattr(closed, "connect"):
            closed.connect(
                lambda key=key, window_id=window_id: self._forget_reader_window(key, window_id)
            )
        window.destroyed.connect(
            lambda _object=None, key=key, window_id=window_id: self._forget_reader_window(
                key, window_id
            )
        )
        progress_changed = getattr(window, "progress_changed", None)
        if progress_changed is not None and hasattr(progress_changed, "connect"):
            progress_changed.connect(self._handle_reader_progress_changed)
        present_new_window(window)
        logger.info("Standalone reader shown path=%s active=%d", source, len(self._reader_windows))
        return window

    def _create_main_window(self, launcher: StandaloneReaderLauncher) -> QMainWindow:
        return MainWindow(self._context, standalone_reader_launcher=launcher)

    def _create_reader_window(self, request: StandaloneReaderRequest) -> QMainWindow:
        if EPUB_ACCESS_ENABLED and request.path.suffix.lower() in EPUB_EXTENSIONS:
            return NovelReaderWindow(
                self._context,
                request.path,
                book=request.book,
                title=request.title,
                start_page_index=request.start_page_index,
            )
        return ReaderWindow(
            self._context,
            request.path,
            book=request.book,
            title=request.title,
            start_page_index=request.start_page_index,
        )

    def _forget_main_window(self, window_id: int) -> None:
        if self._main_window is not None and id(self._main_window) == window_id:
            self._main_window = None
            logger.debug("Library window released")

    def _forget_reader_window(self, key: str, window_id: int) -> None:
        current = self._reader_windows.get(key)
        if current is not None and id(current) == window_id:
            self._reader_windows.pop(key, None)
            logger.debug("Reader window released active=%d", len(self._reader_windows))

    def _handle_reader_progress_changed(
        self,
        book_uuid: str,
        page_index: int,
        progress_percent: float,
    ) -> None:
        self._context.shelf_viewmodel.apply_reader_progress(
            book_uuid,
            page_index,
            progress_percent,
        )

    @staticmethod
    def _seek_existing_reader(window: QMainWindow, page_index: int | None) -> None:
        if page_index is None:
            return
        viewmodel = getattr(window, "viewmodel", None)
        if viewmodel is None:
            shell = getattr(window, "shell", None)
            viewmodel = getattr(shell, "viewmodel", None)
        seek = getattr(viewmodel, "seek", None)
        if callable(seek):
            seek(page_index)


def present_new_window(window: QMainWindow) -> None:
    center_window_on_launch(window)
    activate_window(window)


def activate_window(window: QMainWindow) -> None:
    if window.isMinimized():
        window.setWindowState(
            window.windowState()
            & ~Qt.WindowState.WindowMinimized
            | Qt.WindowState.WindowActive
        )
        window.show()
    elif not window.isVisible():
        window.show()
    window.raise_()
    window.activateWindow()


def center_window_on_launch(window: QMainWindow) -> None:
    screen = window.screen() or QApplication.primaryScreen()
    if screen is None:
        return
    window_geometry = window.frameGeometry()
    if window_geometry.isNull() or window_geometry.width() <= 0 or window_geometry.height() <= 0:
        window_geometry = window.geometry()
    if window_geometry.isNull() or window_geometry.width() <= 0 or window_geometry.height() <= 0:
        return
    window_geometry.moveCenter(screen.availableGeometry().center())
    window.move(window_geometry.topLeft())
