"""Tests for the comprehensive logging foundation."""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

import pytest

from joyread.infrastructure.logging.logging_service import (
    LOG_ENV_VAR,
    LOG_FILE_BACKUP_COUNT,
    LOG_FILE_MAX_BYTES,
    LOG_FILE_NAME,
    configure_logging,
    describe_callback,
    get_logger,
    log_timed_block,
)


@pytest.fixture(autouse=True)
def _reset_root_logger():
    """Drop handlers installed by previous tests so each run starts clean."""

    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    for handler in saved:
        root.removeHandler(handler)
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    for handler in saved:
        root.addHandler(handler)
    root.setLevel(saved_level)


def test_configure_logging_installs_rotating_file_handler(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    root = logging.getLogger()

    rotating = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rotating) == 1
    handler = rotating[0]
    assert Path(handler.baseFilename) == tmp_path / LOG_FILE_NAME
    assert handler.maxBytes == LOG_FILE_MAX_BYTES
    assert handler.backupCount == LOG_FILE_BACKUP_COUNT


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
    # caplog cannot observe the warning directly because configure_logging
    # removes the root handlers caplog installed; verify via the on-disk log
    # file instead. Flush the rotating handler so the line is on disk.
    for handler in logging.getLogger().handlers:
        handler.flush()
    contents = (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert logging.getLogger().level == logging.INFO
    assert "loud" in contents and "WARNING" in contents


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    configure_logging(tmp_path)
    rotating = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    stream = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(rotating) == 1
    assert len(stream) == 1


def test_configure_logging_writes_to_file(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    logging.getLogger("joyread.test").info("hello from joyread")
    for handler in logging.getLogger().handlers:
        handler.flush()
    log_path = tmp_path / LOG_FILE_NAME
    assert log_path.exists()
    contents = log_path.read_text(encoding="utf-8")
    assert "hello from joyread" in contents
    assert "joyread.test" in contents


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


def test_log_timed_block_emits_done_when_block_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = get_logger("joyread.facade.timed_raises")
    with caplog.at_level(logging.DEBUG, logger="joyread.facade.timed_raises"):
        with pytest.raises(RuntimeError):
            with log_timed_block(logger, "import-batch"):
                raise RuntimeError("boom")
    messages = [record.getMessage() for record in caplog.records]
    # Both bracket lines must fire even though the block raised: that's the
    # whole point of putting the "done" log inside ``finally``.
    assert any(m == "import-batch start" for m in messages)
    assert any(m.startswith("import-batch done in ") for m in messages)


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
    for handler in logging.getLogger().handlers:
        handler.flush()
    contents = (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "simulated qt warning" in contents
    assert "joyread.qt" in contents
    assert "WARNING" in contents
