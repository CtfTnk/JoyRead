"""Queue path-issue service callbacks onto the Qt GUI thread."""

from __future__ import annotations

from PySide6.QtCore import QObject
from PySide6.QtCore import Signal as QtSignal

from joyread.core.models.path_issue import PathIssue
from joyread.core.services.path_issue_service import PathIssueService


class PathIssueBridge(QObject):
    """Adapt a Qt-free service listener to a queued Qt signal."""

    issue_detected = QtSignal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._service: PathIssueService | None = None

    def attach(self, service: PathIssueService) -> None:
        self.detach()
        self._service = service
        service.set_listener(self._on_issue)

    def detach(self) -> None:
        service, self._service = self._service, None
        if service is not None:
            service.set_listener(None)

    def _on_issue(self, issue: PathIssue) -> None:
        self.issue_detected.emit(issue)
