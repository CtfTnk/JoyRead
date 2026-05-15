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
