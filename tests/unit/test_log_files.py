"""Exporting and deleting JoyRead's own log files."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import zipfile

import pytest

from joyread.infrastructure.logging import log_files
from joyread.infrastructure.logging.log_files import (
    LogFilesUnavailable,
    clear_logs,
    default_export_file_name,
    export_logs,
    log_file_paths,
)
from joyread.infrastructure.logging.logging_service import (
    LOG_FILE_NAME,
    configure_logging,
    flush_logging,
    shutdown_logging,
    write_emergency_log,
)


@pytest.fixture(autouse=True)
def _reset_root_logger():
    shutdown_logging()
    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    for handler in saved:
        root.removeHandler(handler)
    yield
    shutdown_logging()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in saved:
        root.addHandler(handler)
    root.setLevel(saved_level)


def _seed_logs(logs_dir: Path) -> None:
    configure_logging(logs_dir)
    logging.getLogger("joyread.reader").info(
        "Opening reader document: path=%s", "/Users/alice/Books/Spy x Family, Vol. 1.pdf"
    )
    assert flush_logging()
    (logs_dir / f"{LOG_FILE_NAME}.1").write_text('{"event":"old"}\n', encoding="utf-8")
    write_emergency_log(logs_dir, "process.startup_failed", "secondary failed")
    (logs_dir / "notes.txt").write_text("the user's own file", encoding="utf-8")


def test_log_file_paths_lists_only_joyread_files(tmp_path: Path) -> None:
    _seed_logs(tmp_path)

    names = [path.name for path in log_file_paths(tmp_path)]

    assert names[:2] == [LOG_FILE_NAME, f"{LOG_FILE_NAME}.1"]
    assert len(names) == 3
    assert names[2].startswith("joyread-emergency-")
    assert "notes.txt" not in names


def test_export_writes_every_log_file_verbatim_into_a_zip(tmp_path: Path) -> None:
    logs_dir = tmp_path / "Logs"
    _seed_logs(logs_dir)
    destination = tmp_path / "out" / "JoyRead-logs.zip"
    destination.parent.mkdir()

    result = export_logs(destination)

    assert result.destination == destination
    assert result.file_count == 3
    assert not destination.with_name(destination.name + ".partial").exists()
    with zipfile.ZipFile(destination) as archive:
        names = archive.namelist()
        active = archive.read(LOG_FILE_NAME).decode("utf-8")
    assert LOG_FILE_NAME in names and f"{LOG_FILE_NAME}.1" in names
    assert "notes.txt" not in names
    assert "/Users/alice/Books/Spy x Family, Vol. 1.pdf" in active
    assert flush_logging()
    exported = [
        json.loads(line)
        for line in (logs_dir / LOG_FILE_NAME).read_text(encoding="utf-8").splitlines()
        if '"logging.files.exported"' in line
    ]
    assert exported[-1]["destination"] == str(destination)
    assert exported[-1]["file_count"] == 3


def test_failed_export_leaves_no_partial_zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs_dir = tmp_path / "Logs"
    _seed_logs(logs_dir)
    destination = tmp_path / "JoyRead-logs.zip"

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(log_files.os, "replace", fail)
    with pytest.raises(OSError):
        export_logs(destination)

    assert not destination.exists()
    assert not destination.with_name(destination.name + ".partial").exists()


def test_clear_truncates_active_file_and_deletes_the_rest(tmp_path: Path) -> None:
    _seed_logs(tmp_path)

    result = clear_logs()

    assert result.removed_count == 3
    assert result.failed_count == 0
    assert [path.name for path in log_file_paths(tmp_path)] == [LOG_FILE_NAME]
    assert (tmp_path / "notes.txt").exists()
    assert flush_logging()
    records = [
        json.loads(line)
        for line in (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8").splitlines()
    ]
    # The writer keeps appending to the same handle after truncation.
    assert records[0]["event"] == "logging.files.cleared"
    assert "Spy x Family" not in (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8")
    logging.getLogger("joyread.after").info("after clear")
    assert flush_logging()
    assert "after clear" in (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8")


def test_operations_need_a_configured_log_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(log_files, "log_directory", lambda: None)

    with pytest.raises(LogFilesUnavailable):
        export_logs(tmp_path / "x.zip")
    with pytest.raises(LogFilesUnavailable):
        clear_logs()


def test_default_export_file_name_is_timestamped() -> None:
    from datetime import datetime

    assert default_export_file_name(datetime(2026, 11, 13, 9, 5, 7)) == "JoyRead-logs-20261113-090507.zip"
