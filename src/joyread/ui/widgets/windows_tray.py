"""Windows notification-area controls for the resident JoyRead process."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal as QtSignal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from joyread.infrastructure.i18n.locale_service import t
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.localized_text import set_localized


_CLEANUP_TEXT = {
    "ready": "windows_background.clear_memory",
    "wait_windows": "windows_background.clear_wait_windows",
    "wait_tasks": "windows_background.clear_wait_tasks",
    "cleaning": "windows_background.clear_running",
    "done": "windows_background.clear_done",
    "failed": "windows_background.clear_failed",
}


class WindowsTrayIcon(QSystemTrayIcon):
    """Present commands; application code decides what each command does."""

    restore_requested = QtSignal()
    open_library_requested = QtSignal()
    open_book_requested = QtSignal()
    cleanup_requested = QtSignal()
    memory_refresh_requested = QtSignal()
    quit_requested = QtSignal()

    def __init__(self, icon: QIcon, parent: QObject) -> None:
        super().__init__(icon, parent)
        self.setObjectName("JoyReadWindowsTrayIcon")
        set_localized(self, "setToolTip", t("app.name"))
        # QSystemTrayIcon does not take ownership of its context menu.
        self._menu = QMenu()
        self._menu.setObjectName("JoyReadWindowsTrayMenu")
        self._menu_text_gutter: int | None = None
        self.open_library_action = self._add_action(
            "windows_background.open_library", self.open_library_requested
        )
        self.open_book_action = self._add_action(
            "windows_background.open_book", self.open_book_requested
        )
        self.cleanup_action = self._add_action(
            "windows_background.clear_memory", self.cleanup_requested
        )
        self.memory_action = QAction(self._menu)
        self.memory_action.setEnabled(False)
        font = self._menu.font()
        font.setPixelSize(Theme.menu_font_size - 1)
        self.memory_action.setFont(font)
        self._menu.addAction(self.memory_action)
        self.set_memory_usage_mb(None)
        self._menu.addSeparator()
        self.quit_action = self._add_action(
            "windows_background.quit", self.quit_requested
        )
        self.setContextMenu(self._menu)
        self._menu.aboutToShow.connect(self.memory_refresh_requested.emit)
        self._menu.aboutToShow.connect(self._reserve_text_padding)
        self.activated.connect(self._handle_activation)

    def _add_action(self, text_key: str, requested: QtSignal) -> QAction:
        action = QAction(self._menu)
        set_localized(action, "setText", t(text_key))
        action.triggered.connect(lambda: requested.emit())
        self._menu.addAction(action)
        return action

    def set_cleanup_status(self, status: str) -> None:
        set_localized(self.cleanup_action, "setText", t(_CLEANUP_TEXT[status]))
        self.cleanup_action.setEnabled(status == "ready")

    def set_memory_usage_mb(self, mb: int | None) -> None:
        key = "windows_background.memory_usage" if mb is not None else "windows_background.memory_unavailable"
        set_localized(self.memory_action, "setText", t(key, mb=mb))

    def _reserve_text_padding(self) -> None:
        # Widen the action rows, not QMenu's outer contents margin: an outer
        # margin leaves an unpainted strip when an item is highlighted. Qt
        # retains a previous minimum in sizeHint(), so calibrate the native
        # style's text gutter once and calculate from current labels thereafter.
        metrics = self._menu.fontMetrics()
        text_width = max(
            (
                metrics.horizontalAdvance(action.text())
                for action in self._menu.actions()
                if not action.isSeparator()
            ),
            default=0,
        )
        if self._menu_text_gutter is None:
            self._menu_text_gutter = max(0, self._menu.sizeHint().width() - text_width)
        self._menu.setMinimumWidth(
            text_width + self._menu_text_gutter + Theme.tray_menu_item_right_padding
        )

    def _handle_activation(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.restore_requested.emit()

    def dispose(self) -> None:
        self.hide()
        self.setContextMenu(None)
        self._menu.deleteLater()
