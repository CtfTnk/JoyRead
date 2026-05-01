"""Left navigation sidebar."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from joyread.core.models.collection import Collection
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.shelf_viewmodel import ShelfKey, collection_shelf_key


class SidebarWidget(QWidget):
    navigation_requested = QtSignal(str)

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._resources = resources
        self.setObjectName("Sidebar")
        self.setFixedWidth(260)
        self._buttons: dict[str, QToolButton] = {}

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(4, 10, 4, 10)
        self._layout.setSpacing(4)

        self._add_section("Book Shelf")
        self._add_item("All", ShelfKey.ALL.value, "icon_book_all.svg", checked=True)
        self._add_item("Recent", ShelfKey.RECENT.value, "icon_recent.svg")
        self._add_item("Favourites", ShelfKey.FAVOURITES.value, "icon_favourite_enabled.svg")

        self._add_section("Collections")
        self._add_item("New Collection", "new_collection", "icon_add.svg")
        self._add_item("A Collection", collection_shelf_key("collection-a"), "icon_collection.svg")

        self._layout.addStretch(1)
        self._add_item("Settings", "settings", "icon_setting.svg")

    def set_collections(self, collections: list[Collection]) -> None:
        # The first implementation keeps one mock collection visible per Figma.
        if collections:
            key = collection_shelf_key(collections[0].uuid)
            button = self._buttons.get(collection_shelf_key("collection-a"))
            if button is not None:
                button.setText(collections[0].name)
                self._buttons[key] = button

    def set_active(self, key: str) -> None:
        for item_key, button in self._buttons.items():
            button.setChecked(item_key == key)

    def _add_section(self, title: str) -> None:
        label = QLabel(title)
        label.setObjectName("SidebarSection")
        label.setFixedHeight(Theme.sidebar_section_height)
        label.setContentsMargins(15, 10, 10, 5)
        self._layout.addWidget(label)

    def _add_item(self, label: str, key: str, icon_name: str, checked: bool = False) -> None:
        button = QToolButton()
        button.setProperty("class", "SidebarItem")
        button.setText(label)
        button.setIcon(QIcon(str(self._resources.icon_path(icon_name))))
        button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setCheckable(True)
        button.setChecked(checked)
        button.setFixedHeight(Theme.sidebar_item_height)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        button.clicked.connect(lambda _checked=False, item_key=key: self.navigation_requested.emit(item_key))
        self._buttons[key] = button
        self._layout.addWidget(button)
