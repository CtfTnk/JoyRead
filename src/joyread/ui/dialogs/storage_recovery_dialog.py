"""Startup dialog shown when the configured JoyRead library cannot be opened."""

from __future__ import annotations

from enum import IntEnum

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


STORAGE_RECOVERY_DIALOG_MIN_WIDTH = 500


class StorageRecoveryDialogResult(IntEnum):
    INITIALIZE = 2
    SELECT = 3


class StorageRecoveryDialog(QDialog):
    def __init__(self, current: str, message: str, parent: QDialog | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("JoyRead Library Unavailable")
        self.setMinimumWidth(STORAGE_RECOVERY_DIALOG_MIN_WIDTH)

        title = QLabel("JoyRead can't open your library.")
        title.setObjectName("StorageRecoveryTitle")

        body = QLabel(
            f"Location:\n{current}\n\n{message}\n\n"
            "Initialize creates a new default JoyRead-Library. "
            "Select switches to an existing JoyRead library."
        )
        body.setWordWrap(True)
        body.setTextInteractionFlags(
            body.textInteractionFlags() | Qt.TextInteractionFlag.TextSelectableByMouse
        )

        initialize = QPushButton("Initialize")
        select = QPushButton("Select")
        select.setDefault(True)

        initialize.clicked.connect(
            lambda: self.done(int(StorageRecoveryDialogResult.INITIALIZE))
        )
        select.clicked.connect(lambda: self.done(int(StorageRecoveryDialogResult.SELECT)))

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(initialize)
        button_row.addWidget(select)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(body)
        layout.addLayout(button_row)
