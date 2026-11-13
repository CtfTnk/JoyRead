"""Per-launch log files, crash detection, crash reports and deleting logs."""

from __future__ import annotations

from datetime import datetime, timezone
import faulthandler
import logging
from pathlib import Path
import zipfile

import pytest

from joyread.infrastructure.logging import log_files, logging_service, run_files
from joyread.infrastructure.logging.log_files import (
    LogFilesUnavailable,
    NoCrashRecorded,
    clear_logs,
    crash_report_file_name,
    desktop_directory,
    recorded_crashes,
    save_crash_report,
)
from joyread.infrastructure.logging.logging_service import (
    configure_logging,
    current_run_stem,
    flush_logging,
    has_unreported_crashes,
    shutdown_logging,
    take_unreported_crashes,
    write_emergency_log,
)
from joyread.infrastructure.logging.log_format import read_log_records
from joyread.infrastructure.logging.run_files import (
    KEPT_RUN_COUNT,
    RunningMarker,
    crash_file_path,
    list_runs,
    prune_runs,
    recover_interrupted_runs,
    run_log_path,
    run_stem,
    running_marker_path,
)


@pytest.fixture(autouse=True)
def _reset_root_logger():
    shutdown_logging()
    take_unreported_crashes()
    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    for handler in saved:
        root.removeHandler(handler)
    yield
    shutdown_logging()
    take_unreported_crashes()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in saved:
        root.addHandler(handler)
    root.setLevel(saved_level)


def _fake_run(logs_dir: Path, stamp: str, pid: int = 100, *, crashed: bool = False) -> str:
    """Leave the files an earlier launch would have, as if it had ended."""

    stem = f"joyread-{stamp}-{pid}"
    logs_dir.mkdir(parents=True, exist_ok=True)
    run_log_path(logs_dir, stem).write_text(
        f"# JoyRead 1.4.0 · run {'0' * 32} · pid {pid} · started 2026-01-01 09:00:00 +0000\n"
        f"09:00:00.000 INFO    process  run {stem} · earlier  (app.bootstrap:1)\n",
        encoding="utf-8",
    )
    if crashed:
        crash_file_path(logs_dir, stem).write_text("crashed\n", encoding="utf-8")
    return stem


def _crashed_launch(logs_dir: Path, stamp: str, trace: str = "") -> str:
    """An earlier launch that never quit: its running marker is still there."""

    stem = _fake_run(logs_dir, stamp)
    running_marker_path(logs_dir, stem).write_text(trace, encoding="utf-8")
    return stem


def _records(path: Path) -> list[dict[str, object]]:
    return read_log_records(path)


# -- naming and listing ---------------------------------------------------------------


def test_a_launch_is_named_by_its_local_start_time_and_pid() -> None:
    started = datetime(2026, 11, 13, 6, 30, 5, tzinfo=timezone.utc)

    stem = run_stem(started, 4242)

    assert stem == f"joyread-{started.astimezone():%Y%m%d-%H%M%S}-4242"


def test_each_launch_writes_its_own_file(tmp_path: Path) -> None:
    earlier = _fake_run(tmp_path, "20260101-090000")

    result = configure_logging(tmp_path)
    logging.getLogger("joyread.reader").info("this launch")
    assert flush_logging()

    assert result.log_file == run_log_path(tmp_path, current_run_stem())
    assert result.log_file != run_log_path(tmp_path, earlier)
    assert any(record["message"] == "this launch" for record in _records(result.log_file))
    assert all(record["message"] != "this launch" for record in _records(run_log_path(tmp_path, earlier)))


def test_list_runs_groups_files_by_launch_newest_first(tmp_path: Path) -> None:
    old = _fake_run(tmp_path, "20260101-090000", crashed=True)
    new = _fake_run(tmp_path, "20260102-090000")
    (tmp_path / f"{new}.log.1").write_text("{}\n", encoding="utf-8")
    write_emergency_log(tmp_path, "process.startup_failed", "secondary failed")
    (tmp_path / "notes.txt").write_text("the user's own file", encoding="utf-8")

    runs = list_runs(tmp_path)

    assert [run.stem for run in runs] == [new, old]
    assert [path.name for path in runs[0].files] == [f"{new}.log", f"{new}.log.1"]
    assert [path.name for path in runs[1].files] == [f"{old}.log", f"{old}.crash.txt"]
    assert runs[0].crashed is False and runs[1].crashed is True


# -- crash detection ------------------------------------------------------------------


def test_a_clean_quit_leaves_no_marker(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    marker = running_marker_path(tmp_path, current_run_stem())
    assert marker.is_file()

    shutdown_logging()

    assert not marker.exists()
    assert recorded_crashes() == []


def test_a_leftover_marker_becomes_a_crash_file_and_is_offered_once(tmp_path: Path) -> None:
    crashed = _crashed_launch(tmp_path, "20260101-090000", trace="Fatal Python error: Segmentation fault")
    clean = _fake_run(tmp_path, "20260101-100000")

    result = configure_logging(tmp_path)

    assert result.crashed_runs == (crashed,)
    assert not running_marker_path(tmp_path, crashed).exists()
    text = crash_file_path(tmp_path, crashed).read_text(encoding="utf-8")
    assert "ended without quitting normally" in text
    assert f"{crashed}.log" in text
    assert "Segmentation fault" in text
    assert not crash_file_path(tmp_path, clean).exists()
    assert has_unreported_crashes()
    assert take_unreported_crashes() == (crashed,)
    assert take_unreported_crashes() == ()
    assert flush_logging()
    detected = [
        record for record in _records(result.log_file)
        if record["event"] == "logging.runs.crash_detected"
    ]
    assert detected and detected[0]["crashed_runs"] == [crashed]


def test_an_empty_marker_says_no_native_trace_was_recorded(tmp_path: Path) -> None:
    crashed = _crashed_launch(tmp_path, "20260101-090000")

    assert recover_interrupted_runs(tmp_path, "joyread-20991231-000000-1") == (crashed,)

    assert "No native crash trace was recorded." in crash_file_path(tmp_path, crashed).read_text(
        encoding="utf-8"
    )


def test_the_current_launchs_marker_is_not_a_crash(tmp_path: Path) -> None:
    current = _crashed_launch(tmp_path, "20260101-090000")

    assert recover_interrupted_runs(tmp_path, current) == ()
    assert running_marker_path(tmp_path, current).exists()


def test_a_startup_failure_is_reported_by_the_next_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging_service, "run_stem", lambda *_args: "joyread-20260101-090000-1")
    configure_logging(tmp_path)
    shutdown_logging(unexpected="JoyRead failed to start: RuntimeError")
    assert running_marker_path(tmp_path, "joyread-20260101-090000-1").is_file()

    monkeypatch.setattr(logging_service, "run_stem", lambda *_args: "joyread-20260101-090500-2")
    result = configure_logging(tmp_path)

    assert result.crashed_runs == ("joyread-20260101-090000-1",)
    text = crash_file_path(tmp_path, "joyread-20260101-090000-1").read_text(encoding="utf-8")
    assert "JoyRead failed to start: RuntimeError" in text


def test_the_marker_takes_faulthandler_only_when_nobody_else_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(faulthandler, "is_enabled", lambda: False)
    monkeypatch.setattr(faulthandler, "enable", lambda **kwargs: calls.append(f"enable all={kwargs['all_threads']}"))
    monkeypatch.setattr(faulthandler, "disable", lambda: calls.append("disable"))

    marker = RunningMarker(tmp_path / ".joyread-20260101-090000-1.running")
    assert marker.owns_faulthandler
    marker.close()

    assert calls == ["enable all=True", "disable"]
    assert not marker.path.exists()

    monkeypatch.setattr(faulthandler, "is_enabled", lambda: True)
    calls.clear()
    borrowed = RunningMarker(tmp_path / ".joyread-20260101-090100-1.running")
    borrowed.close(unexpected="startup failed")

    assert calls == []
    assert borrowed.path.read_text(encoding="utf-8") == "startup failed\n"


# -- retention ------------------------------------------------------------------------


def test_only_the_last_launches_are_kept(tmp_path: Path) -> None:
    stems = [_fake_run(tmp_path, f"2026010{day}-090000", crashed=day == 1) for day in range(1, 8)]
    (tmp_path / "joyread.log").write_text("legacy\n", encoding="utf-8")
    (tmp_path / "joyread.log.3").write_text("legacy\n", encoding="utf-8")
    for index in range(KEPT_RUN_COUNT + 2):
        (tmp_path / f"joyread-emergency-2026010{index}T000000.000000Z-1-abcdef01.jsonl").write_text(
            "{}\n", encoding="utf-8"
        )
    (tmp_path / "notes.txt").write_text("the user's own file", encoding="utf-8")
    current = "joyread-20260201-090000-1"

    removed = prune_runs(tmp_path, current)

    kept = [run.stem for run in list_runs(tmp_path)]
    assert kept == list(reversed(stems))[: KEPT_RUN_COUNT - 1]
    assert not crash_file_path(tmp_path, stems[0]).exists()
    assert not (tmp_path / "joyread.log").exists()
    assert not (tmp_path / "joyread.log.3").exists()
    assert len(list(tmp_path.glob("joyread-emergency-*.jsonl"))) == KEPT_RUN_COUNT
    assert (tmp_path / "notes.txt").exists()
    # 3 launches (one with a crash file), 2 emergency files, 2 legacy files.
    assert removed == 4 + 2 + 2


def test_configuring_logging_prunes_older_launches(tmp_path: Path) -> None:
    for day in range(1, 8):
        _fake_run(tmp_path, f"2026010{day}-090000")

    configure_logging(tmp_path)

    runs = list_runs(tmp_path)
    assert len(runs) == KEPT_RUN_COUNT
    assert runs[0].stem == current_run_stem()


# -- crash report ---------------------------------------------------------------------


def test_the_crash_report_holds_only_launches_that_crashed(tmp_path: Path) -> None:
    logs_dir = tmp_path / "Logs"
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    crashed = _crashed_launch(logs_dir, "20260101-090000", trace="trace")
    clean = _fake_run(logs_dir, "20260101-100000")
    configure_logging(logs_dir)
    logging.getLogger("joyread.reader").info(
        "Opening reader document: path=%s", "/Users/alice/Books/Spy x Family, Vol. 1.pdf"
    )
    assert flush_logging()

    result = save_crash_report(desktop, now=datetime(2026, 11, 13, 9, 30, 0))

    assert result.destination == desktop / "JoyRead-crash-report-20261113-093000.zip"
    assert result.crash_count == 1
    with zipfile.ZipFile(result.destination) as archive:
        names = sorted(archive.namelist())
        assert names == sorted([f"{crashed}.log", f"{crashed}.crash.txt"])
        assert b"run " + crashed.encode() in archive.read(f"{crashed}.log")
    assert clean not in "".join(names)
    assert current_run_stem() not in "".join(names)
    assert list(desktop.iterdir()) == [result.destination]
    assert flush_logging()
    saved = [
        record for record in _records(run_log_path(logs_dir, current_run_stem()))
        if record["event"] == "logging.crash_report.saved"
    ]
    assert saved and saved[0]["destination"] == str(result.destination)


def test_a_second_report_does_not_replace_the_first(tmp_path: Path) -> None:
    _crashed_launch(tmp_path / "Logs", "20260101-090000")
    configure_logging(tmp_path / "Logs")
    moment = datetime(2026, 11, 13, 9, 30, 0)

    first = save_crash_report(tmp_path, now=moment)
    second = save_crash_report(tmp_path, now=moment)

    assert first.destination.name == "JoyRead-crash-report-20261113-093000.zip"
    assert second.destination.name == "JoyRead-crash-report-20261113-093000-2.zip"
    assert first.destination.exists()


def test_no_crash_means_no_report(tmp_path: Path) -> None:
    _fake_run(tmp_path / "Logs", "20260101-090000")
    configure_logging(tmp_path / "Logs")

    with pytest.raises(NoCrashRecorded):
        save_crash_report(tmp_path)

    assert recorded_crashes() == []
    assert not list(tmp_path.glob("*.zip"))


def test_a_failed_report_leaves_no_partial_zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _crashed_launch(tmp_path / "Logs", "20260101-090000")
    configure_logging(tmp_path / "Logs")

    def broken_write(self, *_args, **_kwargs):  # noqa: ANN001
        raise OSError("disk full")

    monkeypatch.setattr(zipfile.ZipFile, "write", broken_write)

    with pytest.raises(OSError, match="disk full"):
        save_crash_report(tmp_path)

    assert not list(tmp_path.glob("*.zip"))
    assert not list(tmp_path.glob("*.partial"))


def test_crash_report_name() -> None:
    assert crash_report_file_name(datetime(2026, 1, 2, 3, 4, 5)) == "JoyRead-crash-report-20260102-030405.zip"


def test_desktop_directory_is_an_existing_folder() -> None:
    assert desktop_directory().is_dir()


# -- deleting -------------------------------------------------------------------------


def test_delete_removes_every_log_and_empties_the_current_one(tmp_path: Path) -> None:
    crashed = _fake_run(tmp_path, "20260101-090000", crashed=True)
    clean = _fake_run(tmp_path, "20260101-100000")
    configure_logging(tmp_path)
    write_emergency_log(tmp_path, "process.startup_failed", "secondary failed")
    (tmp_path / "notes.txt").write_text("the user's own file", encoding="utf-8")
    logging.getLogger("joyread.reader").info("Opening reader document: path=/Users/alice/a.pdf")
    assert flush_logging()
    current = run_log_path(tmp_path, current_run_stem())

    result = clear_logs()

    assert result.failed_count == 0
    assert result.removed_count == 5  # two launches' logs, a crash file, emergency, current
    assert not run_log_path(tmp_path, crashed).exists()
    assert not crash_file_path(tmp_path, crashed).exists()
    assert not run_log_path(tmp_path, clean).exists()
    assert not list(tmp_path.glob("joyread-emergency-*"))
    assert (tmp_path / "notes.txt").exists()
    assert running_marker_path(tmp_path, current_run_stem()).exists()
    assert flush_logging()
    records = _records(current)
    assert records[0]["event"] == "logging.files.cleared"
    assert "/Users/alice" not in current.read_text(encoding="utf-8")

    logging.getLogger("joyread.reader").info("after clearing")
    assert flush_logging()
    assert _records(current)[-1]["message"] == "after clearing"


def test_without_a_log_directory_nothing_is_touched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(log_files, "log_directory", lambda: None)

    with pytest.raises(LogFilesUnavailable):
        save_crash_report(tmp_path)
    with pytest.raises(LogFilesUnavailable):
        clear_logs()
    assert recorded_crashes() == []


def test_run_files_module_stays_free_of_qt() -> None:
    source = Path(run_files.__file__).read_text(encoding="utf-8")
    assert "PySide6" not in source
    assert "logging_service" not in source
