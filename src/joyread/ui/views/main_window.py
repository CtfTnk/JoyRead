"""Main JoyRead window."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QHBoxLayout, QMainWindow, QSizeGrip, QStackedWidget, QVBoxLayout, QWidget

from joyread.app.app_context import AppContext
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.shelf_viewmodel import ShelfKey
from joyread.ui.views.settings_view import SettingsView
from joyread.ui.views.shelf_view import ShelfView
from joyread.ui.widgets.dialogs import JoyReadDialogOverlay
from joyread.ui.widgets.sidebar import SidebarWidget
from joyread.ui.widgets.window_chrome import WindowChromeWidget


class MainWindow(QMainWindow):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._context = context
        self.setObjectName("MainWindow")
        self.setWindowTitle("JoyRead")
        self.setWindowIcon(QIcon(str(context.resources.app_icon_path())))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(Theme.window_width, Theme.window_height)
        self.setMinimumSize(Theme.window_min_width, Theme.window_min_height)

        root = QWidget()
        root.setObjectName("RootPanel")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.chrome = WindowChromeWidget(context.resources)
        root_layout.addWidget(self.chrome)

        view_panel = QWidget()
        view_panel.setObjectName("ViewPanel")
        layout = QHBoxLayout(view_panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.sidebar = SidebarWidget(context.resources)
        self.shelf_view = ShelfView(context.shelf_viewmodel, context.resources)
        self.content_stack = QStackedWidget()
        self.content_stack.setObjectName("MainContentStack")
        self.content_stack.addWidget(self.shelf_view)
        layout.addWidget(self.sidebar)
        layout.addWidget(self.content_stack, stretch=1)
        root_layout.addWidget(view_panel, stretch=1)

        self.settings_view = SettingsView(context.settings_viewmodel, context.resources, root)
        self.settings_view.close_requested.connect(self._hide_settings_page)
        self.settings_view.hide()

        self._resize_grip = QSizeGrip(root)
        self._resize_grip.setFixedSize(Theme.resize_grip_size, Theme.resize_grip_size)
        self._resize_grip.setObjectName("ResizeGrip")
        self._resize_grip.raise_()

        self.dialog_overlay = JoyReadDialogOverlay(root)
        self.dialog_overlay.hide()
        self.setCentralWidget(root)
        self._position_dialog_overlay()

        self.chrome.set_action_menu(self.shelf_view.create_action_menu())
        self.chrome.sidebar_toggle_requested.connect(self._toggle_sidebar)
        self.chrome.view_mode_changed.connect(context.shelf_viewmodel.set_view_mode)
        self.chrome.sort_changed.connect(context.shelf_viewmodel.set_sort)
        self.sidebar.navigation_requested.connect(self._handle_navigation)
        self.shelf_view.info_requested.connect(self.dialog_overlay.show_info)
        self.settings_view.info_requested.connect(self.dialog_overlay.show_info)
        context.shelf_viewmodel.state_changed.connect(self._sync_sidebar)
        context.shelf_viewmodel.state_changed.connect(self._sync_chrome)
        context.shelf_viewmodel.load_books()
        self.sidebar.set_collections(context.shelf_viewmodel.collections)
        self.shelf_view.render()

    def _handle_navigation(self, key: str) -> None:
        if key == "new_collection":
            self.dialog_overlay.show_info("New Collection", "Collection creation is not implemented yet.")
            return
        if key == "settings":
            self._show_settings_page()
            return
        self._hide_settings_page()
        self._context.shelf_viewmodel.set_current_shelf(key)

    def _sync_sidebar(self) -> None:
        if self.settings_view.isVisible():
            self.sidebar.set_active("settings")
            return
        current = self._context.shelf_viewmodel.current_shelf
        if current in {ShelfKey.ALL.value, ShelfKey.RECENT.value, ShelfKey.FAVOURITES.value} or current.startswith(
            "collection:"
        ):
            self.sidebar.set_active(current)

    def _sync_chrome(self) -> None:
        self.chrome.set_view_mode(self._context.shelf_viewmodel.view_mode.value)

    def _toggle_sidebar(self) -> None:
        visible = not self.sidebar.isVisible()
        self.sidebar.setVisible(visible)
        self.shelf_view.set_sidebar_visible(visible)
        self.settings_view.set_sidebar_visible(visible)
        self.chrome.set_sidebar_visible(visible)

    def _show_settings_page(self) -> None:
        self._context.shelf_viewmodel.clear_selection(emit_state=False)
        self._context.shelf_viewmodel.hide_detail()
        self._position_settings_overlay()
        self.settings_view.show()
        self.settings_view.raise_()
        self.settings_view.setFocus(Qt.FocusReason.PopupFocusReason)
        self._raise_dialog_overlay_if_visible()
        self.sidebar.set_active("settings")

    def _hide_settings_page(self) -> None:
        if self.settings_view.isHidden():
            return
        self.settings_view.hide()
        self._sync_sidebar()
        if hasattr(self, "_resize_grip"):
            self._resize_grip.raise_()
        self._raise_dialog_overlay_if_visible()

    def _position_settings_overlay(self) -> None:
        if not hasattr(self, "settings_view"):
            return
        root = self.centralWidget()
        if root is None:
            return
        self.settings_view.setGeometry(0, 0, root.width(), root.height())

    def _position_dialog_overlay(self) -> None:
        if not hasattr(self, "dialog_overlay"):
            return
        root = self.centralWidget()
        if root is None:
            return
        self.dialog_overlay.setGeometry(0, 0, root.width(), root.height())
        self._raise_dialog_overlay_if_visible()

    def _raise_dialog_overlay_if_visible(self) -> None:
        if hasattr(self, "dialog_overlay") and self.dialog_overlay.isVisible():
            self.dialog_overlay.raise_()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._position_settings_overlay()
        self._position_dialog_overlay()
        if hasattr(self, "_resize_grip"):
            margin = 2
            self._resize_grip.move(
                self.centralWidget().width() - self._resize_grip.width() - margin,
                self.centralWidget().height() - self._resize_grip.height() - margin,
            )
