"""Figma-style progress pill used by book cards and list rows."""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QSizePolicy, QWidget

from joyread.ui.resources.styles.theme import Theme


class BookProgressBar(QFrame):
    """Progress bar with a minimum rounded indicator for non-zero progress."""

    def __init__(
        self,
        progress_percent: int,
        parent: QWidget | None = None,
        *,
        width: int = Theme.book_progress_width,
    ) -> None:
        super().__init__(parent)
        self._progress_percent = 0
        self._progress_width = width
        self.setObjectName("BookProgress")
        self.setFixedSize(self._progress_width, Theme.book_progress_height)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._indicator = QFrame()
        self._indicator.setObjectName("BookProgressIndicator")
        self._indicator.setFixedHeight(Theme.book_progress_height)
        self._indicator.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._indicator)
        layout.addStretch(1)

        self.set_progress(progress_percent)

    @property
    def progress_percent(self) -> int:
        return self._progress_percent

    def set_progress(self, progress_percent: int) -> None:
        self._progress_percent = max(0, min(100, int(progress_percent)))
        self._refresh_indicator_width()

    def sizeHint(self) -> QSize:
        return QSize(self._progress_width, Theme.book_progress_height)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_indicator_width()

    def _refresh_indicator_width(self) -> None:
        if self._progress_percent <= 0:
            self._indicator.hide()
            self._indicator.setFixedWidth(0)
            return

        raw_width = round(self.width() * (self._progress_percent / 100))
        visible_width = min(self.width(), max(Theme.book_progress_height, raw_width))
        self._indicator.setFixedWidth(visible_width)
        self._indicator.show()
