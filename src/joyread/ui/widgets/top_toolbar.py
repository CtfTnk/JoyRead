"""Bookshelf content banner adapted from the Figma main content area."""

from __future__ import annotations

from PySide6.QtCore import QSize, Signal as QtSignal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QWidget,
)

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.shelf_viewmodel import FileFilter


class TopToolbarWidget(QWidget):
    search_changed = QtSignal(str)
    filter_changed = QtSignal(str)
    collapse_requested = QtSignal()

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._resources = resources
        self.setFixedHeight(Theme.toolbar_height)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(10)

        self._title = QLabel("All")
        self._title.setObjectName("PageTitle")
        layout.addWidget(self._title)
        layout.addStretch(1)

        search_panel = QFrame()
        search_panel.setProperty("class", "SearchPanel")
        search_panel.setFixedSize(Theme.search_panel_width, Theme.toolbar_control_height)
        search_layout = QHBoxLayout(search_panel)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(6)

        self._search = QLineEdit()
        self._search.setObjectName("SearchField")
        self._search.setPlaceholderText("Search anything...")
        self._search.addAction(
            QIcon(str(resources.icon_path("icon_search.svg"))),
            QLineEdit.ActionPosition.TrailingPosition,
        )
        self._search.setFixedSize(Theme.search_width, Theme.toolbar_control_height)
        self._search.textChanged.connect(self.search_changed.emit)
        search_layout.addWidget(self._search)

        self._collapse_button = _toolbar_button(QIcon(str(resources.icon_path("icon_left.svg"))), "Collapse search")
        self._collapse_button.clicked.connect(self.collapse_requested.emit)
        search_layout.addWidget(self._collapse_button)
        layout.addWidget(search_panel)

        self._filter_combo = QComboBox()
        self._filter_combo.setProperty("class", "ToolbarControl")
        self._filter_combo.addItems([filter_name.value for filter_name in FileFilter])
        self._filter_combo.setFixedSize(Theme.file_filter_width, Theme.toolbar_control_height)
        self._filter_combo.currentTextChanged.connect(self.filter_changed.emit)
        layout.addWidget(self._filter_combo)

    def set_title(self, title: str) -> None:
        self._title.setText(title)

def _toolbar_button(icon: QIcon, tooltip: str) -> QToolButton:
    button = QToolButton()
    button.setProperty("class", "ToolbarButton")
    button.setIcon(icon)
    button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
    button.setToolTip(tooltip)
    button.setFixedSize(Theme.toolbar_button_size, Theme.toolbar_control_height)
    return button
