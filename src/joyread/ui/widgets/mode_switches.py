"""Figma-aligned switch controls for view and sort mode selection."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QRect, QRectF, QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QToolButton, QWidget

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.shelf_viewmodel import ViewMode


@dataclass(frozen=True)
class SwitchOption:
    value: str
    icon_name: str
    tooltip: str


class FigmaSwitchOptionButton(QToolButton):
    """Paints Figma's switch option variants without letting QSS alter geometry."""

    def __init__(self, icon: QIcon, tooltip: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "FigmaSwitchOption")
        self.setIcon(icon)
        self.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
        self.setToolTip(tooltip)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(Theme.switch_option_size, Theme.switch_option_size)

    def enterEvent(self, event) -> None:  # noqa: ANN001 - PySide event type differs by Qt minor version.
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: ANN001 - PySide event type differs by Qt minor version.
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ARG002
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        is_selected = self.isChecked()
        if not self.isEnabled():
            background = QColor(Theme.color_button_edge)
            draw_border = is_selected
        elif is_selected:
            background = QColor(Theme.color_window)
            draw_border = True
        elif self.underMouse():
            background = QColor(Theme.color_selected)
            draw_border = False
        else:
            background = QColor(Theme.color_switch_background)
            draw_border = False

        if draw_border:
            inset = Theme.switch_option_border_width / 2
            option_rect = QRectF(
                inset,
                inset,
                self.width() - Theme.switch_option_border_width,
                self.height() - Theme.switch_option_border_width,
            )
            painter.setPen(QPen(QColor(Theme.color_button_inner_edge), Theme.switch_option_border_width))
        else:
            option_rect = QRectF(0, 0, self.width(), self.height())
            painter.setPen(Qt.PenStyle.NoPen)

        painter.setBrush(background)
        painter.drawRoundedRect(option_rect, Theme.switch_option_radius, Theme.switch_option_radius)

        # Keep the SVG centered in the fixed 28px option. Qt's painted inner
        # edge should not shift the icon the way Figma's floating stroke might
        # appear to in exported coordinates.
        icon_rect = QRect(
            Theme.switch_option_icon_inset,
            Theme.switch_option_icon_inset,
            Theme.icon_size,
            Theme.icon_size,
        )
        self.icon().paint(painter, icon_rect)


class FigmaSwitchWidget(QFrame):
    """Shared implementation for Figma's 70x36 two-option switch components."""

    value_changed = QtSignal(str)

    def __init__(
        self,
        resources: ResourceLoader,
        options: Sequence[SwitchOption],
        initial_value: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if len(options) != 2:
            raise ValueError("FigmaSwitchWidget currently implements Figma's two-option switch pattern.")

        self._resources = resources
        self._buttons: dict[str, QToolButton] = {}
        self._value = initial_value

        self.setProperty("class", "FigmaSwitch")
        self.setFixedSize(Theme.switch_width, Theme.switch_height)

        layout = QHBoxLayout(self)
        # Figma's switch padding is 4px from the visual outer stroke. Qt's
        # 2px border consumes layout space, so the actual layout margin is 2px.
        layout.setContentsMargins(
            Theme.switch_layout_margin,
            Theme.switch_layout_margin,
            Theme.switch_layout_margin,
            Theme.switch_layout_margin,
        )
        layout.setSpacing(Theme.switch_gap)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for option in options:
            button = self._make_button(option)
            self._buttons[option.value] = button
            self._group.addButton(button)
            layout.addWidget(button)

        self.set_value(initial_value, emit=False)

    @property
    def value(self) -> str:
        return self._value

    def set_value(self, value: str, emit: bool = False) -> None:
        if value not in self._buttons:
            raise ValueError(f"Unknown switch value: {value}")
        changed = value != self._value
        self._value = value
        for option_value, button in self._buttons.items():
            button.setChecked(option_value == value)
        if changed and emit:
            self.value_changed.emit(value)

    def _make_button(self, option: SwitchOption) -> QToolButton:
        button = FigmaSwitchOptionButton(
            QIcon(str(self._resources.icon_path(option.icon_name))),
            option.tooltip,
        )
        # Figma marks onSelect only for the inactive option; repeat-clicking
        # the active option should not emit a selection command.
        button.clicked.connect(lambda _checked=False, value=option.value: self._handle_option_clicked(value))
        return button

    def _handle_option_clicked(self, value: str) -> None:
        if value == self._value:
            self.set_value(self._value, emit=False)
            return
        self.set_value(value, emit=True)


class ListModeSwitchWidget(FigmaSwitchWidget):
    def __init__(
        self,
        resources: ResourceLoader,
        initial_value: str = ViewMode.GRID.value,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            resources=resources,
            options=(
                SwitchOption(ViewMode.LIST.value, "icon_list_detailMode.svg", "List mode"),
                SwitchOption(ViewMode.GRID.value, "icon_list_cardMode.svg", "Grid mode"),
            ),
            initial_value=initial_value,
            parent=parent,
        )


class SortModeSwitchWidget(FigmaSwitchWidget):
    ASCENDING = "ascending"
    DESCENDING = "descending"

    def __init__(
        self,
        resources: ResourceLoader,
        initial_value: str = DESCENDING,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            resources=resources,
            options=(
                SwitchOption(self.ASCENDING, "icon_sort_a-z.svg", "Sort ascending"),
                SwitchOption(self.DESCENDING, "icon_sort_z-a.svg", "Sort descending"),
            ),
            initial_value=initial_value,
            parent=parent,
        )

    @property
    def ascending(self) -> bool:
        return self.value == self.ASCENDING

    def set_ascending(self, ascending: bool, emit: bool = False) -> None:
        self.set_value(self.ASCENDING if ascending else self.DESCENDING, emit=emit)
