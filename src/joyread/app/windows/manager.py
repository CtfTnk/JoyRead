"""Application-scope ownership and reuse for JoyRead top-level windows."""

from __future__ import annotations

from collections.abc import Iterable
import logging
from pathlib import Path
from typing import Callable
from uuid import uuid4

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QMainWindow

from joyread.app.app_context import AppContext
from joyread.app.launch.intent import canonical_path_key, normalize_launch_paths
from joyread.app.windows.activation import (
    ReopenDecision,
    WindowActivationRegistry,
    decide_reopen,
)
from joyread.app.storage_transition_driver import StorageTransitionController
from joyread.app.windows.ownership import WindowOwnership, ordered_close_sequence
from joyread.app.windows.novel_provider import NovelReaderProvider
from joyread.app.windows.requests import StandaloneReaderLauncher, StandaloneReaderRequest
from joyread.ui.views.main_window import MainWindow
from joyread.ui.views.reader_window import ReaderWindow


logger = logging.getLogger(__name__)

MainWindowFactory = Callable[[StandaloneReaderLauncher], QMainWindow]
ReaderWindowFactory = Callable[[StandaloneReaderRequest], QMainWindow]


class ApplicationWindowManager(QObject):
    """Own Main and standalone Readers with the lifetimes the user expects.

    Readers the Library launches are owned by Main and close with it. Readers
    the operating system asked for are roots and outlive Main. See
    :mod:`joyread.app.windows.ownership` for why the distinction matters.
    """

    def __init__(
        self,
        context: AppContext,
        *,
        main_window_factory: MainWindowFactory | None = None,
        reader_window_factory: ReaderWindowFactory | None = None,
        novel_reader_provider: NovelReaderProvider | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._context = context
        self._novel_reader_provider = novel_reader_provider
        self._main_window_factory = main_window_factory or self._create_main_window
        self._reader_window_factory = reader_window_factory or self._create_reader_window
        self._main_window: QMainWindow | None = None
        self._reader_windows: dict[str, QMainWindow] = {}
        self._reader_keys: dict[int, str] = {}
        self._window_log_ids: dict[int, str] = {}
        self._live_windows: dict[int, QMainWindow] = {}
        self._ownership = WindowOwnership()
        self._activation = WindowActivationRegistry()
        self._storage_transition: StorageTransitionController | None = None

    @property
    def storage_transition_controller(self) -> StorageTransitionController:
        """The transition driver, which needs this manager to reach Readers.

        Built lazily so a manager constructed with a stub context in a focused
        test does not pay for one it never uses.
        """

        if self._storage_transition is None:
            self._storage_transition = StorageTransitionController(self._context, self, parent=self)
        return self._storage_transition

    @property
    def main_window(self) -> QMainWindow | None:
        return self._main_window

    @property
    def reader_windows(self) -> tuple[QMainWindow, ...]:
        return tuple(self._reader_windows.values())

    @property
    def has_windows(self) -> bool:
        return bool(self._live_windows)

    def close_all_readers(self) -> int:
        """Close every Reader window, leaving the Library open.

        A storage transition replaces the services a Reader session is bound
        to, and there is no way to re-point a live session at a rebuilt stack,
        so the sessions go rather than being left dangling. The Library stays:
        it is where the transition was requested from and where its result is
        reported.

        Iterates over a snapshot because closing a window unregisters it.
        """

        readers = self.reader_windows
        for window in readers:
            window.close()
        logger.info("Closed %d reader window(s) for a storage transition", len(readers))
        return len(readers)

    def show_library(self) -> QMainWindow:
        window = self._main_window
        if window is not None:
            activate_window(window)
            logger.info(
                "Existing Library window focused",
                extra={
                    "event": "window.focused",
                    "category": "window",
                    "status": "finished",
                    "window_id": self._window_log_ids.get(id(window)),
                    "window_kind": "library",
                },
            )
            return window

        window = self._main_window_factory(self.open_reader_from_library)
        self._main_window = window
        self._register_window(window, owner=None)
        present_new_window(window)
        logger.info(
            "Library window created",
            extra={
                "event": "window.created",
                "category": "window",
                "status": "finished",
                "window_id": self._window_log_ids.get(id(window)),
                "window_kind": "library",
            },
        )
        return window

    def open_files(self, paths: Iterable[str | Path]) -> tuple[QMainWindow, ...]:
        """Open documents requested by the operating system or local IPC.

        These Readers are roots. A document already open as a Library-owned
        Reader is promoted, because an OS request means the user expects that
        window to survive closing the Library.
        """

        windows: list[QMainWindow] = []
        for path in normalize_launch_paths(paths):
            window = self.open_reader(StandaloneReaderRequest(path=path, title=path.stem))
            if window is not None:
                windows.append(window)
        return tuple(windows)

    def open_reader_from_library(
        self, request: StandaloneReaderRequest
    ) -> QMainWindow | None:
        """Open a Reader on the Library's behalf, owned by the Library."""

        return self.open_reader(request, owner=self._main_window)

    def open_reader(
        self,
        request: StandaloneReaderRequest,
        *,
        owner: QMainWindow | None = None,
    ) -> QMainWindow | None:
        paths = normalize_launch_paths((request.path,))
        if not paths:
            logger.warning("Ignoring unsupported standalone reader path=%s", request.path)
            return None
        source = paths[0]
        key = canonical_path_key(source)
        existing = self._reader_windows.get(key)
        if existing is not None:
            self._reconcile_ownership(existing, owner=owner)
            self._seek_existing_reader(existing, request.start_page_index)
            activate_window(existing)
            logger.info(
                "Existing Reader window focused",
                extra={
                    "event": "window.focused",
                    "category": "window",
                    "status": "finished",
                    "window_id": self._window_log_ids.get(id(existing)),
                    "window_kind": "reader",
                    "document_id": key,
                },
            )
            return existing

        normalized_request = StandaloneReaderRequest(
            path=source,
            book=request.book,
            title=request.title,
            start_page_index=request.start_page_index,
        )
        window = self._reader_window_factory(normalized_request)
        self._reader_windows[key] = window
        self._reader_keys[id(window)] = key
        self._register_window(window, owner=owner)

        progress_changed = getattr(window, "progress_changed", None)
        if progress_changed is not None and hasattr(progress_changed, "connect"):
            progress_changed.connect(self._handle_reader_progress_changed)
        present_new_window(window)
        logger.info(
            "Reader window created",
            extra={
                "event": "window.created",
                "category": "window",
                "status": "finished",
                "window_id": self._window_log_ids.get(id(window)),
                "window_kind": "reader",
                "document_id": key,
                "outcome": "library_owned" if owner is not None else "root",
                "count": len(self._reader_windows),
            },
        )
        return window

    def handle_reopen(self) -> QMainWindow | None:
        """Answer a dock/reopen request: raise the last window, else build Main."""

        if decide_reopen(self._activation) == ReopenDecision.SHOW_LIBRARY:
            logger.info("Reopen with no live window; showing the Library")
            return self.show_library()
        window = self._most_recent_window()
        if window is None:
            return self.show_library()
        logger.info("Reopen focusing the most recently active window")
        activate_window(window)
        return window

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        # Activation order drives the reopen decision, so it has to follow the
        # user's real focus changes and not just the windows we created.
        if event.type() == QEvent.Type.WindowActivate and id(watched) in self._live_windows:
            self._activation.record_activation(id(watched))
        return super().eventFilter(watched, event)

    def _register_window(self, window: QMainWindow, *, owner: QMainWindow | None) -> None:
        window_id = id(window)
        self._live_windows[window_id] = window
        self._window_log_ids[window_id] = uuid4().hex
        if owner is None:
            self._ownership.add_root(window_id)
        else:
            self._ownership.add_child(window_id, owner=id(owner))
        self._activation.record_activation(window_id)
        window.installEventFilter(self)

        closed = getattr(window, "closed", None)
        if closed is not None and hasattr(closed, "connect"):
            closed.connect(lambda window_id=window_id: self._handle_window_closed(window_id))
        window.destroyed.connect(
            lambda _object=None, window_id=window_id: self._release_window(window_id)
        )

    def _reconcile_ownership(
        self, window: QMainWindow, *, owner: QMainWindow | None
    ) -> None:
        window_id = id(window)
        if owner is None:
            # Promotion is one-way; see WindowOwnership for the rationale.
            self._ownership.promote_to_root(window_id)
        else:
            self._ownership.add_child(window_id, owner=id(owner))

    def _handle_window_closed(self, window_id: int) -> None:
        """Close the windows this one owned, then forget it."""

        for owned_id in ordered_close_sequence(
            self._ownership, window_id, known=self._live_windows
        ):
            owned = self._live_windows.get(owned_id)
            if owned is not None:
                owned.close()
        self._release_window(window_id)

    def _release_window(self, window_id: int) -> None:
        window = self._live_windows.pop(window_id, None)
        log_id = self._window_log_ids.pop(window_id, None)
        self._ownership.remove(window_id)
        self._activation.forget(window_id)

        key = self._reader_keys.pop(window_id, None)
        if key is not None:
            current = self._reader_windows.get(key)
            if current is not None and id(current) == window_id:
                self._reader_windows.pop(key, None)
                logger.info(
                    "Reader window released",
                    extra={
                        "event": "window.closed",
                        "category": "window",
                        "status": "finished",
                        "window_id": log_id,
                        "window_kind": "reader",
                        "document_id": key,
                        "count": len(self._reader_windows),
                    },
                )
        if self._main_window is not None and id(self._main_window) == window_id:
            self._main_window = None
            logger.info(
                "Library window released",
                extra={
                    "event": "window.closed",
                    "category": "window",
                    "status": "finished",
                    "window_id": log_id,
                    "window_kind": "library",
                },
            )
        if window is not None:
            window.removeEventFilter(self)

    def _most_recent_window(self) -> QMainWindow | None:
        for window_id in self._activation.ordered():
            window = self._live_windows.get(window_id)
            if window is not None:
                return window
        return None

    def _create_main_window(self, launcher: StandaloneReaderLauncher) -> QMainWindow:
        return MainWindow(
            self._context,
            standalone_reader_launcher=launcher,
            storage_transition_controller=self.storage_transition_controller,
            novel_reader_provider=self._novel_reader_provider,
        )

    def _create_reader_window(self, request: StandaloneReaderRequest) -> QMainWindow:
        provider = self._novel_reader_provider
        if provider is not None and request.path.suffix.lower() in provider.extensions:
            return provider.create_window(self._context, request)
        return ReaderWindow(
            self._context,
            request.path,
            book=request.book,
            title=request.title,
            start_page_index=request.start_page_index,
        )

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
