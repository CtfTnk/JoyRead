"""Save a crash report, or delete JoyRead's own log files.

The logs record real file paths and book names (see ``logging_service``), and
they never leave the machine on their own. A crash report is how the user
gets the relevant part out: a zip, on the Desktop, holding only the launches
that ended without a clean quit (see ``run_files``). Everything else stays in
the log folder, which the user can open and read for themselves. Deleting is
for getting rid of the logs, for example after hiding books whose names
earlier entries mention.

Both run on a worker thread. Deleting holds the log writer still while it
works (``hold_log_files``), so the current launch's file is emptied through
its open handle rather than removed from under it.
"""

from __future__ import annotations

import logging
import os
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QStandardPaths

from joyread.core.operation_logging import log_event
from joyread.infrastructure.logging.logging_service import (
    current_run_stem,
    hold_log_files,
    log_directory,
)
from joyread.infrastructure.logging.run_files import (
    RunLogs,
    crashed_runs,
    emergency_log_files,
    legacy_log_files,
    list_runs,
    run_log_path,
)


logger = logging.getLogger(__name__)

_PARTIAL_SUFFIX = ".partial"
_MAX_NAME_ATTEMPTS = 100


class LogFilesUnavailable(RuntimeError):
    """This process has no log directory, so there is nothing to act on."""

    task_failure_kind = "controlled"


class NoCrashRecorded(RuntimeError):
    """No kept launch ended unexpectedly, so there is no crash to report."""

    task_failure_kind = "controlled"


@dataclass(frozen=True, slots=True)
class CrashReportResult:
    destination: Path
    crash_count: int
    file_count: int


@dataclass(frozen=True, slots=True)
class LogClearResult:
    removed_count: int
    failed_count: int


def recorded_crashes() -> list[RunLogs]:
    """Kept launches that ended unexpectedly, newest first; [] without logs."""

    logs_dir = log_directory()
    return [] if logs_dir is None else crashed_runs(logs_dir)


def desktop_directory() -> Path:
    """The user's Desktop, or their home folder where there is none."""

    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
    if location:
        desktop = Path(location)
        if desktop.is_dir():
            return desktop
    return Path.home()


def crash_report_file_name(now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"JoyRead-crash-report-{stamp}.zip"


def save_crash_report(directory: Path, *, now: datetime | None = None) -> CrashReportResult:
    """Zip every launch that ended unexpectedly into ``directory``.

    The current launch is never among them -- it has not ended -- so no file
    the writer is appending to is read. The zip is built under a temporary
    name and renamed into place, and an existing file is never replaced.
    """

    logs_dir = _require_log_directory()
    crashes = crashed_runs(logs_dir)
    if not crashes:
        raise NoCrashRecorded("No kept launch ended unexpectedly")
    destination = _unused_path(directory, crash_report_file_name(now))
    partial = destination.with_name(destination.name + _PARTIAL_SUFFIX)
    file_count = 0
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for run in crashes:
                for path in run.files:
                    archive.write(path, arcname=path.name)
                    file_count += 1
        os.replace(partial, destination)
    except BaseException:
        _remove_quietly(partial)
        raise
    log_event(
        logger,
        logging.INFO,
        "logging.crash_report.saved",
        "Crash report saved",
        category="settings",
        status="finished",
        destination=destination,
        crash_count=len(crashes),
        file_count=file_count,
    )
    return CrashReportResult(destination, len(crashes), file_count)


def clear_logs() -> LogClearResult:
    """Delete every log file and empty the current launch's.

    The current launch's file stays open in the writer, so it is truncated
    through that handle rather than unlinked: Windows refuses to delete an open
    file, and on POSIX an unlinked file would keep receiving records nobody can
    see. Its running marker is left alone -- it is how a crash later in this
    launch would still be noticed.
    """

    logs_dir = _require_log_directory()
    stem = current_run_stem()
    active = run_log_path(logs_dir, stem) if stem is not None else None
    removed = 0
    failed = 0
    with hold_log_files() as handler:
        files = [path for run in list_runs(logs_dir) for path in run.files]
        files.extend(emergency_log_files(logs_dir))
        files.extend(legacy_log_files(logs_dir))
        for path in files:
            try:
                if path == active and handler is not None and handler.stream is not None:
                    restart = getattr(handler, "restart", None)
                    if callable(restart):
                        restart()
                    else:
                        handler.stream.seek(0)
                        handler.stream.truncate()
                else:
                    path.unlink()
                removed += 1
            except OSError:
                failed += 1
    # Written after the lock is released, so it is the emptied log's first entry.
    log_event(
        logger,
        logging.WARNING if failed else logging.INFO,
        "logging.files.cleared",
        "Log files deleted",
        category="settings",
        status="finished",
        outcome="degraded" if failed else "cleared",
        removed_count=removed,
        failed_count=failed,
    )
    return LogClearResult(removed, failed)


def _unused_path(directory: Path, file_name: str) -> Path:
    candidate = directory / file_name
    stem, suffix = candidate.stem, candidate.suffix
    for attempt in range(2, _MAX_NAME_ATTEMPTS + 2):
        if not candidate.exists():
            return candidate
        candidate = directory / f"{stem}-{attempt}{suffix}"
    raise FileExistsError(f"No free crash report name in {directory}")


def _require_log_directory() -> Path:
    logs_dir = log_directory()
    if logs_dir is None:
        raise LogFilesUnavailable("Logging has no log directory in this process")
    return logs_dir


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log_event(
            logger,
            logging.WARNING,
            "logging.crash_report.cleanup_failed",
            "Could not remove a partial crash report",
            category="settings",
            exc_info=True,
            path=path,
        )


__all__ = [
    "CrashReportResult",
    "LogClearResult",
    "LogFilesUnavailable",
    "NoCrashRecorded",
    "clear_logs",
    "crash_report_file_name",
    "desktop_directory",
    "recorded_crashes",
    "save_crash_report",
]
