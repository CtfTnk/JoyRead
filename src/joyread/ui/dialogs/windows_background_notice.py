"""Windows close-to-background explanation shown before the last window closes."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QVBoxLayout, QWidget

from joyread.infrastructure.i18n.locale_service import t
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.localized_text import LocalizedLabel, LocalizedPushButton, set_localized


# QDialog reserves 0/1 for Rejected/Accepted; this is the explicit quit choice.
EXIT_APPLICATION = 2


class WindowsBackgroundNoticeDialog(QDialog):
    """Collect the one-time notice choice without changing window lifetime."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("WindowsBackgroundNoticeDialog")
        set_localized(self, "setWindowTitle", t("windows_background.notice_title"))
        self.setMinimumWidth(Theme.dialog_width)

        body = LocalizedLabel(t("windows_background.notice_body"))
        body.setWordWrap(True)
        body.setTextInteractionFlags(
            body.textInteractionFlags() | Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.dont_show_again = QCheckBox(self)
        self.dont_show_again.setObjectName("WindowsBackgroundDontShowAgain")
        set_localized(
            self.dont_show_again, "setText", t("windows_background.dont_show_again")
        )

        confirm = LocalizedPushButton(t("windows_background.confirm"))
        confirm.setObjectName("WindowsBackgroundConfirm")
        confirm.setDefault(True)
        confirm.clicked.connect(self.accept)

        exit_button = LocalizedPushButton(t("windows_background.quit"))
        exit_button.setObjectName("WindowsBackgroundQuit")
        exit_button.clicked.connect(lambda: self.done(EXIT_APPLICATION))

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(exit_button)
        buttons.addWidget(confirm)

        layout = QVBoxLayout(self)
        layout.setSpacing(Theme.dialog_gap)
        layout.addWidget(body)
        layout.addWidget(self.dont_show_again)
        layout.addLayout(buttons)
