"""Render global path-issue ViewModel state through an existing JoyRead dialog."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QWidget

from joyread.core.models.path_issue import PathIssue, PathIssueKind
from joyread.infrastructure.i18n.locale_service import t
from joyread.ui.viewmodels.path_issue_viewmodel import PathIssueViewModel
from joyread.ui.widgets.dialogs import JoyReadDialogOverlay


WINDOWS_LONG_PATHS_DOCUMENTATION = (
    "https://learn.microsoft.com/windows/win32/fileio/maximum-file-path-limitation"
)


class PathIssuePromptController(QObject):
    """Let exactly one active top-level window present a shared path issue."""

    def __init__(
        self,
        owner: QWidget,
        overlay: JoyReadDialogOverlay,
        viewmodel: PathIssueViewModel,
    ) -> None:
        super().__init__(owner)
        self._owner = owner
        self._overlay = overlay
        self._viewmodel = viewmodel
        owner.installEventFilter(self)
        viewmodel.issue_requested.connect(self.present)
        # Startup validation can diagnose a problem before the first window is
        # constructed. The ViewModel retains it until a visible window claims it.
        QTimer.singleShot(0, self.present_pending)

    def present_pending(self) -> None:
        issue = self._viewmodel.pending_issue
        if issue is not None:
            self.present(issue)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self._owner and event.type() == QEvent.Type.Show:
            QTimer.singleShot(0, self.present_pending)
        return super().eventFilter(watched, event)

    def present(self, issue: PathIssue) -> None:
        window = self._owner.window()
        if not window.isVisible():
            return
        active = QApplication.activeWindow()
        if active is not None and active is not window:
            return
        if not self._viewmodel.claim(issue):
            return

        if issue.kind == PathIssueKind.WINDOWS_LONG_PATHS_DISABLED:
            self._overlay.show_confirm(
                t("dialog.long_path_title"),
                t("dialog.long_path_disabled_msg", length=str(issue.path_length)),
                on_confirm=self._open_documentation,
                confirm_text=t("dialog.long_path_view_instructions"),
                cancel_text=t("dialog.btn_later"),
            )
            return
        self._overlay.show_info(
            t("dialog.long_path_title"),
            t("dialog.long_path_unsupported_msg", length=str(issue.path_length)),
        )

    @staticmethod
    def _open_documentation() -> None:
        QDesktopServices.openUrl(QUrl(WINDOWS_LONG_PATHS_DOCUMENTATION))
