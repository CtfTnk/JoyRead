"""Launch-time lock screen for Hidden Space.

When the user closed the previous session with "Show Collections" enabled,
the shelf is gated behind this overlay until the user either verifies
their password or chooses **Hide** (which flips the toggle off and
reveals the normal shelf — books stay marked hidden in the DB).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from joyread.ui.resources.styles.theme import Theme


logger = logging.getLogger(__name__)


class HiddenSpaceLockOverlay(QWidget):
    """Full-window #ECECEC overlay with Verify / Hide buttons."""

    verified = QtSignal()
    dismissed = QtSignal()

    def __init__(
        self,
        parent: QWidget,
        *,
        hint: str | None,
        verify: callable,
    ) -> None:
        # ``verify`` takes a plaintext password string and returns True
        # when the password is correct. Injected so the overlay stays
        # service-agnostic (and trivially testable).
        super().__init__(parent)
        self._verify = verify
        self.setObjectName("HiddenSpaceLockOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Inline stylesheet for the flat #ECECEC background. Using a
        # property class would require touching main.qss; the colour is
        # the same Theme.color_reader_background token applied directly.
        self.setStyleSheet(f"#HiddenSpaceLockOverlay {{ background-color: {Theme.color_reader_background}; }}")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._panel = QFrame()
        self._panel.setObjectName("HiddenSpaceLockPanel")
        self._panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._panel.setFixedWidth(Theme.dialog_width)
        # Match the standard dialog panel chrome so the lock screen feels
        # like a single sentence rather than a one-off widget.
        self._panel.setStyleSheet(
            "#HiddenSpaceLockPanel { background-color: #ffffff; border: 2px solid #929292; border-radius: 10px; }"
        )

        panel_layout = QVBoxLayout(self._panel)
        panel_layout.setContentsMargins(
            Theme.dialog_layout_margin,
            Theme.dialog_layout_margin,
            Theme.dialog_layout_margin,
            Theme.dialog_layout_margin,
        )
        panel_layout.setSpacing(12)
        panel_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        title_label = QLabel("Hidden Space")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet("font-weight: 600; font-size: 16px;")
        panel_layout.addWidget(title_label)

        self._hint_label = QLabel("")
        self._hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint_label.setWordWrap(True)
        self._hint_label.setStyleSheet("color: #6d6d6d; font-size: 13px;")
        if hint:
            self._hint_label.setText(f"Hint: {hint}")
        else:
            self._hint_label.setVisible(False)
        panel_layout.addWidget(self._hint_label)

        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setPlaceholderText("Password")
        self._password.setFixedHeight(32)
        self._password.returnPressed.connect(self._on_verify_clicked)
        panel_layout.addWidget(self._password)

        self._state_label = QLabel("")
        self._state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._state_label.setStyleSheet("color: #bf0c0c; font-size: 12px;")
        self._state_label.setVisible(False)
        panel_layout.addWidget(self._state_label)

        button_row = QWidget()
        button_layout = QHBoxLayout(button_row)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(12)

        self._hide_button = self._make_button("Hide")
        self._hide_button.clicked.connect(self._on_hide_clicked)
        button_layout.addWidget(self._hide_button)

        self._verify_button = self._make_button("Verify", emphasis=True)
        self._verify_button.clicked.connect(self._on_verify_clicked)
        button_layout.addWidget(self._verify_button)

        panel_layout.addWidget(button_row)
        root_layout.addWidget(self._panel, alignment=Qt.AlignmentFlag.AlignCenter)

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def focus_password(self) -> None:
        self._password.setFocus(Qt.FocusReason.PopupFocusReason)

    def _make_button(self, text: str, *, emphasis: bool = False) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedSize(120, 28)
        weight = "600" if emphasis else "500"
        bg = "#e5e5e5" if emphasis else "#ffffff"
        button.setStyleSheet(
            f"QToolButton {{ background-color: {bg}; border: 1px solid #e0e0e0; "
            f"border-radius: 6px; font-weight: {weight}; }}"
        )
        return button

    def _on_verify_clicked(self) -> None:
        password = self._password.text()
        try:
            ok = bool(self._verify(password))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("HiddenSpaceLockOverlay verify callable failed: %s", exc)
            ok = False
        if not ok:
            self._state_label.setText("Incorrect password.")
            self._state_label.setVisible(True)
            self._password.selectAll()
            self._password.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        logger.info("HiddenSpaceLockOverlay: password verified")
        self.verified.emit()

    def _on_hide_clicked(self) -> None:
        logger.info("HiddenSpaceLockOverlay: user chose Hide (toggle off, no password)")
        self.dismissed.emit()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # Block click-through to the (still-hidden) shelf.
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # Esc is intentionally a no-op: the lock screen has to be
        # explicitly dismissed via Verify or Hide so the user can't
        # accidentally skip it.
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            return
        super().keyPressEvent(event)
