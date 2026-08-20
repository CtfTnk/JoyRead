"""Tests for the comprehensive logging foundation."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import stat
import sys
from threading import Event

import pytest

from joyread.core.operation_context import bind_operation, create_operation
from joyread.infrastructure.logging import logging_service
from joyread.infrastructure.logging.logging_service import (
    LOG_ENV_VAR,
    LOG_FILE_BACKUP_COUNT,
    LOG_FILE_MAX_BYTES,
    LOG_FILE_NAME,
    configure_early_logging,
    configure_logging,
    describe_callback,
    flush_logging,
    get_logger,
    log_event,
    log_timed_block,
    operation_scope,
    shutdown_logging,
    write_emergency_log,
)
from joyread.core.services.storage_recovery_service import StorageRecoveryCancelled


@pytest.fixture(autouse=True)
def _reset_root_logger():
    """Drop handlers installed by previous tests so each run starts clean."""

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
        try:
            handler.close()
        except Exception:
            pass
    for handler in saved:
        root.addHandler(handler)
    root.setLevel(saved_level)


def test_configure_logging_installs_async_runtime_with_rotating_file(tmp_path: Path) -> None:
    result = configure_logging(tmp_path)
    root = logging.getLogger()

    assert result.file_logging_enabled is True
    assert result.log_file == tmp_path / LOG_FILE_NAME
    assert len(root.handlers) == 1
    assert root.handlers[0].__class__.__name__ == "_AsyncDispatchHandler"
    assert LOG_FILE_MAX_BYTES == 5 * 1024 * 1024
    assert LOG_FILE_BACKUP_COUNT == 5


def test_configure_logging_default_level_is_info(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LOG_ENV_VAR, raising=False)
    configure_logging(tmp_path)
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_env_var_promotes_debug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOG_ENV_VAR, "debug")
    configure_logging(tmp_path)
    assert logging.getLogger().level == logging.DEBUG
    # Module loggers inherit the root level by default; confirm via effective.
    assert logging.getLogger("joyread.test").getEffectiveLevel() == logging.DEBUG


def test_configure_logging_unknown_env_value_falls_back_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LOG_ENV_VAR, "loud")
    configure_logging(tmp_path)
    assert flush_logging()
    records = _read_json_records(tmp_path / LOG_FILE_NAME)
    assert logging.getLogger().level == logging.INFO
    assert any(record["event"] == "logging.level.invalid" for record in records)


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    configure_logging(tmp_path)
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert handlers[0].__class__.__name__ == "_AsyncDispatchHandler"


def test_configure_logging_writes_to_file(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    logging.getLogger("joyread.test").info("hello from joyread")
    assert flush_logging()
    log_path = tmp_path / LOG_FILE_NAME
    assert log_path.exists()
    records = _read_json_records(log_path)
    record = next(item for item in records if item["message"] == "hello from joyread")
    assert record["logger"] == "joyread.test"
    assert record["run_id"]
    assert record["run_started_at_utc"].endswith("Z")
    assert record["timestamp_utc"].endswith("Z")
    assert record["pid"] == os.getpid()


def test_json_formatter_preserves_new_structured_fields_without_a_whitelist(
    tmp_path: Path,
) -> None:
    configure_logging(tmp_path)
    log_event(
        logging.getLogger("joyread.test"),
        logging.INFO,
        "cache.snapshot",
        "cache snapshot",
        category="cache",
        current_bytes=123,
        budget_bytes=456,
        strategy="zip_bundle",
    )
    assert flush_logging()

    record = _read_json_records(tmp_path / LOG_FILE_NAME)[-1]
    assert record["current_bytes"] == 123
    assert record["budget_bytes"] == 456
    assert record["strategy"] == "zip_bundle"


def test_configure_logging_quiets_pil(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    assert logging.getLogger("PIL").level == logging.WARNING


def test_get_logger_returns_named_logger() -> None:
    one = get_logger("joyread.facade.test")
    two = get_logger("joyread.facade.test")
    assert one is two
    assert one.name == "joyread.facade.test"


def test_describe_callback_names_plain_lambda_and_bound_method() -> None:
    def local_callback() -> None:
        pass

    class Receiver:
        def handle(self) -> None:
            pass

    lambda_label = describe_callback(lambda: None)
    function_label = describe_callback(local_callback)
    method_label = describe_callback(Receiver().handle)

    assert lambda_label.endswith(".<lambda>")
    assert function_label.endswith("test_describe_callback_names_plain_lambda_and_bound_method.<locals>.local_callback")
    assert method_label.endswith("Receiver.handle")


def test_log_timed_block_emits_start_and_done(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = get_logger("joyread.facade.timed")
    with caplog.at_level(logging.DEBUG, logger="joyread.facade.timed"):
        with log_timed_block(logger, "scan"):
            pass
    messages = [record.getMessage() for record in caplog.records]
    assert any(m == "scan start" for m in messages)
    assert any(m.startswith("scan done in ") and m.endswith(" ms") for m in messages)


def test_log_timed_block_emits_failed_when_block_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = get_logger("joyread.facade.timed_raises")
    with caplog.at_level(logging.DEBUG, logger="joyread.facade.timed_raises"):
        with pytest.raises(RuntimeError):
            with log_timed_block(logger, "import-batch"):
                raise RuntimeError("boom")
    messages = [record.getMessage() for record in caplog.records]
    assert any(m == "import-batch start" for m in messages)
    assert any(m.startswith("import-batch failed in ") for m in messages)
    assert not any(m.startswith("import-batch done in ") for m in messages)


def test_log_timed_block_respects_custom_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = get_logger("joyread.facade.timed_info")
    with caplog.at_level(logging.INFO, logger="joyread.facade.timed_info"):
        with log_timed_block(logger, "migrate", level=logging.INFO):
            pass
    levels = {record.levelno for record in caplog.records}
    assert levels == {logging.INFO}


def test_qt_message_handler_forwards_to_joyread_qt_logger(tmp_path: Path) -> None:
    from PySide6 import QtCore

    configure_logging(tmp_path)
    QtCore.qWarning("simulated qt warning")
    assert flush_logging()
    records = _read_json_records(tmp_path / LOG_FILE_NAME)
    record = next(item for item in records if item["message"] == "simulated qt warning")
    assert record["logger"] == "joyread.qt"
    assert record["level"] == "WARNING"
    assert record["event"] == "qt.message"


def test_early_records_are_replayed_to_production_file(tmp_path: Path) -> None:
    metadata = configure_early_logging()
    logging.getLogger("joyread.startup").info("startup before paths")

    configure_logging(tmp_path)
    assert flush_logging()

    records = _read_json_records(tmp_path / LOG_FILE_NAME)
    early = next(item for item in records if item["message"] == "startup before paths")
    assert early["run_id"] == metadata.run_id


def test_paths_and_passwords_are_redacted_by_default(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    logging.getLogger("joyread.privacy").error(
        "unar failed to open /Users/alice/Private/secret.cbz password=hunter2 -pleaked"
    )
    assert flush_logging()

    contents = (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "/Users/alice" not in contents
    assert "hunter2" not in contents
    assert "leaked" not in contents
    assert "<path:" in contents
    assert "<redacted>" in contents


def test_glued_password_flag_is_preserved_without_archive_tool_context(tmp_path: Path) -> None:
    """The glued form (`-p<value>`) is genuinely ambiguous with legitimate
    single-dash flag names (-pages, -profile, -port); redacting it
    unconditionally destroys crash-diagnostic text that has nothing to do
    with archive passwords. Only redact it where an archive tool this app
    actually invokes is mentioned in the same text -- true in every real
    leak path, since the executable is command[0].
    """

    configure_logging(tmp_path)
    logging.getLogger("joyread.privacy").error(
        "render flags: -pages 3 -profile fast -port 8080"
    )
    assert flush_logging()

    contents = (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "-pages" in contents
    assert "-profile" in contents
    assert "-port" in contents
    assert "<redacted>" not in contents


def test_glued_password_flag_is_redacted_with_archive_tool_context(tmp_path: Path) -> None:
    """The real leak this guards: subprocess.TimeoutExpired's str() embeds the
    whole argv, including 7-Zip's glued `-p{password}` convention, alongside
    the executable name."""

    configure_logging(tmp_path)
    logging.getLogger("joyread.privacy").error(
        "Command '['7zz', 'x', '-so', '-y', '-phunter2', '--', '/tmp/a.7z']' timed out"
    )
    assert flush_logging()

    contents = (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "hunter2" not in contents
    assert "<redacted>" in contents


def test_multi_word_path_segments_are_fully_redacted(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    logging.getLogger("joyread.privacy").error(
        "Unable to open /Users/alice/My Books/Vol 1.cbz"
    )
    assert flush_logging()

    contents = (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "My Books" not in contents
    assert "Vol 1" not in contents
    assert "<path:" in contents


def test_password_after_separated_cli_flag_is_redacted(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    logging.getLogger("joyread.privacy").error(
        "Command '['unar', '-quiet', '-password', 'hunter2', '/tmp/x.rar']' timed out"
    )
    assert flush_logging()

    contents = (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "hunter2" not in contents
    assert "<redacted>" in contents


def test_path_field_with_space_is_fully_redacted(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    log_event(
        logging.getLogger("joyread.privacy"),
        logging.ERROR,
        "privacy.path_field",
        "Archive read failed",
        archive_path=Path("/Users/alice/My Books/secret.cbz"),
    )
    assert flush_logging()

    contents = (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "My Books" not in contents
    assert "secret.cbz" not in contents
    assert "<path:" in contents


def test_sensitive_path_opt_in_never_exposes_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(logging_service.LOG_SENSITIVE_ENV_VAR, "1")
    configure_logging(tmp_path)
    log_event(
        logging.getLogger("joyread.privacy"),
        logging.ERROR,
        "privacy.test",
        "unar failed to open /Users/alice/Private/secret.cbz password=hunter2 -pleaked",
        password="field-secret",
    )
    assert flush_logging()

    contents = (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "/Users/alice/Private/secret.cbz" in contents
    assert "hunter2" not in contents
    assert "leaked" not in contents
    assert "field-secret" not in contents


def test_operation_scope_emits_correlated_terminal_events(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    with operation_scope(logging.getLogger("joyread.reader"), "reader.document.open") as operation:
        logging.getLogger("joyread.archive").info("archive scan")
    assert flush_logging()

    records = _read_json_records(tmp_path / LOG_FILE_NAME)
    related = [record for record in records if record["operation_id"] == operation.operation_id]
    assert [record["event"] for record in related] == [
        "reader.document.open.started",
        "log.message",
        "reader.document.open.finished",
    ]
    assert all(record["operation_name"] == "reader.document.open" for record in related)
    assert related[-1]["status"] == "finished"
    assert related[-1]["duration_ms"] >= 0


def test_operation_scope_classifies_cancelled_exceptions_without_error_traceback(
    tmp_path: Path,
) -> None:
    configure_logging(tmp_path)
    with pytest.raises(StorageRecoveryCancelled):
        with operation_scope(logging.getLogger("joyread.storage"), "storage.recovery"):
            raise StorageRecoveryCancelled
    assert flush_logging()

    record = _read_json_records(tmp_path / LOG_FILE_NAME)[-1]
    assert record["event"] == "storage.recovery.cancelled"
    assert record["level"] == "INFO"
    assert record["status"] == "cancelled"
    assert "exception" not in record


def test_bound_child_operation_records_parent_id(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    parent = create_operation("reader.open", category="reader")
    with bind_operation(parent):
        child = create_operation("archive.scan", category="archive")
    with bind_operation(child):
        log_event(
            logging.getLogger("joyread.archive"),
            logging.INFO,
            "archive.scan.started",
            "archive scan started",
        )
    assert flush_logging()

    record = _read_json_records(tmp_path / LOG_FILE_NAME)[-1]
    assert record["operation_id"] == child.operation_id
    assert record["parent_operation_id"] == parent.operation_id


def test_file_logging_failure_degrades_to_stderr(tmp_path: Path) -> None:
    not_a_directory = tmp_path / "blocked"
    not_a_directory.write_text("file", encoding="utf-8")

    result = configure_logging(not_a_directory)
    logging.getLogger("joyread.test").info("still alive")

    assert result.file_logging_enabled is False
    assert result.log_file is None
    assert result.error is not None
    assert flush_logging()


def test_shutdown_drains_and_removes_runtime_handler(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    logging.getLogger("joyread.test").info("before shutdown")

    result = shutdown_logging()

    assert result.drained is True
    assert result.pending_records == 0
    assert logging.getLogger().handlers == []
    records = _read_json_records(tmp_path / LOG_FILE_NAME)
    assert any(record["message"] == "before shutdown" for record in records)
    assert any(record["event"] == "logging.runtime.stopping" for record in records)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")
def test_log_file_is_private_on_posix(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    assert flush_logging()

    mode = stat.S_IMODE((tmp_path / LOG_FILE_NAME).stat().st_mode)
    assert mode == 0o600


def test_process_exception_hook_persists_traceback_and_chains_previous_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forwarded: list[tuple[type[BaseException], BaseException]] = []

    def previous_hook(exc_type, exc_value, _traceback):  # noqa: ANN001
        forwarded.append((exc_type, exc_value))

    monkeypatch.setattr(sys, "excepthook", previous_hook)
    configure_logging(tmp_path)
    try:
        raise RuntimeError("hook boom")
    except RuntimeError:
        exc_info = sys.exc_info()
    sys.excepthook(*exc_info)
    assert flush_logging()

    record = _read_json_records(tmp_path / LOG_FILE_NAME)[-1]
    assert record["event"] == "process.unhandled_exception"
    assert record["level"] == "CRITICAL"
    assert record["error_type"] == "RuntimeError"
    assert "hook boom" in record["exception"]
    assert forwarded and forwarded[0][0] is RuntimeError

    shutdown_logging()
    assert sys.excepthook is previous_hook


def test_shutdown_restores_the_previous_qt_message_handler(tmp_path: Path) -> None:
    from PySide6 import QtCore

    received: list[str] = []

    def previous_handler(_kind, _context, message):  # noqa: ANN001
        received.append(message)

    original = QtCore.qInstallMessageHandler(previous_handler)
    try:
        configure_logging(tmp_path)
        shutdown_logging()
        QtCore.qWarning("after joyread logging")
        assert received == ["after joyread logging"]
    finally:
        QtCore.qInstallMessageHandler(original)


def test_emergency_log_is_unique_private_and_redacted(tmp_path: Path) -> None:
    configure_early_logging()
    try:
        raise RuntimeError("failed at /Users/alice/private.cbz password=hunter2")
    except RuntimeError as error:
        first = write_emergency_log(tmp_path, "launch.failed", "startup failed", error=error)
        second = write_emergency_log(tmp_path, "launch.failed", "startup failed", error=error)

    assert first is not None and second is not None and first != second
    payload = _read_json_records(first)[0]
    assert payload["event"] == "launch.failed"
    assert payload["error_type"] == "RuntimeError"
    assert "/Users/alice" not in payload["exception"]
    assert "hunter2" not in payload["exception"]
    if os.name == "posix":
        assert stat.S_IMODE(first.stat().st_mode) == 0o600


def test_bounded_queue_reports_dropped_records_without_blocking_producer() -> None:
    entered = Event()
    release = Event()
    records: list[logging.LogRecord] = []

    class BlockingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)
            if getattr(record, "event", None) != "logging.queue_records_dropped":
                entered.set()
                release.wait(timeout=2)

    runtime = logging_service._AsyncLogRuntime((BlockingHandler(),), capacity=1)  # noqa: SLF001
    runtime.start()
    first = logging.LogRecord("test", logging.INFO, __file__, 1, "first", (), None)
    second = logging.LogRecord("test", logging.INFO, __file__, 2, "second", (), None)
    third = logging.LogRecord("test", logging.INFO, __file__, 3, "third", (), None)
    runtime.enqueue(first)
    assert entered.wait(timeout=1)
    runtime.enqueue(second)
    runtime.enqueue(third)
    assert runtime.dropped_records == 1

    release.set()
    result = runtime.stop(1.0)

    assert result.drained is True
    assert result.dropped_records == 1
    assert any(
        getattr(record, "event", None) == "logging.queue_records_dropped" for record in records
    )


def test_logging_shutdown_reports_timeout_instead_of_claiming_success() -> None:
    entered = Event()
    release = Event()

    class BlockingHandler(logging.Handler):
        def emit(self, _record: logging.LogRecord) -> None:
            entered.set()
            release.wait(timeout=2)

    runtime = logging_service._AsyncLogRuntime((BlockingHandler(),), capacity=1)  # noqa: SLF001
    runtime.start()
    runtime.enqueue(logging.LogRecord("test", logging.INFO, __file__, 1, "blocked", (), None))
    assert entered.wait(timeout=1)

    timed_out = runtime.stop(0.01)
    assert timed_out.drained is False

    release.set()
    drained = runtime.stop(1.0)
    assert drained.drained is True


def _read_json_records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
