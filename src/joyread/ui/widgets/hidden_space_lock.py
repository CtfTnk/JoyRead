"""Launch-time lock screen for Hidden Space.

When the user closed the previous session with "Show Collections" enabled,
the Library opens behind this overlay, asking for the passphrase. **Unlock**
asks the window to unlock the vault, which takes a moment (scrypt) and runs
off the UI thread, so the overlay only shows a busy state and then the
result. **Hide** turns Show Collections off; hidden books stay encrypted.
**Use recovery code** hands over to the recovery dialog.

The hidden books are encrypted whether or not this screen is passed, so it
guards nothing by itself: it is the prompt the user asked for by leaving
Hidden Space shown.
"""

from __future__ import annotations

from joyread.ui.widgets.localized_text import LocalizedLabel, set_localized

import logging

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

from joyread.infrastructure.i18n.locale_service import t
from joyread.ui.resources.styles.theme import Theme


logger = logging.getLogger(__name__)


class HiddenSpaceLockOverlay(QWidget):
    """Full-window overlay with Hide / Unlock buttons and a recovery route.

    Visual styling lives entirely in ``main.qss`` (rules keyed off the
    ``#HiddenSpaceLockOverlay`` / panel / state object names and the
    ``HiddenSpaceLockButtonText`` / ``HiddenSpaceLockLinkText`` classes). The
    Python side owns only structure, layout, and signal wiring; the window
    decides what an unlock request means (unlock, or the legacy upgrade).

    There is deliberately no passphrase hint: this screen is shown to whoever
    is at the keyboard, which is exactly who the passphrase keeps out.
    """

    #: The typed passphrase. Answer with :meth:`show_error`, or tear down.
    unlock_requested = QtSignal(str)
    recovery_requested = QtSignal()
    dismissed = QtSignal()

    def __init__(
        self,
        parent: QWidget,
        *,
        field_label: str | None = None,
        recovery_available: bool = True,
    ) -> None:
        super().__init__(parent)
        self._busy = False
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

        title_label = LocalizedLabel(t("dialog.hidden_space_title"))
        title_label.setProperty("class", "JoyReadDialogTitle")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        panel_layout.addWidget(title_label)

        # Use the standard ``DialogInputField`` class so the password
        # field picks up the same rounded-corner styling as every other
        # dialog input.
        self._password = QLineEdit()
        self._password.setProperty("class", "DialogInputField")
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setInputMethodHints(Qt.InputMethodHint.ImhNone)
        set_localized(
            self._password, "setPlaceholderText", field_label or t("hidden_space.passphrase_header")
        )
        self._password.setFixedHeight(Theme.dialog_input_field_height)
        self._password.returnPressed.connect(self._on_verify_clicked)
        panel_layout.addWidget(self._password)

        self._state_label = LocalizedLabel("")
        self._state_label.setObjectName("HiddenSpaceLockState")
        self._state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._state_label.setVisible(False)
        panel_layout.addWidget(self._state_label)

        button_row = QWidget()
        button_layout = QHBoxLayout(button_row)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(Theme.spacing_md)

        self._hide_button = _HiddenSpaceLockButton(t("dialog.btn_hide"))
        self._hide_button.clicked.connect(self._on_hide_clicked)
        button_layout.addWidget(self._hide_button)

        self._verify_button = _HiddenSpaceLockButton(t("hidden_space.btn_unlock"))
        self._verify_button.clicked.connect(self._on_verify_clicked)
        button_layout.addWidget(self._verify_button)

        panel_layout.addWidget(button_row)

        self._recovery_link = _HiddenSpaceLockButton(
            t("hidden_space.use_recovery_code"), text_class="HiddenSpaceLockLinkText", link=True
        )
        self._recovery_link.clicked.connect(self._on_recovery_clicked)
        self._recovery_link.setVisible(recovery_available)
        panel_layout.addWidget(self._recovery_link, alignment=Qt.AlignmentFlag.AlignHCenter)
        root_layout.addWidget(self._panel, alignment=Qt.AlignmentFlag.AlignCenter)

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def focus_password(self) -> None:
        self._password.setFocus(Qt.FocusReason.PopupFocusReason)

    @property
    def is_busy(self) -> bool:
        return self._busy

    def set_busy(self, busy: bool) -> None:
        """While an unlock runs: nothing to press, and say so."""

        self._busy = bool(busy)
        for control in (self._password, self._hide_button, self._verify_button, self._recovery_link):
            control.setEnabled(not self._busy)
        if self._busy:
            self._show_state(t("hidden_space.unlocking"), error=False)

    def show_error(self, message: str) -> None:
        self.set_busy(False)
        self._show_state(message, error=True)
        self._password.selectAll()
        self._password.setFocus(Qt.FocusReason.OtherFocusReason)

    def _show_state(self, message: str, *, error: bool) -> None:
        set_localized(self._state_label, "setText", message)
        self._state_label.setProperty("error", "true" if error else "false")
        self._state_label.style().unpolish(self._state_label)
        self._state_label.style().polish(self._state_label)
        self._state_label.setVisible(bool(message))

    def _on_verify_clicked(self) -> None:
        if self._busy:
            return
        password = self._password.text()
        if not password:
            self._show_state(t("hidden_space_error.passphrase_required"), error=True)
            return
        self.unlock_requested.emit(password)

    def _on_hide_clicked(self) -> None:
        if self._busy:
            return
        logger.info("HiddenSpaceLockOverlay: user chose Hide (toggle off, no password)")
        self.dismissed.emit()

    def _on_recovery_clicked(self) -> None:
        if self._busy:
            return
        self.recovery_requested.emit()

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

    def __init__(
        self,
        text: str,
        parent: QWidget | None = None,
        *,
        text_class: str = "HiddenSpaceLockButtonText",
        link: bool = False,
    ) -> None:
        super().__init__(parent)
        self._pressed_inside = False
        # A link is the quieter, secondary route: text only, no button frame.
        self.setProperty("class", "HiddenSpaceLockLink" if link else "DialogTextButton")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(Theme.dialog_button_height + (0 if link else 6))
        if not link:
            self.setMinimumWidth(120)
        self.setFrameShape(QFrame.Shape.NoFrame)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._label = LocalizedLabel(text)
        self._label.setProperty("class", text_class)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._label)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self.isEnabled():
            event.accept()
            return
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
