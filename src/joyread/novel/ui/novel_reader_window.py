"""Independent top-level window hosting the novel reader skeleton."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import QMainWindow, QWidget

from joyread.app.app_context import AppContext
from joyread.core.models.book import Book
from joyread.ui.resources.styles.theme import Theme
from joyread.novel.ui.novel_reader_shell import NovelReaderShellWidget
from joyread.ui.widgets.window_gestures import install_system_resize_border


class NovelReaderWindow(QMainWindow):
    """Frameless top-level host for the novel reader shell."""

    progress_changed = QtSignal(str, int, float)
    closed = QtSignal()

    def __init__(
        self,
        context: AppContext,
        source_path: str | Path,
        *,
        book: Book | None = None,
        title: str | None = None,
        start_page_index: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("NovelReaderWindow")
        self.setWindowTitle(title or (book.title if book is not None else Path(source_path).stem))
        # Inherited from QApplication::windowIcon(); see MainWindow for why
        # re-reading the icon file per window was worth removing.
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(Theme.reader_width, Theme.reader_height)
        self.setMinimumSize(Theme.reader_min_width, Theme.reader_min_height)

        self.shell = NovelReaderShellWidget(
            context,
            source_path,
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

        # Mirror the manga ReaderWindow aliases so tests and embedded
        # callers can talk to the shell's pieces directly regardless of
        # which reader they opened.
        self.content_area = self.shell.content_area
        self.canvas = self.shell.content_area
        self.header = self.shell.header
        self.footer = self.shell.footer
        self.left_arrow = self.shell.left_arrow
        self.right_arrow = self.shell.right_arrow
        self.custom_panel = self.shell.custom_panel
        self.topic_panel = self.shell.topic_panel
        self.dialog_overlay = self.shell.dialog_overlay
        self.panel_scrim = self.shell.panel_scrim

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self.shell.handle_key_press(event):
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shell.cancel()
        self.closed.emit()
        super().closeEvent(event)
