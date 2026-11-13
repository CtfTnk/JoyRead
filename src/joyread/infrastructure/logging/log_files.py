"""Export and delete JoyRead's own log files.

The logs record real file paths and book names (see ``logging_service``), and
they never leave the machine on their own. These operations are how the user
gets them out -- one zip to attach to a report -- or gets rid of them, for
example after hiding books whose names earlier entries mention.

Both run on a worker thread and hold the log writer still while they work
(``hold_log_files``), so a rotation cannot rename a file mid-read and the
active file is never deleted out from under its open handle.
"""

from __future__ import annotations

import logging
import os
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from joyread.core.operation_logging import log_event
from joyread.infrastructure.logging.logging_service import (
    EMERGENCY_LOG_GLOB,
    LOG_FILE_BACKUP_COUNT,
    LOG_FILE_NAME,
    hold_log_files,
    log_directory,
)


logger = logging.getLogger(__name__)

_PARTIAL_SUFFIX = ".partial"


class LogFilesUnavailable(RuntimeError):
    """This process has no log directory, so there is nothing to act on."""

    task_failure_kind = "controlled"


@dataclass(frozen=True, slots=True)
class LogExportResult:
    destination: Path
    file_count: int
    byte_count: int


@dataclass(frozen=True, slots=True)
class LogClearResult:
    removed_count: int
    failed_count: int


def log_file_paths(logs_dir: Path) -> list[Path]:
    """Every log file JoyRead owns in ``logs_dir``, newest first.

    That is the active file, its rotated backups, and any emergency files a
    failed startup left behind. Anything else a user put in the folder is not
    ours to export or delete.
    """

    active = logs_dir / LOG_FILE_NAME
    candidates = [active]
    candidates.extend(
        logs_dir / f"{LOG_FILE_NAME}.{index}" for index in range(1, LOG_FILE_BACKUP_COUNT + 1)
    )
    files = [path for path in candidates if path.is_file()]
    files.extend(sorted(logs_dir.glob(EMERGENCY_LOG_GLOB), reverse=True))
    return files


def default_export_file_name(now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"JoyRead-logs-{stamp}.zip"


def export_logs(destination: Path) -> LogExportResult:
    """Write every log file into one zip at ``destination``.

    The zip is built beside the destination and renamed into place, so a
    failure never leaves a truncated archive under the name the user chose.
    """

    logs_dir = _require_log_directory()
    destination = destination.expanduser()
    partial = destination.with_name(destination.name + _PARTIAL_SUFFIX)
    file_count = 0
    byte_count = 0
    try:
        with hold_log_files():
            files = log_file_paths(logs_dir)
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in files:
                    archive.write(path, arcname=path.name)
                    file_count += 1
                    byte_count += path.stat().st_size
        os.replace(partial, destination)
    except BaseException:
        _remove_quietly(partial)
        raise
    log_event(
        logger,
        logging.INFO,
        "logging.files.exported",
        "Log files exported",
        category="settings",
        status="finished",
        destination=destination,
        file_count=file_count,
        byte_count=byte_count,
    )
    return LogExportResult(destination, file_count, byte_count)


def clear_logs() -> LogClearResult:
    """Delete rotated and emergency logs and empty the active file.

    The active file stays open in the writer, so it is truncated through that
    handle rather than unlinked: Windows refuses to delete an open file, and on
    POSIX an unlinked file would keep receiving records nobody can see.
    """

    logs_dir = _require_log_directory()
    removed = 0
    failed = 0
    with hold_log_files() as handler:
        active = logs_dir / LOG_FILE_NAME
        for path in log_file_paths(logs_dir):
            try:
                if path == active and handler is not None and handler.stream is not None:
                    handler.stream.seek(0)
                    handler.stream.truncate()
                else:
                    path.unlink()
                removed += 1
            except OSError:
                failed += 1
    # Written after the lock is released, so it is the new log's first entry.
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
            "logging.files.export_cleanup_failed",
            "Could not remove a partial log export",
            category="settings",
            exc_info=True,
            path=path,
        )


__all__ = [
    "LogClearResult",
    "LogExportResult",
    "LogFilesUnavailable",
    "clear_logs",
    "default_export_file_name",
    "export_logs",
    "log_file_paths",
]
