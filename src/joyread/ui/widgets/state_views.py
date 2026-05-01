"""Placeholder state views for the bookshelf area."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class StateView(QWidget):
    def __init__(self, title: str, body: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "StateView")

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(8)

        title_label = QLabel(title)
        title_label.setProperty("class", "StateTitle")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        body_label = QLabel(body)
        body_label.setProperty("class", "StateBody")
        body_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body_label.setWordWrap(True)

        layout.addWidget(title_label)
        layout.addWidget(body_label)
