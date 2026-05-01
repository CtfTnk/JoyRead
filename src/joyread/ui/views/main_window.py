"""Main JoyRead window."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QMainWindow, QMessageBox, QSizeGrip, QVBoxLayout, QWidget

from joyread.app.app_context import AppContext
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.shelf_viewmodel import ShelfKey
from joyread.ui.views.shelf_view import ShelfView
from joyread.ui.widgets.sidebar import SidebarWidget
from joyread.ui.widgets.window_chrome import WindowChromeWidget


class MainWindow(QMainWindow):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._context = context
        self.setObjectName("MainWindow")
        self.setWindowTitle("JoyRead")
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
        layout.addWidget(self.sidebar)
        layout.addWidget(self.shelf_view, stretch=1)
        root_layout.addWidget(view_panel, stretch=1)

        self._resize_grip = QSizeGrip(root)
        self._resize_grip.setFixedSize(Theme.resize_grip_size, Theme.resize_grip_size)
        self._resize_grip.setObjectName("ResizeGrip")
        self._resize_grip.raise_()
        self.setCentralWidget(root)

        self.chrome.set_action_menu(self.shelf_view.create_action_menu())
        self.chrome.sidebar_toggle_requested.connect(self._toggle_sidebar)
        self.chrome.view_mode_changed.connect(context.shelf_viewmodel.set_view_mode)
        self.chrome.sort_changed.connect(context.shelf_viewmodel.set_sort)
        self.sidebar.navigation_requested.connect(self._handle_navigation)
        context.shelf_viewmodel.state_changed.connect(self._sync_sidebar)
        context.shelf_viewmodel.state_changed.connect(self._sync_chrome)
        context.shelf_viewmodel.load_books()
        self.sidebar.set_collections(context.shelf_viewmodel.collections)
        self.shelf_view.render()

    def _handle_navigation(self, key: str) -> None:
        if key == "new_collection":
            QMessageBox.information(self, "New Collection", "Collection creation is not implemented yet.")
            return
        if key == "settings":
            QMessageBox.information(self, "Settings", "Settings page is reserved for a future phase.")
            return
        self._context.shelf_viewmodel.set_current_shelf(key)

    def _sync_sidebar(self) -> None:
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
        self.chrome.set_sidebar_visible(visible)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if hasattr(self, "_resize_grip"):
            margin = 2
            self._resize_grip.move(
                self.centralWidget().width() - self._resize_grip.width() - margin,
                self.centralWidget().height() - self._resize_grip.height() - margin,
            )
