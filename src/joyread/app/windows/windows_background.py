"""Windows-only resident lifetime, tray routing, and idle memory cleanup."""

from __future__ import annotations

from collections.abc import Callable
import logging

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMainWindow, QSystemTrayIcon

from joyread.app.windows.manager import ApplicationWindowManager
from joyread.core.file_types import SUPPORTED_READER_EXTENSIONS
from joyread.infrastructure.config.settings_store import SettingsStore
from joyread.infrastructure.i18n.locale_service import t
from joyread.infrastructure.windows_process_memory import current_process_working_set_mb
from joyread.ui.dialogs.windows_background_notice import (
    EXIT_APPLICATION,
    WindowsBackgroundNoticeDialog,
)
from joyread.ui.widgets.windows_tray import WindowsTrayIcon


logger = logging.getLogger(__name__)
TASK_POLL_MS = 5_000
RESULT_LABEL_MS = 3_000


class WindowsBackgroundSession(QObject):
    """Keep the primary process available while every managed window is closed."""

    def __init__(
        self,
        app: QApplication,
        windows: ApplicationWindowManager,
        settings_store: SettingsStore,
        context_provider: Callable[[], object],
    ) -> None:
        super().__init__(app)
        self._app = app
        self._windows = windows
        self._store = settings_store
        self._context_provider = context_provider
        settings = settings_store.load()
        self._enabled = settings.windows_background_enabled
        self._notice_suppressed = settings.windows_background_notice_suppressed
        self._auto_cleanup_enabled = settings.windows_background_auto_cleanup_enabled
        self._auto_cleanup_seconds = settings.windows_background_auto_cleanup_seconds
        self._previous_quit_on_close = app.quitOnLastWindowClosed()
        self._tray: WindowsTrayIcon | None = None
        self._bound_viewmodel: object | None = None
        self._exiting = False
        self._disposed = False
        self._picker_open = False
        self._manual_pending = False
        self._auto_due = False
        self._cleaning = False

        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._on_idle_timeout)
        self._task_poll = QTimer(self)
        self._task_poll.setSingleShot(True)
        self._task_poll.timeout.connect(self._try_cleanup)
        self._result_timer = QTimer(self)
        self._result_timer.setSingleShot(True)
        self._result_timer.timeout.connect(lambda: self._set_cleanup_status("ready"))

        windows.set_last_close_guard(self._allow_last_close)
        windows.windows_changed.connect(self._on_windows_changed)
        app.aboutToQuit.connect(self.dispose)
        self._bind_settings_viewmodel()
        if self._enabled:
            self._ensure_tray()
        if not windows.has_windows:
            self._on_windows_changed()

    @property
    def tray(self) -> WindowsTrayIcon | None:
        return self._tray

    def _ensure_tray(self) -> bool:
        if not self._enabled or not QSystemTrayIcon.isSystemTrayAvailable():
            self._deactivate_tray()
            return False
        icon = self._app.windowIcon()
        if icon.isNull():
            logger.warning("Windows background unavailable: application icon is missing")
            self._deactivate_tray()
            return False
        if self._tray is None:
            tray = WindowsTrayIcon(icon, self._app)
            tray.restore_requested.connect(self._windows.handle_reopen)
            tray.open_library_requested.connect(self._windows.show_library)
            tray.open_book_requested.connect(self._open_book)
            tray.cleanup_requested.connect(self.request_cleanup)
            tray.memory_refresh_requested.connect(self._refresh_memory_usage)
            tray.quit_requested.connect(self.request_quit)
            self._tray = tray
        self._tray.show()
        self._app.setQuitOnLastWindowClosed(False)
        return True

    def _deactivate_tray(self) -> None:
        if self._tray is not None:
            self._tray.hide()
        self._idle_timer.stop()
        self._task_poll.stop()
        self._result_timer.stop()
        self._manual_pending = False
        self._auto_due = False
        self._cleaning = False
        self._app.setQuitOnLastWindowClosed(self._previous_quit_on_close)

    def _bind_settings_viewmodel(self) -> None:
        context = self._context_provider()
        viewmodel = getattr(context, "settings_viewmodel", None)
        if viewmodel is None or viewmodel is self._bound_viewmodel:
            return
        if self._bound_viewmodel is not None:
            self._bound_viewmodel.windows_background_changed.disconnect(
                self._on_enabled_changed
            )
            self._bound_viewmodel.windows_background_cleanup_changed.disconnect(
                self._on_cleanup_changed
            )
        viewmodel.windows_background_changed.connect(self._on_enabled_changed)
        viewmodel.windows_background_cleanup_changed.connect(self._on_cleanup_changed)
        self._bound_viewmodel = viewmodel

    def _on_enabled_changed(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if self._enabled:
            self._notice_suppressed = False
            if self._ensure_tray() and not self._windows.has_windows:
                self._schedule_idle_clear()
            return
        self._deactivate_tray()
        if not self._windows.has_windows:
            self._app.quit()

    def _on_cleanup_changed(self, config: tuple[bool, int]) -> None:
        self._auto_cleanup_enabled, self._auto_cleanup_seconds = config
        # Reconfigure the one-shot wait from now. An already queued automatic
        # clear is withdrawn; a user-requested manual clear keeps its place.
        self._idle_timer.stop()
        self._auto_due = False
        if not self._manual_pending:
            self._task_poll.stop()
            self._cleaning = False
        if not self._windows.has_windows:
            self._schedule_idle_clear()

    def _allow_last_close(self, window: QMainWindow) -> bool:
        if self._exiting or not self._enabled or not self._ensure_tray():
            return True
        if self._notice_suppressed:
            return True
        dialog = WindowsBackgroundNoticeDialog(window)
        result = dialog.exec()
        suppress = dialog.dont_show_again.isChecked()
        dialog.deleteLater()
        if result == EXIT_APPLICATION:
            self._begin_quit()
            # Let the guarded close finish first. Closing its windows from
            # inside the nested dialog's exec() would re-enter Close handling.
            QTimer.singleShot(0, self._finish_quit)
            return True
        if result != QDialog.DialogCode.Accepted:
            return False
        if suppress:
            try:
                self._store.update(windows_background_notice_suppressed=True)
            except Exception:
                logger.exception("Could not save Windows background notice preference")
            else:
                self._notice_suppressed = True
        return True

    def _on_windows_changed(self) -> None:
        if self._disposed or self._exiting:
            return
        self._bind_settings_viewmodel()
        if self._windows.has_windows:
            self._idle_timer.stop()
            self._task_poll.stop()
            self._auto_due = False
            if self._manual_pending:
                self._set_cleanup_status("wait_windows")
            return
        if self._manual_pending:
            QTimer.singleShot(0, self._try_cleanup)
        elif self._enabled and self._tray is not None and self._tray.isVisible():
            self._schedule_idle_clear()

    def _schedule_idle_clear(self) -> None:
        if (
            self._enabled
            and self._auto_cleanup_enabled
            and self._tray is not None
            and self._tray.isVisible()
            and not self._windows.has_windows
            and not self._manual_pending
            and not self._cleaning
            and not self._idle_timer.isActive()
        ):
            self._idle_timer.start(self._auto_cleanup_seconds * 1000)

    def _on_idle_timeout(self) -> None:
        if not self._auto_cleanup_enabled:
            return
        self._auto_due = True
        self._try_cleanup()

    def request_cleanup(self) -> None:
        if self._manual_pending or self._cleaning or not self._enabled:
            return
        self._manual_pending = True
        self._idle_timer.stop()
        self._result_timer.stop()
        self._try_cleanup()

    def _try_cleanup(self) -> None:
        if not self._auto_cleanup_enabled:
            self._auto_due = False
        if self._disposed or self._cleaning or not (self._manual_pending or self._auto_due):
            return
        if self._windows.has_windows or self._picker_open:
            if self._manual_pending:
                self._set_cleanup_status("wait_windows")
            return
        reader = self._reader_runtime()
        if reader.task_service.pending_task_count() > 0:
            if self._manual_pending:
                self._set_cleanup_status("wait_tasks")
            self._task_poll.start(TASK_POLL_MS)
            return
        self._task_poll.stop()
        self._cleaning = True
        if self._manual_pending:
            self._set_cleanup_status("cleaning")
        QTimer.singleShot(0, self._perform_cleanup)

    def _perform_cleanup(self) -> None:
        if self._disposed or not self._cleaning:
            return
        if not (self._manual_pending or self._auto_due):
            self._cleaning = False
            return
        if self._windows.has_windows or self._picker_open:
            self._cleaning = False
            self._try_cleanup()
            return
        if self._reader_runtime().task_service.pending_task_count() > 0:
            self._cleaning = False
            self._try_cleanup()
            return
        try:
            reader = self._reader_runtime()
            reader.cache_service.reader_page_cache.clear()
            reader.cache_service.thumbnail_cache.clear()
            context = self._context_provider()
            if getattr(context, "library_runtime", None) is not None:
                context.cache_service.cover_index.clear()
                context.library_runtime.thumbnail_service.clear_idle_memory()
        except Exception:
            logger.exception("Windows idle memory cleanup failed")
            status = "failed"
        else:
            logger.info("Windows idle memory caches cleared")
            status = "done"
        self._cleaning = False
        manual = self._manual_pending
        self._manual_pending = False
        self._auto_due = False
        if manual:
            self._set_cleanup_status(status)
            self._result_timer.start(RESULT_LABEL_MS)
        self._refresh_memory_usage()

    def _reader_runtime(self):  # noqa: ANN201
        context = self._context_provider()
        return getattr(context, "reader_runtime", context)

    def _set_cleanup_status(self, status: str) -> None:
        if self._tray is not None:
            self._tray.set_cleanup_status(status)

    def _refresh_memory_usage(self) -> None:
        if self._tray is not None:
            self._tray.set_memory_usage_mb(current_process_working_set_mb())

    def _open_book(self) -> None:
        self._picker_open = True
        self._idle_timer.stop()
        readable_suffixes = sorted(SUPPORTED_READER_EXTENSIONS)
        extensions = " ".join(f"*{suffix}" for suffix in readable_suffixes)
        try:
            paths, _selected_filter = QFileDialog.getOpenFileNames(
                None,
                t("dialog.file_open_book_title"),
                "",
                t("dialog.file_filter_readable_books", extensions=extensions),
            )
        except Exception:
            logger.exception("Windows tray file picker failed")
            paths = []
        finally:
            self._picker_open = False
        if paths:
            self._windows.open_files(paths)
        elif not self._windows.has_windows and self._enabled:
            self._schedule_idle_clear()
        if self._manual_pending:
            self._try_cleanup()

    def request_quit(self) -> None:
        if self._exiting:
            return
        self._begin_quit()
        self._finish_quit()

    def _begin_quit(self) -> None:
        self._exiting = True
        self._idle_timer.stop()
        self._task_poll.stop()
        self._result_timer.stop()

    def _finish_quit(self) -> None:
        self._windows.close_all_windows()
        if self._windows.has_windows:
            self._exiting = False
            return
        self._app.quit()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._idle_timer.stop()
        self._task_poll.stop()
        self._result_timer.stop()
        self._windows.set_last_close_guard(None)
        self._windows.windows_changed.disconnect(self._on_windows_changed)
        if self._bound_viewmodel is not None:
            self._bound_viewmodel.windows_background_changed.disconnect(
                self._on_enabled_changed
            )
            self._bound_viewmodel.windows_background_cleanup_changed.disconnect(
                self._on_cleanup_changed
            )
        if self._tray is not None:
            self._tray.dispose()
        self._app.setQuitOnLastWindowClosed(self._previous_quit_on_close)


def install_windows_background(
    app: QApplication,
    windows: ApplicationWindowManager,
    settings_store: SettingsStore,
    context_provider: Callable[[], object],
) -> WindowsBackgroundSession:
    session = WindowsBackgroundSession(app, windows, settings_store, context_provider)
    setattr(app, "_joyread_windows_background", session)
    return session
