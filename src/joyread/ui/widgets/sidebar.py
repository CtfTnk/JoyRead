"""Left navigation sidebar aligned to the Figma sidebar frame."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QIcon, QMouseEvent
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

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
        self.setFixedWidth(Theme.sidebar_width)
        self._buttons: dict[str, SidebarItemWidget] = {}
        self._visible_collection_key = collection_shelf_key("collection-a")

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(
            Theme.sidebar_margin_horizontal,
            Theme.sidebar_margin_vertical,
            Theme.sidebar_margin_horizontal,
            Theme.sidebar_margin_vertical,
        )
        root_layout.setSpacing(0)

        upper_part = QWidget()
        upper_part.setObjectName("SidebarUpperPart")
        upper_layout = QVBoxLayout(upper_part)
        upper_layout.setContentsMargins(0, 0, 0, 0)
        upper_layout.setSpacing(Theme.sidebar_gap)

        upper_layout.addWidget(
            self._section(
                "Book Shelf",
                (
                    ("All", ShelfKey.ALL.value, "icon_book_all.svg", True),
                    ("Recent", ShelfKey.RECENT.value, "icon_recent.svg", False),
                    ("Favourites", ShelfKey.FAVOURITES.value, "icon_favourite_enabled.svg", False),
                ),
            )
        )
        upper_layout.addWidget(
            self._section(
                "Collections",
                (
                    ("New Collection", "new_collection", "icon_add.svg", False),
                    ("A Collection", self._visible_collection_key, "icon_collection.svg", False),
                ),
            )
        )

        root_layout.addWidget(upper_part)
        root_layout.addStretch(1)

        lower_part = QWidget()
        lower_part.setObjectName("SidebarLowerPart")
        lower_layout = QVBoxLayout(lower_part)
        lower_layout.setContentsMargins(0, 0, 0, Theme.sidebar_lower_padding_bottom)
        lower_layout.setSpacing(0)
        lower_layout.addWidget(self._item("Settings", "settings", "icon_setting.svg"))
        root_layout.addWidget(lower_part)

    def set_collections(self, collections: list[Collection]) -> None:
        # The first implementation keeps one mock collection visible per Figma.
        if collections:
            key = collection_shelf_key(collections[0].uuid)
            button = self._buttons.pop(self._visible_collection_key, None)
            if button is not None:
                button.set_label(collections[0].name)
                button.set_navigation_key(key)
                self._buttons[key] = button
                self._visible_collection_key = key

    def set_active(self, key: str) -> None:
        for item_key, button in self._buttons.items():
            button.set_checked(item_key == key)

    def _section(self, title: str, items: tuple[tuple[str, str, str, bool], ...]) -> QWidget:
        section = QWidget()
        section.setObjectName("SidebarSectionGroup")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Theme.sidebar_gap)

        layout.addWidget(SidebarSectionBanner(title, self._resources))
        for label, key, icon_name, checked in items:
            layout.addWidget(self._item(label, key, icon_name, checked=checked))
        return section

    def _item(self, label: str, key: str, icon_name: str, checked: bool = False) -> "SidebarItemWidget":
        button = SidebarItemWidget(label, icon_name, self._resources, navigation_key=key, checked=checked)
        button.clicked.connect(lambda item=button: self.navigation_requested.emit(item.navigation_key))
        self._buttons[key] = button
        return button


class SidebarSectionBanner(QFrame):
    """Figma sidebar_section_banner: text left, 20px dropout indicator right."""

    def __init__(self, title: str, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SidebarSectionBanner")
        self.setFixedHeight(Theme.sidebar_section_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.sidebar_section_padding_left,
            Theme.sidebar_section_padding_top,
            Theme.sidebar_section_padding_right,
            Theme.sidebar_section_padding_bottom,
        )
        layout.setSpacing(0)

        label = QLabel(title)
        label.setObjectName("SidebarSectionLabel")
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(label, stretch=1)

        arrow = QLabel()
        arrow.setObjectName("SidebarSectionArrow")
        arrow.setFixedSize(Theme.sidebar_section_arrow_size, Theme.sidebar_section_arrow_size)
        arrow.setPixmap(
            QIcon(str(resources.icon_path("icon_dropout.svg"))).pixmap(
                QSize(Theme.sidebar_section_arrow_size, Theme.sidebar_section_arrow_size)
            )
        )
        layout.addWidget(arrow)


class SidebarItemWidget(QFrame):
    """Figma sidebar item with fixed padding and a 5px icon/text label group gap."""

    clicked = QtSignal()

    def __init__(
        self,
        label: str,
        icon_name: str,
        resources: ResourceLoader,
        *,
        navigation_key: str,
        checked: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.navigation_key = navigation_key
        self._pressed_inside = False
        self.setProperty("class", "SidebarItem")
        self.setProperty("selected", "true" if checked else "false")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(Theme.sidebar_item_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.sidebar_item_padding_left,
            Theme.sidebar_item_padding_vertical,
            Theme.sidebar_item_padding_right,
            Theme.sidebar_item_padding_vertical,
        )
        layout.setSpacing(Theme.sidebar_item_icon_text_gap)

        icon = QLabel()
        icon.setObjectName("SidebarItemIcon")
        icon.setFixedSize(Theme.icon_size, Theme.icon_size)
        icon.setPixmap(QIcon(str(resources.icon_path(icon_name))).pixmap(QSize(Theme.icon_size, Theme.icon_size)))
        layout.addWidget(icon)

        self._label = QLabel(label)
        self._label.setObjectName("SidebarItemLabel")
        self._label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._label)
        layout.addStretch(1)

    def set_label(self, label: str) -> None:
        self._label.setText(label)

    def set_navigation_key(self, key: str) -> None:
        self.navigation_key = key

    def set_checked(self, checked: bool) -> None:
        self.setProperty("selected", "true" if checked else "false")
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
                self._finish_click_interaction()
            return
        self._pressed_inside = False
        super().mouseReleaseEvent(event)

    def _finish_click_interaction(self) -> None:
        # Match the chrome action button behavior: opening an overlay can steal
        # the leave event that normally clears a sidebar item's hover paint.
        app = QApplication.instance()
        if app is not None:
            app.sendEvent(self, QEvent(QEvent.Type.Leave))
        self.update()
