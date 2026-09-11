"""Placeholder state views for the bookshelf area."""

from __future__ import annotations

from joyread.ui.widgets.localized_text import LocalizedLabel, set_localized

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class StateView(QWidget):
    def __init__(self, title: str, body: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "StateView")

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(8)

        self._title_label = LocalizedLabel(title)
        self._title_label.setProperty("class", "StateTitle")
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._body_label = LocalizedLabel(body)
        self._body_label.setProperty("class", "StateBody")
        self._body_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._body_label.setWordWrap(True)

        layout.addWidget(self._title_label)
        layout.addWidget(self._body_label)

    def set_text(self, title: str, body: str) -> None:
        set_localized(self._title_label, "setText", title)
        set_localized(self._body_label, "setText", body)
