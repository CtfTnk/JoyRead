"""Reusable labels that hide overflowing text without changing layout size."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics, QResizeEvent
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget


class ElidedLabel(QLabel):
    """Single-line label that exposes full text only when it is visually clipped."""

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        elide_mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
    ) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._elide_mode = elide_mode
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.setWordWrap(False)
        self.set_full_text(text)

    @property
    def full_text(self) -> str:
        return self._full_text

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API override.
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self._refresh_elision()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_elision()

    def _refresh_elision(self) -> None:
        metrics = QFontMetrics(self.font())
        available_width = max(0, self.contentsRect().width())
        display_text = metrics.elidedText(self._full_text, self._elide_mode, available_width)
        QLabel.setText(self, display_text)
        # Only show the hint when there is hidden content. This keeps short
        # titles from producing redundant hover chrome.
        self.setToolTip(self._full_text if display_text != self._full_text else "")
