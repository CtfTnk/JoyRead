"""Independent archive-backed manga reader window."""

from __future__ import annotations

from joyread.ui.widgets.localized_text import set_localized

from pathlib import Path
from typing import Protocol

from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import QMainWindow, QWidget

from joyread.app.reader_library_port import ReaderLibraryPort
from joyread.app.reader_runtime import ReaderRuntime
from joyread.core.models.book import Book
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.views.reader_shell import ReaderShellWidget
from joyread.ui.widgets.window_gestures import install_system_resize_border


class ReaderWindowContext(Protocol):
    reader_runtime: ReaderRuntime | None
    library_service: ReaderLibraryPort


class ReaderWindow(QMainWindow):
    """Frameless top-level host for the reusable reader shell."""

    progress_changed = QtSignal(str, int, float)
    closed = QtSignal()

    def __init__(
        self,
        context: ReaderRuntime | ReaderWindowContext,
        source_path: str | Path,
        *,
        book: Book | None = None,
        title: str | None = None,
        start_page_index: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ReaderWindow")
        set_localized(self, "setWindowTitle", title or (book.title if book is not None else Path(source_path).stem))
        # Inherited from QApplication::windowIcon(); see MainWindow for why
        # re-reading the icon file per window was worth removing.
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(Theme.reader_width, Theme.reader_height)
        self.setMinimumSize(Theme.reader_min_width, Theme.reader_min_height)

        reader = context if isinstance(context, ReaderRuntime) else context.reader_runtime
        if reader is None:
            raise RuntimeError("ReaderWindow requires a ReaderRuntime")
        library_port = (
            context.library_service
            if book is not None and not isinstance(context, ReaderRuntime)
            else None
        )
        self.shell = ReaderShellWidget(
            reader,
            source_path,
            library_port=library_port,
            book=book,
            title=title,
            show_back_button=False,
            start_page_index=start_page_index,
        )
        self.shell.progress_changed.connect(self.progress_changed.emit)
        self.setCentralWidget(self.shell)
        # Frameless windows get no resize border from the platform, and the
        # reader's drag handle auto-hides -- so the edge has to live on the
        # window itself rather than on any widget that can disappear.
        self._resize_border = install_system_resize_border(self)

        # Compatibility aliases: the embedded reader (in MainWindow) and
        # tests both reach into these widgets by attribute name. Exposing
        # them on the window lets both code paths share the same access
        # surface regardless of whether the reader is embedded or standalone,
        # so existing tests do not need to know about the inner shell.
        self.canvas = self.shell.canvas
        self.header = self.shell.header
        self.footer = self.shell.footer
        self.left_arrow = self.shell.left_arrow
        self.right_arrow = self.shell.right_arrow
        self.settings_panel = self.shell.settings_panel
        self.topic_panel = self.shell.topic_panel
        self.dialog_overlay = self.shell.dialog_overlay
        self.panel_scrim = self.shell.panel_scrim
        self.viewmodel = self.shell.viewmodel

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self.shell.handle_key_press(event):
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shell.cancel()
        self.closed.emit()
        super().closeEvent(event)
