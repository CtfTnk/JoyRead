"""Figma-derived dropdown buttons used by the bookshelf toolbar chrome."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QIcon, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.menus import FigmaMenu


class FigmaDropdownButton(QFrame):
    """Button matching Figma's dropdown component with a custom same-width menu."""

    value_changed = QtSignal(str)

    def __init__(
        self,
        resources: ResourceLoader,
        options: Sequence[str],
        *,
        width: int,
        initial_value: str,
        tooltip: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if initial_value not in options:
            raise ValueError(f"Initial dropdown value {initial_value!r} is not in options.")

        self._resources = resources
        self._options = tuple(options)
        self._value = initial_value

        self.setProperty("class", "FigmaDropdownButton")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tooltip)
        self.setFixedSize(width, Theme.toolbar_control_height)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(4)
        shadow.setOffset(0, 1)
        shadow.setColor(QColor(0, 0, 0, 64))
        self.setGraphicsEffect(shadow)

        layout = QHBoxLayout(self)
        # Figma's 4px visual padding includes the 1px stroke; Qt's QSS border
        # consumes layout space, so the actual layout margin is 3px.
        layout.setContentsMargins(
            Theme.control_layout_margin,
            Theme.control_layout_margin,
            Theme.control_layout_margin,
            Theme.control_layout_margin,
        )
        layout.setSpacing(Theme.control_gap)

        self._label = QLabel(initial_value)
        self._label.setProperty("class", "FigmaDropdownText")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setFixedHeight(Theme.control_text_height)
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._label, stretch=1)

        self._inner_button = QFrame()
        self._inner_button.setObjectName("FigmaDropdownInnerButton")
        self._inner_button.setProperty("class", "FigmaDropdownInnerButton")
        self._inner_button.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._inner_button.setFixedSize(Theme.dropdown_inner_size, Theme.dropdown_inner_size)
        self._set_inner_hovered(False)

        inner_layout = QVBoxLayout(self._inner_button)
        inner_layout.setContentsMargins(0, 0, 0, 0)
        inner_layout.setSpacing(0)

        icon_label = QLabel()
        icon_label.setFixedSize(Theme.icon_size, Theme.icon_size)
        icon_label.setPixmap(QIcon(str(resources.icon_path("icon_dropout.svg"))).pixmap(_icon_qsize()))
        inner_layout.addWidget(icon_label, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._inner_button)

    @property
    def value(self) -> str:
        return self._value

    def set_value(self, value: str, *, emit: bool = False) -> None:
        if value not in self._options:
            raise ValueError(f"Unknown dropdown value: {value}")
        changed = value != self._value
        self._value = value
        self._label.setText(value)
        if changed and emit:
            self.value_changed.emit(value)

    def update_options(self, options: Sequence[str], new_value: str) -> None:
        """Replace the option list and select a new value without emitting a signal.

        Used when display labels change (e.g. on language switch) while the
        logical selection stays the same.
        """
        if new_value not in options:
            raise ValueError(f"update_options: new_value {new_value!r} not in options")
        self._options = tuple(options)
        self._value = new_value
        self._label.setText(new_value)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._open_menu()
            event.accept()
            return
        super().mousePressEvent(event)

    def enterEvent(self, event) -> None:  # noqa: ANN001 - PySide event type differs by Qt minor version.
        self._set_inner_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001 - PySide event type differs by Qt minor version.
        self._set_inner_hovered(False)
        super().leaveEvent(event)

    def _open_menu(self) -> None:
        menu = FigmaMenu(self, width=self.width())
        menu.closed.connect(self._finish_menu_interaction)
        for option in self._options:
            menu.add_item(option, lambda value=option: self.set_value(value, emit=True))
        self._finish_menu_interaction()
        menu.exec(self.mapToGlobal(QPoint(0, self.height())))

    def _finish_menu_interaction(self) -> None:
        # Popup mouse capture can swallow leave events. Clear the visual hover
        # state explicitly so the control cannot stay tinted after dismissal.
        self._set_inner_hovered(False)
        self.clearFocus()
        _send_leave_event(self)

    def _set_inner_hovered(self, hovered: bool) -> None:
        self._inner_button.setProperty("hovered", "true" if hovered else "false")
        self._inner_button.style().unpolish(self._inner_button)
        self._inner_button.style().polish(self._inner_button)
        self._inner_button.update()


def _icon_qsize() -> QSize:
    return QSize(Theme.icon_size, Theme.icon_size)


def _send_leave_event(widget: QWidget) -> None:
    app = QApplication.instance()
    if app is not None:
        app.sendEvent(widget, QEvent(QEvent.Type.Leave))
