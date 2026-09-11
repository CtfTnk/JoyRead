"""Startup dialog shown when the configured JoyRead library cannot be opened."""

from __future__ import annotations

from joyread.ui.widgets.localized_text import LocalizedLabel, LocalizedPushButton, set_localized

from enum import IntEnum
from joyread.infrastructure.i18n.locale_service import t

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


STORAGE_RECOVERY_DIALOG_MIN_WIDTH = 500


class StorageRecoveryDialogResult(IntEnum):
    INITIALIZE = 2
    SELECT = 3


class StorageRecoveryDialog(QDialog):
    def __init__(self, current: str, message: str, parent: QDialog | None = None) -> None:
        super().__init__(parent)
        set_localized(self, "setWindowTitle", t("startup.library_unavailable"))
        self.setMinimumWidth(STORAGE_RECOVERY_DIALOG_MIN_WIDTH)

        title = LocalizedLabel(t("startup.cannot_open_library"))
        title.setObjectName("StorageRecoveryTitle")

        body = LocalizedLabel(t("startup.recovery_body", path=current, detail=message))
        body.setWordWrap(True)
        body.setTextInteractionFlags(
            body.textInteractionFlags() | Qt.TextInteractionFlag.TextSelectableByMouse
        )

        initialize = LocalizedPushButton(t("startup.initialize"))
        select = LocalizedPushButton(t("startup.select"))
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
