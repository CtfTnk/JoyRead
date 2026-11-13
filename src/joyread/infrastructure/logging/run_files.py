"""One log file per launch, the last few launches kept, crashes detected.

Every primary launch writes ``joyread-<local start time>-<pid>.log`` (JSON
Lines; a ``.log.1`` appears only if the launch outgrows the per-launch cap).
Opening the log folder therefore shows one file per launch, named by when it
started, and a launch's whole story is in its own file.

Crash detection uses a hidden marker, ``.joyread-<…>.running``, opened when
the launch starts. A clean quit closes and deletes it. A marker that is still
there on the next launch means that launch ended some other way -- a native
crash, a force quit, a power loss; from outside they look the same -- and it
becomes a visible ``joyread-<…>.crash.txt`` beside that launch's log. While
the marker is open it is also the :mod:`faulthandler` target, so a native
crash (a segfault in Qt, an abort) leaves the Python stack of every thread in
it, and that trace ends up in the ``.crash.txt``.

This module is Qt-free and does not import the logging runtime, so the
runtime, the export code and the tests can all use it.
"""

from __future__ import annotations

import faulthandler
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO


RUN_LOG_SUFFIX = ".log"
CRASH_FILE_SUFFIX = ".crash.txt"
RUN_LOG_MAX_BYTES = 5 * 1024 * 1024
RUN_LOG_BACKUP_COUNT = 1
#: Launches whose logs are kept, the current one included.
KEPT_RUN_COUNT = 5
EMERGENCY_LOG_GLOB = "joyread-emergency-*.jsonl"
#: The single rotating file older versions wrote. Removed on the first launch
#: that uses per-launch files: it is not one launch, so it cannot be kept as one.
LEGACY_LOG_FILE_NAME = "joyread.log"
_LEGACY_BACKUP_LIMIT = 9

_STEM = r"joyread-(?P<stamp>\d{8}-\d{6})-(?P<pid>\d+)"
_RUN_FILE_RE = re.compile(rf"^(?P<stem>{_STEM})(?P<kind>\.log(?:\.\d+)?|\.crash\.txt)$")
_MARKER_RE = re.compile(rf"^\.(?P<stem>{_STEM})\.running$")

_CRASH_HEADER = (
    "JoyRead ended without quitting normally: a crash, a force quit or a power loss.\n"
    "Launch: {stem}\n"
    "The log of that launch is {log_name}\n"
)
_NO_TRACE = "\nNo native crash trace was recorded.\n"
_TRACE_HEADING = "\nRecorded while the launch was running:\n\n"


@dataclass(frozen=True, slots=True)
class RunLogs:
    """The files one launch left in the log folder."""

    stem: str
    files: tuple[Path, ...]
    crashed: bool

    @property
    def log_file(self) -> Path:
        return self.files[0].with_name(self.stem + RUN_LOG_SUFFIX)


def run_stem(started_at: datetime, pid: int) -> str:
    """The name a launch's files share, in local time so users recognise it."""

    return f"joyread-{started_at.astimezone():%Y%m%d-%H%M%S}-{pid}"


def run_log_path(logs_dir: Path, stem: str) -> Path:
    return logs_dir / f"{stem}{RUN_LOG_SUFFIX}"


def crash_file_path(logs_dir: Path, stem: str) -> Path:
    return logs_dir / f"{stem}{CRASH_FILE_SUFFIX}"


def running_marker_path(logs_dir: Path, stem: str) -> Path:
    return logs_dir / f".{stem}.running"


def list_runs(logs_dir: Path) -> list[RunLogs]:
    """Every launch with files in ``logs_dir``, newest first.

    Running markers are not listed: the current launch's marker is in use, and
    an earlier one is turned into a crash file before anything reads the list.
    """

    grouped: dict[str, list[Path]] = {}
    sort_keys: dict[str, tuple[str, int]] = {}
    try:
        entries = list(logs_dir.iterdir())
    except OSError:
        return []
    for path in entries:
        match = _RUN_FILE_RE.match(path.name)
        if match is None or not path.is_file():
            continue
        stem = match["stem"]
        grouped.setdefault(stem, []).append(path)
        sort_keys[stem] = (match["stamp"], int(match["pid"]))
    runs: list[RunLogs] = []
    for stem, files in grouped.items():
        files.sort(key=lambda path: _file_order(stem, path))
        crashed = any(path.name.endswith(CRASH_FILE_SUFFIX) for path in files)
        runs.append(RunLogs(stem, tuple(files), crashed))
    runs.sort(key=lambda run: sort_keys[run.stem], reverse=True)
    return runs


def crashed_runs(logs_dir: Path) -> list[RunLogs]:
    return [run for run in list_runs(logs_dir) if run.crashed]


def legacy_log_files(logs_dir: Path) -> list[Path]:
    candidates = [logs_dir / LEGACY_LOG_FILE_NAME]
    candidates.extend(
        logs_dir / f"{LEGACY_LOG_FILE_NAME}.{index}" for index in range(1, _LEGACY_BACKUP_LIMIT + 1)
    )
    return [path for path in candidates if path.is_file()]


def emergency_log_files(logs_dir: Path) -> list[Path]:
    """Startup-failure files, newest first (their names start with a UTC stamp)."""

    try:
        return sorted(logs_dir.glob(EMERGENCY_LOG_GLOB), reverse=True)
    except OSError:
        return []


def recover_interrupted_runs(logs_dir: Path, current_stem: str) -> tuple[str, ...]:
    """Turn every leftover running marker into a crash file; return their stems.

    Called once the current launch is the primary, so no other launch can
    still be writing to a marker found here.
    """

    recovered: list[str] = []
    try:
        entries = sorted(logs_dir.iterdir())
    except OSError:
        return ()
    for marker in entries:
        match = _MARKER_RE.match(marker.name)
        if match is None or match["stem"] == current_stem:
            continue
        stem = match["stem"]
        try:
            trace = marker.read_text(encoding="utf-8", errors="replace").strip()
            text = _CRASH_HEADER.format(stem=stem, log_name=f"{stem}{RUN_LOG_SUFFIX}")
            text += f"{_TRACE_HEADING}{trace}\n" if trace else _NO_TRACE
            _write_private_text(crash_file_path(logs_dir, stem), text)
            marker.unlink()
        except OSError:
            continue
        recovered.append(stem)
    return tuple(recovered)


def prune_runs(logs_dir: Path, current_stem: str, keep: int = KEPT_RUN_COUNT) -> int:
    """Delete all but the newest ``keep`` launches, and the legacy log.

    The current launch is always kept and counts towards ``keep``. Emergency
    startup-failure files are trimmed to the same number. Returns how many
    files were deleted; a file that cannot be deleted is left for next time.
    """

    keep = max(1, keep)
    doomed: list[Path] = []
    older = [run for run in list_runs(logs_dir) if run.stem != current_stem]
    for run in older[keep - 1 :]:
        doomed.extend(run.files)
    doomed.extend(emergency_log_files(logs_dir)[keep:])
    doomed.extend(legacy_log_files(logs_dir))
    removed = 0
    for path in doomed:
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


class RunningMarker:
    """The hidden file whose survival marks a launch that did not quit cleanly.

    It is the :mod:`faulthandler` target while open, unless something else (a
    debugger, pytest, ``PYTHONFAULTHANDLER``) already enabled it -- that owner
    keeps it, and the marker only marks.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: TextIO | None = path.open("w", encoding="utf-8")
        if os.name == "posix":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        self._owns_faulthandler = False
        if not faulthandler.is_enabled():
            faulthandler.enable(file=self._stream, all_threads=True)
            self._owns_faulthandler = True

    @property
    def owns_faulthandler(self) -> bool:
        return self._owns_faulthandler

    def close(self, *, unexpected: str | None = None) -> None:
        """Delete the marker after a clean quit; keep it, with a note, otherwise.

        ``unexpected`` is for an exit the process saw coming but that is still
        a failure -- startup raising, say. The note is written into the marker
        and the next launch reports it like any other crash.
        """

        stream = self._stream
        if stream is None:
            return
        self._stream = None
        if self._owns_faulthandler:
            faulthandler.disable()
            self._owns_faulthandler = False
        try:
            if unexpected:
                stream.write(unexpected.rstrip() + "\n")
            stream.close()
        except OSError:
            pass
        if unexpected:
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def _file_order(stem: str, path: Path) -> tuple[int, str]:
    name = path.name
    if name == f"{stem}{RUN_LOG_SUFFIX}":
        return (0, name)
    if name.endswith(CRASH_FILE_SUFFIX):
        return (2, name)
    return (1, name)


def _write_private_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


__all__ = [
    "CRASH_FILE_SUFFIX",
    "EMERGENCY_LOG_GLOB",
    "KEPT_RUN_COUNT",
    "LEGACY_LOG_FILE_NAME",
    "RUN_LOG_BACKUP_COUNT",
    "RUN_LOG_MAX_BYTES",
    "RUN_LOG_SUFFIX",
    "RunLogs",
    "RunningMarker",
    "crash_file_path",
    "crashed_runs",
    "emergency_log_files",
    "legacy_log_files",
    "list_runs",
    "prune_runs",
    "recover_interrupted_runs",
    "run_log_path",
    "run_stem",
    "running_marker_path",
]
