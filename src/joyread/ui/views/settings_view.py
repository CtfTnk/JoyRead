"""Main content wrapper for the reusable JoyRead settings page."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtGui import QKeyEvent, QKeySequence, QMouseEvent, QResizeEvent, QShortcut, QShowEvent
from PySide6.QtWidgets import QWidget

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.settings_viewmodel import SettingsViewModel
from joyread.ui.widgets.settings_page import SettingsPageWidget


class SettingsView(QWidget):
    close_requested = QtSignal()
    info_requested = QtSignal(str, str)

    def __init__(
        self,
        viewmodel: SettingsViewModel,
        resources: ResourceLoader,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SettingsOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.page = SettingsPageWidget(viewmodel, resources, self)
        self.page.storage_change_requested.connect(self._show_storage_placeholder)

        self._escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._escape_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._escape_shortcut.activated.connect(self.close_requested.emit)

    def set_sidebar_visible(self, visible: bool) -> None:
        self.setProperty("sidebarVisible", "true" if visible else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self.page.geometry().contains(event.position().toPoint()):
            self.close_requested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_page()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._position_page()

    def _show_storage_placeholder(self) -> None:
        self.info_requested.emit("Storage Location", "Storage location selection is not implemented yet.")

    def _position_page(self) -> None:
        width = _clamp(
            round(self.width() * (Theme.settings_panel_width / Theme.window_width)),
            Theme.settings_panel_min_width,
            Theme.settings_panel_max_width,
        )
        height = _clamp(
            round(self.height() * (Theme.settings_panel_height / Theme.window_height)),
            Theme.settings_panel_min_height,
            Theme.settings_panel_max_height,
        )
        x = (self.width() - width) // 2
        y = (self.height() - height) // 2
        self.page.setGeometry(x, y, width, height)


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return min(max(value, minimum), maximum)
