"""Launch-time lock screen for Hidden Space.

When the user closed the previous session with "Show Collections" enabled,
the shelf is gated behind this overlay until the user either verifies
their password or chooses **Hide** (which flips the toggle off and
reveals the normal shelf — books stay marked hidden in the DB).
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from joyread.ui.resources.styles.theme import Theme


logger = logging.getLogger(__name__)


class HiddenSpaceLockOverlay(QWidget):
    """Full-window #ECECEC overlay with Verify / Hide buttons.

    Visual styling lives entirely in ``main.qss`` (rules keyed off the
    ``#HiddenSpaceLockOverlay`` / panel / hint object names and the
    ``HiddenSpaceLockButtonText`` class). The Python side owns only
    structure, layout, and signal wiring.
    """

    verified = QtSignal()
    dismissed = QtSignal()

    def __init__(
        self,
        parent: QWidget,
        *,
        hint: str | None,
        verify: Callable[[str], bool],
    ) -> None:
        # ``verify`` takes a plaintext password string and returns True
        # when the password is correct. Injected so the overlay stays
        # service-agnostic — MainWindow wires it to the SettingsViewModel
        # so layer isolation is preserved (View → ViewModel → Service).
        super().__init__(parent)
        self._verify = verify
        self.setObjectName("HiddenSpaceLockOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Reuse the standard ``JoyReadDialogPanel`` QSS class so the
        # panel inherits the same rounded corner radius, border, and
        # background as every other dialog. ``QFrame.Shape.NoFrame``
        # prevents Qt's default frame paint from masking the QSS-driven
        # border-radius.
        self._panel = QFrame()
        self._panel.setProperty("class", "JoyReadDialogPanel")
        self._panel.setFrameShape(QFrame.Shape.NoFrame)
        self._panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._panel.setFixedWidth(Theme.dialog_width)

        panel_layout = QVBoxLayout(self._panel)
        panel_layout.setContentsMargins(
            Theme.dialog_layout_margin,
            Theme.dialog_layout_margin,
            Theme.dialog_layout_margin,
            Theme.dialog_layout_margin,
        )
        panel_layout.setSpacing(Theme.spacing_md)
        panel_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        title_label = QLabel("Hidden Space")
        title_label.setProperty("class", "JoyReadDialogTitle")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        panel_layout.addWidget(title_label)

        self._hint_label = QLabel("")
        self._hint_label.setObjectName("HiddenSpaceLockHint")
        self._hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint_label.setWordWrap(True)
        if hint:
            self._hint_label.setText(f"Hint: {hint}")
        else:
            self._hint_label.setVisible(False)
        panel_layout.addWidget(self._hint_label)

        # Use the standard ``DialogInputField`` class so the password
        # field picks up the same rounded-corner styling as every other
        # dialog input.
        self._password = QLineEdit()
        self._password.setProperty("class", "DialogInputField")
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setPlaceholderText("Password")
        self._password.setFixedHeight(Theme.dialog_input_field_height)
        self._password.returnPressed.connect(self._on_verify_clicked)
        panel_layout.addWidget(self._password)

        self._state_label = QLabel("")
        self._state_label.setObjectName("HiddenSpaceLockState")
        self._state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._state_label.setVisible(False)
        panel_layout.addWidget(self._state_label)

        button_row = QWidget()
        button_layout = QHBoxLayout(button_row)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(Theme.spacing_md)

        self._hide_button = _HiddenSpaceLockButton("Hide")
        self._hide_button.clicked.connect(self._on_hide_clicked)
        button_layout.addWidget(self._hide_button)

        self._verify_button = _HiddenSpaceLockButton("Verify")
        self._verify_button.clicked.connect(self._on_verify_clicked)
        button_layout.addWidget(self._verify_button)

        panel_layout.addWidget(button_row)
        root_layout.addWidget(self._panel, alignment=Qt.AlignmentFlag.AlignCenter)

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def focus_password(self) -> None:
        self._password.setFocus(Qt.FocusReason.PopupFocusReason)

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


class _HiddenSpaceLockButton(QFrame):
    """Click-target QFrame styled as a ``DialogTextButton``.

    Reusing the ``DialogTextButton`` class gives us hover background and
    dialog-button radius for free; the label uses a dedicated class
    (``HiddenSpaceLockButtonText``) so the QSS can apply a larger font
    here without disturbing dialog buttons elsewhere.
    """

    clicked = QtSignal()

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pressed_inside = False
        self.setProperty("class", "DialogTextButton")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(Theme.dialog_button_height + 6)
        self.setMinimumWidth(120)
        self.setFrameShape(QFrame.Shape.NoFrame)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._label = QLabel(text)
        self._label.setProperty("class", "HiddenSpaceLockButtonText")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._label)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed_inside = True
            event.accept()
            return
        self._pressed_inside = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed_inside:
            self._pressed_inside = False
            event.accept()
            if self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
            return
        self._pressed_inside = False
        super().mouseReleaseEvent(event)
