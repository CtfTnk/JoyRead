"""Settings > About > Diagnostics, and the offer after a crash.

The user can open the log folder and read the per-launch logs themselves, or
save a crash report to the Desktop. A crash report holds only the launches
that ended without a clean quit, so it exists only when JoyRead has actually
seen one: Settings says so when there is none, and a launch that finds a new
crash offers the report once, as soon as the library window is free to show
a dialog.

Every way of saving says first what the report contains -- real file paths
and book names, including books that were hidden later -- because that is
what the user weighs before sending it. There is deliberately no "hide paths"
variant: a crash log that cannot name the failing file does not help.

File work runs on the process-wide task service, not the Library's scope: the
logs belong to the application support root and must stay reachable while the
Library is loading, moving or unavailable.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QWidget

from joyread.app.tasking import TaskExecutor
from joyread.infrastructure.i18n.locale_service import t
from joyread.infrastructure.logging.log_files import (
    CrashReportResult,
    LogClearResult,
    LogFilesUnavailable,
    NoCrashRecorded,
    clear_logs,
    desktop_directory,
    recorded_crashes,
    save_crash_report,
)
from joyread.infrastructure.logging.run_files import KEPT_RUN_COUNT
from joyread.ui.viewmodels.settings_viewmodel import SettingsViewModel
from joyread.ui.widgets.dialogs import JoyReadDialogOverlay


logger = logging.getLogger(__name__)

#: How long to wait before trying again when another dialog is open.
_OFFER_RETRY_MS = 1000


class DiagnosticsFlows(QObject):
    def __init__(
        self,
        dialogs: JoyReadDialogOverlay,
        viewmodel: SettingsViewModel,
        task_service: TaskExecutor,
        host: QWidget,
    ) -> None:
        super().__init__(host)
        self._dialogs = dialogs
        self._vm = viewmodel
        self._tasks = task_service
        self._host = host

    def open_log_folder(self) -> None:
        directory = self._vm.log_directory()
        if directory is None:
            self._show_unavailable()
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
            logger.warning("Log folder could not be opened path=%s", directory)
            self._dialogs.show_info(
                t("diagnostics.banner"),
                t("diagnostics.open_failed", path=str(directory)),
            )

    def start_crash_report(self) -> None:
        if self._vm.log_directory() is None:
            self._show_unavailable()
            return
        if not recorded_crashes():
            self._show_no_crash()
            return
        self._dialogs.show_confirm(
            t("diagnostics.crash_save_title"),
            t("diagnostics.crash_save_message"),
            self._save_crash_report,
            confirm_text=t("diagnostics.btn_save_crash"),
        )

    def offer_crash_report(self) -> None:
        """Offer the report once if this launch found that the last one crashed.

        Another dialog may be up at startup (a storage notice, a recovery
        prompt); the offer waits for it instead of replacing it.
        """

        if not self._vm.has_unreported_crashes():
            return
        if self._dialogs.isVisible():
            QTimer.singleShot(_OFFER_RETRY_MS, self.offer_crash_report)
            return
        self._vm.take_unreported_crashes()
        self._dialogs.show_confirm(
            t("diagnostics.crashed_title"),
            t("diagnostics.crashed_message"),
            self._save_crash_report,
            confirm_text=t("diagnostics.btn_save_crash"),
            cancel_text=t("diagnostics.btn_not_now"),
        )

    def start_delete(self) -> None:
        if self._vm.log_directory() is None:
            self._show_unavailable()
            return
        self._dialogs.show_confirm(
            t("diagnostics.delete_title"),
            t("diagnostics.delete_message"),
            self._delete,
            confirm_text=t("diagnostics.btn_delete"),
            destructive=True,
        )

    # -- steps ----------------------------------------------------------------------

    def _save_crash_report(self) -> None:
        self._tasks.submit(
            "save-crash-report",
            lambda: save_crash_report(desktop_directory()),
            on_success=self._show_crash_saved,
            on_failure=lambda error: self._show_failure("diagnostics.crash_failed_title", error),
        )

    def _delete(self) -> None:
        self._tasks.submit(
            "delete-logs",
            clear_logs,
            on_success=self._show_deleted,
            on_failure=lambda error: self._show_failure("diagnostics.delete_failed_title", error),
        )

    def _show_crash_saved(self, result: CrashReportResult) -> None:
        self._dialogs.show_info(
            t("diagnostics.crash_saved_title"),
            t("diagnostics.crash_saved_message", path=str(result.destination)),
        )

    def _show_deleted(self, result: LogClearResult) -> None:
        if result.failed_count:
            message = t(
                "diagnostics.delete_partial_message",
                count=str(result.removed_count),
                failed=str(result.failed_count),
            )
        else:
            message = t("diagnostics.delete_done_message", count=str(result.removed_count))
        self._dialogs.show_info(t("diagnostics.delete_done_title"), message)

    def _show_failure(self, title_key: str, error: Exception) -> None:
        # The task service has already logged the failure at its boundary.
        if isinstance(error, LogFilesUnavailable):
            message = t("diagnostics.unavailable")
        elif isinstance(error, NoCrashRecorded):
            message = t("diagnostics.no_crash_message", count=str(KEPT_RUN_COUNT))
        else:
            message = t("error.operation_failed", detail=str(error))
        self._dialogs.show_info(t(title_key), message)

    def _show_no_crash(self) -> None:
        self._dialogs.show_info(
            t("diagnostics.no_crash_title"),
            t("diagnostics.no_crash_message", count=str(KEPT_RUN_COUNT)),
        )

    def _show_unavailable(self) -> None:
        self._dialogs.show_info(t("diagnostics.banner"), t("diagnostics.unavailable"))
