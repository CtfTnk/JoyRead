"""Left navigation sidebar aligned to the Figma sidebar frame."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QContextMenuEvent, QIcon, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from joyread.core.models.collection import Collection
from joyread.infrastructure.i18n.locale_service import t
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.shelf_viewmodel import ShelfKey, collection_shelf_key
from joyread.ui.widgets.auto_hide_scrollbar import AutoHideScrollHandle
from joyread.ui.widgets.section_banner import SectionBanner, SidebarSectionBanner


class SidebarWidget(QWidget):
    navigation_requested = QtSignal(str)
    collection_menu_requested = QtSignal(str, QPoint)

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._resources = resources
        self.setObjectName("Sidebar")
        self.setFixedWidth(Theme.sidebar_width)
        self._buttons: dict[str, SidebarItemWidget] = {}
        # ``_hidden_item`` is built up-front but visibility is gated on the
        # "Show Hidden Collection" setting toggled from the Privacy page.
        self._hidden_item: SidebarItemWidget | None = None
        # Fixed-label items that need refreshing on language change.
        self._fixed_labels: list[tuple[SidebarItemWidget, str]] = []  # (widget, locale_key)
        self._book_shelf_banner: SidebarSectionBanner | None = None
        self._collections_banner: SidebarSectionBanner | None = None

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(
            Theme.sidebar_margin_horizontal,
            Theme.sidebar_margin_vertical,
            Theme.sidebar_margin_horizontal,
            Theme.sidebar_margin_vertical,
        )
        root_layout.setSpacing(0)

        scroll_area = QScrollArea()
        scroll_area.setObjectName("SidebarScrollArea")
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll_area.viewport().setObjectName("SidebarScrollViewport")
        scroll_area.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._scroll_handle = AutoHideScrollHandle(scroll_area, parent=self)

        upper_part = QWidget()
        upper_part.setObjectName("SidebarUpperPart")
        upper_layout = QVBoxLayout(upper_part)
        upper_layout.setContentsMargins(0, 0, 0, 0)
        upper_layout.setSpacing(Theme.sidebar_gap)

        upper_layout.addWidget(self._build_book_shelf_section())
        self._collections_section = QWidget()
        self._collections_section.setObjectName("SidebarSectionGroup")
        collections_layout = QVBoxLayout(self._collections_section)
        collections_layout.setContentsMargins(0, 0, 0, 0)
        collections_layout.setSpacing(Theme.sidebar_gap)
        self._collections_banner = SidebarSectionBanner(t("sidebar.collections"), self._resources)
        collections_layout.addWidget(self._collections_banner)
        new_collection_item = self._item(t("sidebar.new_collection"), "new_collection", "icon_add.svg")
        self._fixed_labels.append((new_collection_item, "sidebar.new_collection"))
        collections_layout.addWidget(new_collection_item)

        self._collections_container = QWidget()
        self._collections_container.setObjectName("SidebarCollectionsContainer")
        self._collections_layout = QVBoxLayout(self._collections_container)
        self._collections_layout.setContentsMargins(0, 0, 0, 0)
        self._collections_layout.setSpacing(Theme.sidebar_gap)
        collections_layout.addWidget(self._collections_container)
        upper_layout.addWidget(self._collections_section)
        upper_layout.addStretch(1)

        scroll_area.setWidget(upper_part)
        root_layout.addWidget(scroll_area, stretch=1)

        lower_part = QWidget()
        lower_part.setObjectName("SidebarLowerPart")
        lower_layout = QVBoxLayout(lower_part)
        lower_layout.setContentsMargins(0, 0, 0, Theme.sidebar_lower_padding_bottom)
        lower_layout.setSpacing(0)
        settings_item = self._item(t("sidebar.settings"), "settings", "icon_setting.svg")
        self._fixed_labels.append((settings_item, "sidebar.settings"))
        lower_layout.addWidget(settings_item)
        root_layout.addWidget(lower_part)

    def set_collections(self, collections: list[Collection]) -> None:
        for key in tuple(self._buttons):
            if key.startswith("collection:"):
                self._buttons.pop(key)
        while self._collections_layout.count():
            item = self._collections_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        for collection in collections:
            key = collection_shelf_key(collection.uuid)
            # Hidable collections get the strike-through eye icon so users
            # can tell at a glance which collections belong to Hidden Space
            # while the toggle is on.
            icon = "icon_collection_hiden.svg" if collection.is_hidable else "icon_collection.svg"
            button = self._item(
                collection.name,
                key,
                icon,
                allow_context_menu=True,
            )
            self._collections_layout.addWidget(button)

    def set_hidden_visible(self, visible: bool) -> None:
        if self._hidden_item is not None:
            self._hidden_item.setVisible(visible)

    def refresh_labels(self) -> None:
        """Re-apply translated labels to all fixed sidebar items (called on language change)."""
        if self._book_shelf_banner is not None:
            self._book_shelf_banner.set_title(t("sidebar.book_shelf"))
        if self._collections_banner is not None:
            self._collections_banner.set_title(t("sidebar.collections"))
        for widget, key in self._fixed_labels:
            widget.set_label(t(key))

    def set_active(self, key: str) -> None:
        for item_key, button in self._buttons.items():
            button.set_checked(item_key == key)

    def _build_book_shelf_section(self) -> QWidget:
        # The fixed Book Shelf section, plus a Hidden row that the Privacy
        # toggle reveals. Built inline (rather than via ``_section``) so the
        # Hidden row reference survives for later visibility toggling.
        section = QWidget()
        section.setObjectName("SidebarSectionGroup")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Theme.sidebar_gap)

        self._book_shelf_banner = SidebarSectionBanner(t("sidebar.book_shelf"), self._resources)
        layout.addWidget(self._book_shelf_banner)
        all_item = self._item(t("sidebar.all"), ShelfKey.ALL.value, "icon_book_all.svg", checked=True)
        self._fixed_labels.append((all_item, "sidebar.all"))
        layout.addWidget(all_item)
        recent_item = self._item(t("sidebar.recent"), ShelfKey.RECENT.value, "icon_recent.svg")
        self._fixed_labels.append((recent_item, "sidebar.recent"))
        layout.addWidget(recent_item)
        favourites_item = self._item(t("sidebar.favourites"), ShelfKey.FAVOURITES.value, "icon_favourite_enabled.svg")
        self._fixed_labels.append((favourites_item, "sidebar.favourites"))
        layout.addWidget(favourites_item)
        self._hidden_item = self._item(t("sidebar.hidden"), ShelfKey.HIDDEN.value, "icon_hidden_collection.svg")
        self._fixed_labels.append((self._hidden_item, "sidebar.hidden"))
        self._hidden_item.setVisible(False)
        layout.addWidget(self._hidden_item)
        return section

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

    def _item(
        self,
        label: str,
        key: str,
        icon_name: str,
        checked: bool = False,
        allow_context_menu: bool = False,
    ) -> "SidebarItemWidget":
        button = SidebarItemWidget(
            label,
            icon_name,
            self._resources,
            navigation_key=key,
            checked=checked,
            allow_context_menu=allow_context_menu,
        )
        button.clicked.connect(lambda item=button: self.navigation_requested.emit(item.navigation_key))
        button.menu_requested.connect(lambda item_key, pos: self.collection_menu_requested.emit(item_key, pos))
        self._buttons[key] = button
        return button


__all__ = ["SidebarItemWidget", "SidebarSectionBanner", "SidebarWidget", "SectionBanner"]


class SidebarItemWidget(QFrame):
    """Figma sidebar item with fixed padding and a 5px icon/text label group gap."""

    clicked = QtSignal()
    menu_requested = QtSignal(str, QPoint)

    def __init__(
        self,
        label: str,
        icon_name: str,
        resources: ResourceLoader,
        *,
        navigation_key: str,
        checked: bool = False,
        allow_context_menu: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.navigation_key = navigation_key
        self._allow_context_menu = allow_context_menu
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

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        if self._allow_context_menu:
            self.menu_requested.emit(self.navigation_key, event.globalPos())
            event.accept()
            return
        super().contextMenuEvent(event)

    def _finish_click_interaction(self) -> None:
        # Match the chrome action button behavior: opening an overlay can steal
        # the leave event that normally clears a sidebar item's hover paint.
        app = QApplication.instance()
        if app is not None:
            app.sendEvent(self, QEvent(QEvent.Type.Leave))
        self.update()
