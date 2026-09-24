"""Disposable P0 content-paint probes; remove after P5 acceptance.

The production widgets only call or install this module. Keeping the probe
logic here makes the measurement hooks easy to remove without unwinding normal
Reader or Shelf rendering behavior.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, QObject, QRectF, QTimer
from PySide6.QtWidgets import QApplication, QWidget

from joyread.app import startup_trace


logger = logging.getLogger(__name__)


def record_reader_canvas_paint(canvas: QWidget) -> None:
    """Mark the first visible prepared page after its canvas paint completes."""

    if getattr(canvas, "_p0_content_reported", False) or canvas.is_page_slide_active:
        return
    layout = canvas._layout_result
    if layout is None:
        return
    viewport = QRectF(canvas.rect())
    for draw in layout.page_draws:
        drawn = QRectF(
            draw.rect.x + canvas._effective_pan_x(),
            draw.rect.y,
            draw.rect.width,
            draw.rect.height,
        )
        if not drawn.intersects(viewport):
            continue
        pixmap = canvas._pixmaps.get(draw.page_index)
        if pixmap is not None and not pixmap.isNull():
            milestone = "reader_first_page_paint"
        elif draw.page_index in canvas._failed_pages:
            milestone = "reader_error_visible"
        else:
            continue
        canvas._p0_content_reported = True
        if startup_trace.mark(milestone) is not None:
            startup_trace.flush_to_log(logger)
        return


class P0ShelfContentProbe(QObject):
    """Observe book-card and empty/error paints without changing Shelf logic."""

    def __init__(self, shelf: QWidget) -> None:
        super().__init__(shelf)
        self._shelf = shelf
        self._pending = False
        self._book_done = False
        self._empty_done = False
        self._error_done = False
        self._app = QApplication.instance()
        shelf.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self._shelf and event.type() == QEvent.Type.Show:
            self._shelf.removeEventFilter(self)
            if self._app is not None:
                self._app.installEventFilter(self)
            return False
        if event.type() != QEvent.Type.Paint or self._pending:
            return False
        shelf = self._shelf
        active = shelf.stack.currentWidget()
        milestone: str | None = None
        if watched is shelf.error_state and active is shelf.error_state and not self._error_done:
            milestone = "library_error_visible"
        elif (
            watched is shelf.empty_state
            and active is shelf.empty_state
            and not shelf._viewmodel.books
            and not shelf._viewmodel.is_loading
            and not shelf._viewmodel.error_message
            and not self._empty_done
        ):
            milestone = "library_ready_empty"
        elif (
            not self._book_done
            and active in (shelf.grid, shelf.list_view)
            and isinstance(watched, QWidget)
            and shelf.isAncestorOf(watched)
            and watched.property("class") in ("BookCard", "BookListRow")
        ):
            milestone = "library_first_book_paint"
        if milestone is not None:
            self._pending = True
            QTimer.singleShot(0, lambda name=milestone: self._record_after_paint(name))
        return False

    def _record_after_paint(self, milestone: str) -> None:
        self._pending = False
        shelf = self._shelf
        active = shelf.stack.currentWidget()
        if milestone == "library_first_book_paint":
            if active not in (shelf.grid, shelf.list_view):
                return
            self._book_done = True
        elif milestone == "library_ready_empty":
            if active is not shelf.empty_state:
                return
            self._empty_done = True
        elif milestone == "library_error_visible":
            if active is not shelf.error_state:
                return
            self._error_done = True
        if startup_trace.mark(milestone) is not None:
            startup_trace.flush_to_log(logger)
        if self._app is not None:
            self._app.removeEventFilter(self)
            self._app = None
