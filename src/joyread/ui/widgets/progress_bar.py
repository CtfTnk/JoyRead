"""Reader-aligned progress pill used by book cards, rows and details."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPaintEvent, QResizeEvent
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
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(self._progress_width, Theme.book_progress_height)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_indicator_width()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        track = QPainterPath()
        track.addRoundedRect(QRectF(self.rect()), Theme.book_progress_radius, Theme.book_progress_radius)
        if self._progress_percent > 0:
            read = QPainterPath()
            read.addRoundedRect(
                QRectF(self._indicator.geometry()), Theme.book_progress_radius, Theme.book_progress_radius,
            )
            # The child paints the read pill with the Reader's quiet material.
            # Exclude that shape so its alpha blends with the actual backdrop,
            # rather than with an unread layer already painted underneath it.
            track = track.subtracted(read)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillPath(track, QColor(*Theme.reader_progress_palettes["quiet"].unread))
        painter.end()

    def _refresh_indicator_width(self) -> None:
        if self._progress_percent <= 0:
            self._indicator.hide()
            self._indicator.setFixedWidth(0)
            return

        raw_width = round(self.width() * (self._progress_percent / 100))
        visible_width = min(self.width(), max(Theme.book_progress_height, raw_width))
        self._indicator.setFixedWidth(visible_width)
        self._indicator.show()
