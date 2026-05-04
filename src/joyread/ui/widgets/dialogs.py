"""Reusable app-centered popup dialogs adapted from Figma node 483:1989."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from joyread.ui.resources.styles.theme import Theme


class DialogTextButton(QFrame):
    """Figma ButtonLongText: fixed 100x28 text button with centered label."""

    clicked = QtSignal()

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pressed_inside = False
        self.setProperty("class", "DialogTextButton")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(Theme.dialog_button_width, Theme.dialog_button_height)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(Theme.dialog_button_shadow_blur)
        shadow.setOffset(Theme.dialog_button_shadow_offset, Theme.dialog_button_shadow_offset)
        shadow.setColor(QColor(*Theme.color_shadow_rgba))
        self.setGraphicsEffect(shadow)

        layout = QHBoxLayout(self)
        # Figma uses 4px visual padding and a 1px stroke. Qt borders consume
        # layout space, so 3px margins keep the text inset visually at 4px.
        layout.setContentsMargins(
            Theme.dialog_button_layout_margin,
            Theme.dialog_button_layout_margin,
            Theme.dialog_button_layout_margin,
            Theme.dialog_button_layout_margin,
        )
        layout.setSpacing(0)

        self._label = QLabel(text)
        self._label.setProperty("class", "DialogTextButtonText")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self._label)

    @property
    def text(self) -> str:
        return self._label.text()

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

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class JoyReadDialogPanel(QFrame):
    """Figma's 400x220 general popup panel with title/content/options areas."""

    accepted = QtSignal()
    rejected = QtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "JoyReadDialogPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(Theme.dialog_width, Theme.dialog_height)

        root_layout = QVBoxLayout(self)
        # Figma panel has 10px visual padding and a 2px stroke. Subtract the
        # QSS border so the child frames still begin 10px from the outer edge.
        root_layout.setContentsMargins(
            Theme.dialog_layout_margin,
            Theme.dialog_layout_margin,
            Theme.dialog_layout_margin,
            Theme.dialog_layout_margin,
        )
        root_layout.setSpacing(Theme.dialog_gap)

        self._title_area = QWidget()
        self._title_area.setObjectName("JoyReadDialogTitleArea")
        title_layout = QHBoxLayout(self._title_area)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(0)
        title_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._title_label = QLabel("Title")
        self._title_label.setProperty("class", "JoyReadDialogTitle")
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_layout.addWidget(self._title_label)
        root_layout.addWidget(self._title_area)

        self._content_area = QWidget()
        self._content_area.setObjectName("JoyReadDialogContentArea")
        self._content_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        content_layout = QHBoxLayout(self._content_area)
        content_layout.setContentsMargins(
            Theme.dialog_content_padding,
            Theme.dialog_content_padding,
            Theme.dialog_content_padding,
            Theme.dialog_content_padding,
        )
        content_layout.setSpacing(0)
        content_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._content_label = QLabel("content")
        self._content_label.setProperty("class", "JoyReadDialogContent")
        self._content_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_label.setWordWrap(True)
        content_layout.addWidget(self._content_label)
        root_layout.addWidget(self._content_area, stretch=1)

        self._option_area = QWidget()
        self._option_area.setObjectName("JoyReadDialogOptionArea")
        self._option_layout = QHBoxLayout(self._option_area)
        self._option_layout.setContentsMargins(0, 0, 0, 0)
        self._option_layout.setSpacing(Theme.dialog_option_gap)
        self._option_layout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        root_layout.addWidget(self._option_area)

    def sizeHint(self) -> QSize:
        return QSize(Theme.dialog_width, Theme.dialog_height)

    def set_info(self, title: str, message: str, button_text: str) -> None:
        self._set_text(title, message)
        self._set_buttons(((button_text, self.accepted.emit),))

    def set_confirm(self, title: str, message: str, cancel_text: str, confirm_text: str) -> None:
        self._set_text(title, message)
        self._set_buttons(
            (
                (cancel_text, self.rejected.emit),
                (confirm_text, self.accepted.emit),
            )
        )

    def _set_text(self, title: str, message: str) -> None:
        self._title_label.setText(title)
        self._content_label.setText(message)

    def _set_buttons(self, buttons: tuple[tuple[str, Callable[[], None]], ...]) -> None:
        while self._option_layout.count():
            item = self._option_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        self._option_layout.addStretch(1)
        for label, callback in buttons:
            button = DialogTextButton(label, self)
            button.clicked.connect(callback)
            self._option_layout.addWidget(button)


class JoyReadDialogOverlay(QWidget):
    """Window-local modal layer that closes only through dialog buttons."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("JoyReadDialogOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._on_accept: Callable[[], None] | None = None
        self._on_reject: Callable[[], None] | None = None

        self._panel = JoyReadDialogPanel(self)
        self._panel.accepted.connect(self._accept)
        self._panel.rejected.connect(self._reject)
        self.hide()

    @property
    def panel(self) -> JoyReadDialogPanel:
        return self._panel

    def show_info(self, title: str, message: str, button_text: str = "Confirm") -> None:
        self._on_accept = None
        self._on_reject = None
        self._panel.set_info(title, message, button_text)
        self._show_centered()

    def show_confirm(
        self,
        title: str,
        message: str,
        on_confirm: Callable[[], None],
        on_cancel: Callable[[], None] | None = None,
        confirm_text: str = "Confirm",
        cancel_text: str = "Cancel",
    ) -> None:
        self._on_accept = on_confirm
        self._on_reject = on_cancel
        self._panel.set_confirm(title, message, cancel_text, confirm_text)
        self._show_centered()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # The popup is intentionally modal inside the app window: blank clicks
        # are swallowed but do not dismiss it.
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_panel()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._position_panel()

    def _show_centered(self) -> None:
        self._position_panel()
        self.show()
        self.raise_()
        self._panel.raise_()
        self.setFocus(Qt.FocusReason.PopupFocusReason)

    def _position_panel(self) -> None:
        x = (self.width() - self._panel.width()) // 2
        y = (self.height() - self._panel.height()) // 2
        self._panel.move(max(0, x), max(0, y))

    def _accept(self) -> None:
        callback = self._on_accept
        self._clear_and_hide()
        if callback is not None:
            callback()

    def _reject(self) -> None:
        callback = self._on_reject
        self._clear_and_hide()
        if callback is not None:
            callback()

    def _clear_and_hide(self) -> None:
        self._on_accept = None
        self._on_reject = None
        self.hide()
