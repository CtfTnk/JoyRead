"""UI state for one actionable path issue shared by all top-level windows."""

from __future__ import annotations

from joyread.core.models.path_issue import PathIssue
from joyread.ui.viewmodels.signals import Signal


class PathIssueViewModel:
    """Publish and single-claim path prompts without owning any widgets."""

    def __init__(self) -> None:
        self.issue_requested: Signal[PathIssue] = Signal("path_issue.requested")
        self._pending_issue: PathIssue | None = None
        self._claimed = False

    @property
    def pending_issue(self) -> PathIssue | None:
        return self._pending_issue if not self._claimed else None

    def present(self, issue: PathIssue) -> None:
        self._pending_issue = issue
        self._claimed = False
        self.issue_requested.emit(issue)

    def claim(self, issue: PathIssue) -> bool:
        if self._claimed or self._pending_issue != issue:
            return False
        self._claimed = True
        return True
