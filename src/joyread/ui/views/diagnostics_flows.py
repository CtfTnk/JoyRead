"""Settings > About > Diagnostics: open, export and delete the log files.

Export says what the logs contain before the save dialog opens: they record
real file paths and book names, including books that were hidden later, and
the user decides whether that is acceptable to send. There is deliberately no
"hide paths" variant -- a support log that cannot name the failing file does
not help, and a partial scrub would promise more privacy than it delivers.

File work runs on the process-wide task service, not the Library's scope: the
logs belong to the application support root and must stay reachable while the
Library is loading, moving or unavailable.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QWidget

from joyread.app.tasking import TaskExecutor
from joyread.infrastructure.i18n.locale_service import t
from joyread.infrastructure.logging.log_files import (
    LogClearResult,
    LogExportResult,
    LogFilesUnavailable,
    clear_logs,
    default_export_file_name,
    export_logs,
)
from joyread.ui.viewmodels.settings_viewmodel import SettingsViewModel
from joyread.ui.widgets.dialogs import JoyReadDialogOverlay


logger = logging.getLogger(__name__)

_ZIP_SUFFIX = ".zip"


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

    def start_export(self) -> None:
        if self._vm.log_directory() is None:
            self._show_unavailable()
            return
        self._dialogs.show_confirm(
            t("diagnostics.export_title"),
            t("diagnostics.export_message"),
            self._choose_export_destination,
            confirm_text=t("diagnostics.export_confirm"),
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

    def _choose_export_destination(self) -> None:
        chosen, _filter = QFileDialog.getSaveFileName(
            self._host,
            t("diagnostics.save_title"),
            str(Path.home() / default_export_file_name()),
            t("diagnostics.file_filter"),
        )
        if not chosen:
            return
        destination = Path(chosen)
        if destination.suffix.lower() != _ZIP_SUFFIX:
            destination = destination.with_name(destination.name + _ZIP_SUFFIX)
        self._tasks.submit(
            "export-logs",
            lambda: export_logs(destination),
            on_success=self._show_exported,
            on_failure=lambda error: self._show_failure("diagnostics.export_failed_title", error),
        )

    def _delete(self) -> None:
        self._tasks.submit(
            "delete-logs",
            clear_logs,
            on_success=self._show_deleted,
            on_failure=lambda error: self._show_failure("diagnostics.delete_failed_title", error),
        )

    def _show_exported(self, result: LogExportResult) -> None:
        self._dialogs.show_info(
            t("diagnostics.export_done_title"),
            t(
                "diagnostics.export_done_message",
                count=str(result.file_count),
                path=str(result.destination),
            ),
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
        else:
            message = t("error.operation_failed", detail=str(error))
        self._dialogs.show_info(t(title_key), message)

    def _show_unavailable(self) -> None:
        self._dialogs.show_info(t("diagnostics.banner"), t("diagnostics.unavailable"))
