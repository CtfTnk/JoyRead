"""Bookshelf content banner adapted from the Figma main content area."""

from __future__ import annotations

from PySide6.QtCore import Signal as QtSignal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
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

    def set_title(self, title: str) -> None:
        self._title.setText(title)

def _spacer() -> QFrame:
    frame = QFrame()
    frame.setObjectName("ToolbarSpacer")
    frame.setFixedSize(Theme.toolbar_spacer_width, Theme.toolbar_control_height)
    frame.setFrameShape(QFrame.Shape.NoFrame)
    return frame
    