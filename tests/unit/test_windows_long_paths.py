from __future__ import annotations

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from joyread.core.archive.errors import ArchiveOpenError
from joyread.app.app_context import StorageTransition
from joyread.core.models.path_issue import LongPathAccessError, PathIssue, PathIssueKind
from joyread.core.reader.session_service import ReaderSessionService
from joyread.core.services.archive_extraction_pool import HiddenImageExtractionPool
from joyread.core.services.path_issue_service import PathIssueService
from joyread.core.services.storage_validation_service import (
    StorageValidationCode,
    StorageValidationResult,
    StorageValidationService,
)
from joyread.infrastructure.filesystem.windows_long_paths import WindowsLongPathCapability
from joyread.ui.viewmodels.path_issue_viewmodel import PathIssueViewModel
from joyread.ui.views.main_window import MainWindow


def _long_path() -> Path:
    return Path("C:/") / ("segment-" * 40)


def _winerror_206() -> OSError:
    error = OSError(errno.ENAMETOOLONG, "path too long")
    error.winerror = 206  # type: ignore[attr-defined]
    return error


def test_disabled_windows_policy_rejects_a_known_overlong_path() -> None:
    capability = WindowsLongPathCapability(
        platform_name="nt",
        registry_reader=lambda: False,
    )

    issue = capability.inspect_path(_long_path(), operation="reader_open")

    assert issue is not None
    assert issue.kind == PathIssueKind.WINDOWS_LONG_PATHS_DISABLED
    assert issue.path_length >= 260


def test_enabled_windows_policy_allows_preflight_but_classifies_winerror_206() -> None:
    capability = WindowsLongPathCapability(
        platform_name="nt",
        registry_reader=lambda: True,
    )

    assert capability.inspect_path(_long_path(), operation="reader_open") is None
    issue = capability.inspect_error(
        _winerror_206(),
        (_long_path(),),
        operation="reader_open",
    )

    assert issue is not None
    assert issue.kind == PathIssueKind.PATH_TOO_LONG_UNSUPPORTED


def test_disabled_policy_uses_the_stricter_directory_creation_limit() -> None:
    capability = WindowsLongPathCapability(
        platform_name="nt",
        registry_reader=lambda: False,
    )
    base = os.path.abspath(os.curdir)
    directory = Path("d" * (248 - len(base) - 1))

    assert capability.inspect_path(directory, operation="read") is None
    issue = capability.inspect_directory(directory, operation="mkdir")

    assert issue is not None
    assert issue.kind == PathIssueKind.WINDOWS_LONG_PATHS_DISABLED


def test_non_path_os_error_is_not_misdiagnosed() -> None:
    capability = WindowsLongPathCapability(
        platform_name="nt",
        registry_reader=lambda: False,
    )

    issue = capability.inspect_error(
        PermissionError(errno.EACCES, "denied"),
        (_long_path(),),
        operation="import_source",
    )

    assert issue is None


def test_path_issue_service_keeps_one_late_notification_and_deduplicates() -> None:
    service = PathIssueService(
        WindowsLongPathCapability(platform_name="nt", registry_reader=lambda: False)
    )
    received: list[PathIssue] = []

    assert service.check_path(_long_path(), operation="first") is False
    assert service.check_path(_long_path(), operation="second") is False
    service.set_listener(received.append)

    assert len(received) == 1
    assert received[0].operation == "first"


def test_require_path_publishes_before_raising() -> None:
    service = PathIssueService(
        WindowsLongPathCapability(platform_name="nt", registry_reader=lambda: False)
    )
    received: list[PathIssue] = []
    service.set_listener(received.append)

    with pytest.raises(LongPathAccessError):
        service.require_path(_long_path(), operation="import_manifest")

    assert len(received) == 1


def test_path_issue_viewmodel_allows_only_one_window_to_claim_an_issue() -> None:
    viewmodel = PathIssueViewModel()
    issue = PathIssue(PathIssueKind.WINDOWS_LONG_PATHS_DISABLED, "reader_open", 281)

    viewmodel.present(issue)

    assert viewmodel.pending_issue == issue
    assert viewmodel.claim(issue) is True
    assert viewmodel.claim(issue) is False
    assert viewmodel.pending_issue is None


def test_storage_validation_returns_a_structured_long_path_code() -> None:
    path_issues = PathIssueService(
        WindowsLongPathCapability(platform_name="nt", registry_reader=lambda: False)
    )
    service = StorageValidationService(path_issue_service=path_issues)

    result = service.validate_lightweight(_long_path())

    assert result.code == StorageValidationCode.LONG_PATHS_DISABLED


def test_storage_completion_does_not_overwrite_the_actionable_path_prompt() -> None:
    shown: list[tuple[str, str]] = []
    calls: list[str] = []
    receiver = SimpleNamespace(
        _context=SimpleNamespace(
            finish_storage_transition=lambda _transition: None,
            resume_after_storage_transition=lambda: calls.append("resumed"),
        ),
        _storage_transition=SimpleNamespace(acknowledge=lambda: calls.append("acknowledged")),
        dialog_overlay=SimpleNamespace(
            show_info=lambda title, message: shown.append((title, message))
        ),
    )
    transition = StorageTransition(
        "storage-select",
        SimpleNamespace(),  # type: ignore[arg-type]
        # This test is about the View's arbitration after PathIssueBridge has
        # already presented its actionable dialog.
        result=StorageValidationResult.failure(
            StorageValidationCode.LONG_PATHS_DISABLED,
            "generic fallback",
        ),
    )

    MainWindow._complete_storage_transition(receiver, transition)  # type: ignore[arg-type]

    assert shown == []
    assert calls == ["resumed", "acknowledged"]


def test_hidden_cache_reports_derived_path_before_attempting_the_write(tmp_path: Path) -> None:
    path_issues = PathIssueService(
        WindowsLongPathCapability(platform_name="nt", registry_reader=lambda: False)
    )
    received: list[PathIssue] = []
    path_issues.set_listener(received.append)
    pool = HiddenImageExtractionPool(
        tmp_path / ("deep-" * 20),
        1 << 20,
        path_issue_service=path_issues,
    )

    written = pool.put("file:managed", "page.png", b"payload")

    assert written is False
    assert received
    assert received[0].operation in {
        "archive_cache_directory_create",
        "archive_cache_write",
    }


def test_reader_reports_a_wrapped_winerror_206() -> None:
    class FailingArchiveService:
        def open(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
            try:
                raise _winerror_206()
            except OSError as error:
                raise ArchiveOpenError("could not open") from error

    path_issues = PathIssueService(
        WindowsLongPathCapability(platform_name="nt", registry_reader=lambda: True)
    )
    received: list[PathIssue] = []
    path_issues.set_listener(received.append)
    reader = ReaderSessionService(
        FailingArchiveService(),  # type: ignore[arg-type]
        path_issue_service=path_issues,
    )

    with pytest.raises(ArchiveOpenError):
        reader.open_document("book.cbz")

    assert received[0].kind == PathIssueKind.PATH_TOO_LONG_UNSUPPORTED
