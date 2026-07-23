"""Main content wrapper for the reusable JoyRead settings page."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtGui import QKeyEvent, QKeySequence, QMouseEvent, QResizeEvent, QShortcut, QShowEvent
from PySide6.QtWidgets import QWidget

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.settings_viewmodel import SettingsViewModel
from joyread.ui.viewmodels.tag_management_viewmodel import TagManagementViewModel
from joyread.ui.widgets.settings_page import SettingsPageWidget


logger = logging.getLogger(__name__)


class SettingsView(QWidget):
    close_requested = QtSignal()
    info_requested = QtSignal(str, str)
    storage_move_requested = QtSignal()
    storage_select_requested = QtSignal()
    storage_reset_requested = QtSignal()
    library_maintenance_requested = QtSignal()
    hidden_space_setup_requested = QtSignal()
    hidden_space_verify_requested = QtSignal()
    hidden_space_change_password_requested = QtSignal()
    hidden_space_revert_requested = QtSignal()
    hidden_space_reset_requested = QtSignal()
    tag_operation_completed = QtSignal(bool, str, str)
    tag_delete_requested = QtSignal(str, str)

    def __init__(
        self,
        viewmodel: SettingsViewModel,
        resources: ResourceLoader,
        parent: QWidget | None = None,
        *,
        tag_viewmodel: TagManagementViewModel | None = None,
    ) -> None:
        super().__init__(parent)
        logger.info("SettingsView init")
        self.setObjectName("SettingsOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.page = SettingsPageWidget(viewmodel, resources, self, tag_viewmodel=tag_viewmodel)
        self.page.storage_move_requested.connect(self.storage_move_requested.emit)
        self.page.storage_select_requested.connect(self.storage_select_requested.emit)
        self.page.storage_reset_requested.connect(self.storage_reset_requested.emit)
        self.page.hidden_space_setup_requested.connect(self.hidden_space_setup_requested.emit)
        self.page.hidden_space_verify_requested.connect(self.hidden_space_verify_requested.emit)
        self.page.hidden_space_change_password_requested.connect(
            self.hidden_space_change_password_requested.emit
        )
        self.page.hidden_space_revert_requested.connect(self.hidden_space_revert_requested.emit)
        self.page.hidden_space_reset_requested.connect(self.hidden_space_reset_requested.emit)
        self.page.tag_operation_completed.connect(self.tag_operation_completed.emit)
        self.page.tag_delete_requested.connect(self.tag_delete_requested.emit)
        viewmodel.library_maintenance_requested.connect(self.library_maintenance_requested.emit)

        self._escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._escape_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._escape_shortcut.activated.connect(self.close_requested.emit)

    def set_sidebar_visible(self, visible: bool) -> None:
        self.setProperty("sidebarVisible", "true" if visible else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def refresh_labels(self) -> None:
        """Refresh translated labels in the settings overlay."""
        self.page.refresh_labels()

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
