"""The readable log layout: written for people, read back exactly."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path

import pytest

from joyread.infrastructure.logging import logging_service
from joyread.infrastructure.logging.log_files import clear_logs
from joyread.infrastructure.logging.log_format import (
    format_record,
    format_value,
    header_lines,
    parse_log_lines,
    parse_value,
    read_log_records,
)
from joyread.infrastructure.logging.logging_service import (
    configure_logging,
    current_run_stem,
    flush_logging,
    log_event,
    operation_scope,
    shutdown_logging,
    take_unreported_crashes,
)
from joyread.infrastructure.logging.run_files import run_log_path


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
    take_unreported_crashes()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in saved:
        root.addHandler(handler)
    root.setLevel(saved_level)


_MOMENT = datetime(2026, 11, 13, 21, 11, 49, 657000).astimezone()


def _line(message: str, *, event: str | None = None, fields=(), thread=None, detail=None) -> str:  # noqa: ANN001
    return format_record(
        moment=_MOMENT,
        level="WARNING",
        category="reader",
        message=message,
        event=event,
        fields=list(fields),
        location="core.reader.pages:88",
        thread=thread,
        detail=detail,
    )


def test_a_record_reads_as_one_line() -> None:
    line = _line(
        "Page decode failed",
        event="reader.page.failed",
        fields=[("page", 12), ("path", "/Users/alice/A B.cbz"), ("ok", False)],
        thread="PageWorker-2",
    )

    assert line == (
        "21:11:49.657 WARNING reader   Page decode failed · reader.page.failed page=12 "
        'path="/Users/alice/A B.cbz" ok=false  (core.reader.pages:88, PageWorker-2)'
    )


@pytest.mark.parametrize(
    "value",
    [
        0, 1.5, True, None, "plain", "", "with space", 'quote " inside', "a=b", "dot · inside",
        "(paren)", "[bracket", "/Users/alice/Books/Spy x Family, Vol. 1.pdf", "line\nbreak",
        ["a b", "c"], ["-password", "<redacted>"], [], "日本語のタイトル",
    ],
)
def test_field_values_read_back_exactly(value: object) -> None:
    text = format_value(value)

    assert "\n" not in text
    assert parse_value(text) == value
    [record] = parse_log_lines([_line("m", event="e.v", fields=[("x", value), ("y", 1)])])
    assert record["x"] == value and record["y"] == 1


@pytest.mark.parametrize(
    "message",
    ["Something · broke", "a · b · c", "ends with (parens)  (x:1)", "has key=value · inside", ""],
)
def test_awkward_messages_read_back_exactly(message: str) -> None:
    for event, fields in ((None, []), ("e.v", []), (None, [("n", 1)]), ("e.v", [("s", "x y")])):
        [record] = parse_log_lines([_line(message, event=event, fields=fields)])
        assert record["message"] == message
        assert record["event"] == (event or "log.message")
        assert record["logger"] == "core.reader.pages" and record["lineno"] == 88


def test_continuation_lines_carry_the_rest_of_the_message_and_the_traceback() -> None:
    text = _line("first\nsecond", detail="Traceback (most recent call last):\n  File x\nValueError: bad")

    assert text.splitlines()[1:] == [
        "    second",
        "    Traceback (most recent call last):",
        "      File x",
        "    ValueError: bad",
    ]
    [record] = parse_log_lines(text.splitlines())
    assert record["message"] == "first"
    assert record["detail"].endswith("ValueError: bad")


def test_the_header_says_once_what_every_line_shares() -> None:
    header = header_lines(
        app_version="1.4.0", build_id="ci-7", run_id="r" * 32, pid=42,
        started_at=datetime(2026, 11, 13, 13, 0, tzinfo=timezone.utc),
    )

    assert header[0].startswith("# JoyRead 1.4.0 (build ci-7) · run " + "r" * 32 + " · pid 42 · started ")
    assert parse_log_lines([*header, _line("m")])[0]["date"] == (
        datetime(2026, 11, 13, 13, 0, tzinfo=timezone.utc).astimezone().date().isoformat()
    )


def test_a_launch_past_midnight_marks_the_new_day(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    tomorrow = datetime.now() + timedelta(days=1)
    logger = logging.getLogger("joyread.test")
    record = logger.makeRecord(logger.name, logging.INFO, __file__, 1, "after midnight", (), None)
    record.created = tomorrow.timestamp()
    logger.handle(record)
    assert flush_logging()

    log_file = run_log_path(tmp_path, current_run_stem())
    assert f"# {tomorrow.date().isoformat()}" in log_file.read_text(encoding="utf-8").splitlines()
    record = read_log_records(log_file)[-1]
    assert record["message"] == "after midnight"
    assert record["date"] == tomorrow.date().isoformat()


def test_an_emptied_log_starts_again_with_its_header(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    logging.getLogger("joyread.test").info("before")
    assert flush_logging()

    clear_logs()
    assert flush_logging()

    lines = run_log_path(tmp_path, current_run_stem()).read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# JoyRead ")
    assert "before" not in "\n".join(lines)
    assert read_log_records(run_log_path(tmp_path, current_run_stem()))[0]["event"] == "logging.files.cleared"


def test_records_name_the_line_that_logged_them(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    logger = logging.getLogger("joyread.test")
    log_event(logger, logging.INFO, "test.event", "direct"); event_line = _this_line()
    with operation_scope(logger, "test.scope"): scope_line = _this_line()  # noqa: E701
    assert flush_logging()

    records = read_log_records(run_log_path(tmp_path, current_run_stem()))
    by_event = {record["event"]: record for record in records}
    assert by_event["test.event"]["lineno"] == event_line
    assert by_event["test.scope.started"]["lineno"] == scope_line
    assert by_event["test.scope.finished"]["lineno"] == scope_line


def test_the_console_uses_the_same_layout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(tmp_path)
    logging.getLogger("joyread.test").warning("to the terminal")
    assert flush_logging()

    [record] = [r for r in parse_log_lines(capsys.readouterr().err.splitlines()) if r.get("message") == "to the terminal"]
    assert record["level"] == "WARNING" and record["logger"] == "test"


def _this_line() -> int:
    import inspect

    return inspect.currentframe().f_back.f_lineno


def test_logging_service_keeps_no_json_formatter() -> None:
    assert not hasattr(logging_service, "_JsonFormatter")
