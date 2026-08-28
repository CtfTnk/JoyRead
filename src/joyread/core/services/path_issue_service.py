"""Classify and publish actionable path failures without importing Qt."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import logging
from pathlib import Path
from threading import RLock
from typing import Protocol

from joyread.core.models.path_issue import LongPathAccessError, PathIssue, PathIssueKind


logger = logging.getLogger(__name__)


class LongPathCapability(Protocol):
    """Platform adapter used by :class:`PathIssueService`."""

    def inspect_path(self, path: Path, *, operation: str) -> PathIssue | None: ...

    def inspect_directory(self, path: Path, *, operation: str) -> PathIssue | None: ...

    def inspect_error(
        self,
        error: BaseException,
        paths: Iterable[Path],
        *,
        operation: str,
    ) -> PathIssue | None: ...


PathIssueListener = Callable[[PathIssue], None]


class PathIssueService:
    """Own long-path diagnosis, once-per-run notification, and late delivery.

    File services can call this object from worker threads. The listener must do
    no UI work directly; AppContext installs a Qt bridge that queues delivery to
    the GUI thread.
    """

    def __init__(self, capability: LongPathCapability) -> None:
        self._capability = capability
        self._listener: PathIssueListener | None = None
        self._pending: PathIssue | None = None
        self._reported_kinds: set[PathIssueKind] = set()
        self._lock = RLock()

    def set_listener(self, listener: PathIssueListener | None) -> None:
        pending: PathIssue | None = None
        with self._lock:
            self._listener = listener
            if listener is not None and self._pending is not None:
                pending, self._pending = self._pending, None
        if listener is not None and pending is not None:
            listener(pending)

    def check_path(self, path: str | Path, *, operation: str) -> bool:
        issue = self._capability.inspect_path(Path(path), operation=operation)
        if issue is None:
            return True
        self._publish_once(issue)
        return False

    def require_path(self, path: str | Path, *, operation: str) -> None:
        issue = self._capability.inspect_path(Path(path), operation=operation)
        if issue is None:
            return
        self._publish_once(issue)
        raise LongPathAccessError(issue)

    def check_directory(self, path: str | Path, *, operation: str) -> bool:
        issue = self._capability.inspect_directory(Path(path), operation=operation)
        if issue is None:
            return True
        self._publish_once(issue)
        return False

    def report_os_error(
        self,
        error: BaseException,
        paths: Iterable[str | Path],
        *,
        operation: str,
    ) -> bool:
        issue = self._capability.inspect_error(
            error,
            (Path(path) for path in paths),
            operation=operation,
        )
        if issue is None:
            return False
        self._publish_once(issue)
        return True

    def _publish_once(self, issue: PathIssue) -> None:
        listener: PathIssueListener | None
        with self._lock:
            if issue.kind in self._reported_kinds:
                return
            self._reported_kinds.add(issue.kind)
            listener = self._listener
            if listener is None:
                self._pending = issue
        logger.warning(
            "Actionable path issue detected operation=%s kind=%s path_length=%d",
            issue.operation,
            issue.kind.value,
            issue.path_length,
        )
        if listener is not None:
            listener(issue)
