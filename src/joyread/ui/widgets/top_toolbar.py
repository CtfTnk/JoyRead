"""Bookshelf content banner adapted from the Figma main content area."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QIcon, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.shelf_viewmodel import FileFilter
from joyread.ui.widgets.dropdown_button import FigmaDropdownButton
from joyread.ui.widgets.search_panel import SearchPanelWidget


class TopToolbarWidget(QWidget):
    search_changed = QtSignal(str)
    filter_changed = QtSignal(str)
    tag_filter_requested = QtSignal()

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(Theme.toolbar_height)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.content_horizontal_padding + Theme.banner_horizontal_padding,
            0,
            Theme.content_horizontal_padding + Theme.banner_horizontal_padding,
            0,
        )
        layout.setSpacing(10)

        self._title = QLabel("All")
        self._title.setObjectName("PageTitle")
        layout.addWidget(self._title)
        layout.addStretch(1)

        self._search_panel = SearchPanelWidget(resources)
        self._search_panel.search_submitted.connect(self.search_changed.emit)
        layout.addWidget(self._search_panel)

        layout.addWidget(_spacer())

        self._filter_dropdown = FigmaDropdownButton(
            resources,
            [filter_name.value for filter_name in FileFilter],
            width=Theme.file_filter_width,
            initial_value=FileFilter.ALL.value,
            tooltip="Filter by file type",
        )
        self._filter_dropdown.value_changed.connect(self.filter_changed.emit)
        layout.addWidget(self._filter_dropdown)

        self._tag_filter_button = TagFilterButton(resources)
        self._tag_filter_button.clicked.connect(self.tag_filter_requested.emit)
        layout.addWidget(self._tag_filter_button)

    def set_title(self, title: str) -> None:
        self._title.setText(title)

    def set_filter(self, filter_name: str) -> None:
        self._filter_dropdown.set_value(filter_name)

    def set_tag_filter_active(self, active: bool) -> None:
        self._tag_filter_button.set_active(active)


class TagFilterButton(QFrame):
    clicked = QtSignal()

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._resources = resources
        self._active = False
        self._pressed_inside = False
        self.setProperty("class", "FigmaTagFilterButton")
        self.setProperty("active", "false")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Filter by tag")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(Theme.toolbar_button_size, Theme.toolbar_button_size)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(4)
        shadow.setOffset(0, 1)
        shadow.setColor(QColor(0, 0, 0, 64))
        self.setGraphicsEffect(shadow)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Theme.control_layout_margin,
            Theme.control_layout_margin,
            Theme.control_layout_margin,
            Theme.control_layout_margin,
        )
        layout.setSpacing(0)

        self._icon = QLabel()
        self._icon.setObjectName("TagFilterButtonIcon")
        self._icon.setFixedSize(Theme.icon_size, Theme.icon_size)
        layout.addWidget(self._icon, alignment=Qt.AlignmentFlag.AlignCenter)
        self.set_active(False)

    @property
    def active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        requested_active = bool(active)
        pixmap = self._icon.pixmap()
        icon_loaded = pixmap is not None and not pixmap.isNull()
        if requested_active == self._active and icon_loaded:
            return

        self._active = requested_active
        self.setProperty("active", "true" if self._active else "false")
        icon_name = "icon_tag_selected.svg" if self._active else "icon_tag_unselected.svg"
        self._icon.setPixmap(QIcon(str(self._resources.icon_path(icon_name))).pixmap(_icon_qsize()))
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

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


def _spacer() -> QFrame:
    frame = QFrame()
    frame.setObjectName("ToolbarSpacer")
    frame.setFixedSize(Theme.toolbar_spacer_width, Theme.toolbar_control_height)
    frame.setFrameShape(QFrame.Shape.NoFrame)
    return frame


def _icon_qsize() -> QSize:
    return QSize(Theme.icon_size, Theme.icon_size)
