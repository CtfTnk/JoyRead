"""Regression coverage for previously-silent except blocks.

The settings/progress lookups in :mod:`reader_shell`, the
:class:`DatabaseInterpreter` callback wrapper, and the :class:`TaskService`
``_Runnable`` all used to drop exceptions on the floor. The
``vertical_fit_width`` schema drift hid behind exactly this kind of silent
swallow, so each site must now emit a log line before falling back. These
tests pin that contract.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from joyread.core.reader.models import ReaderSettings
from joyread.infrastructure.qt_task_service import TaskService
from joyread.infrastructure.database.database_interpreter import (
    DatabaseInterpreter,
    DatabasePriority,
)


class _FailingLibrary:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get_reader_settings(self, _book_uuid: str) -> ReaderSettings | None:
        raise self._exc

    def get_progress(self, _book_uuid: str):  # noqa: ANN201 - duck-typed.
        raise self._exc


def _fake_context(library) -> SimpleNamespace:  # noqa: ANN001 - test stub.
    return SimpleNamespace(library_service=library)


def _fake_book(uuid: str) -> SimpleNamespace:
    return SimpleNamespace(uuid=uuid)


def test_reader_settings_for_book_logs_when_repo_raises(caplog: pytest.LogCaptureFixture) -> None:
    from joyread.ui.views.reader_shell import _reader_settings_for_book

    library = _FailingLibrary(sqlite3.OperationalError("no such column: vertical_fit_width"))
    context = _fake_context(library)
    book = _fake_book("book-42")

    with caplog.at_level(logging.ERROR, logger="joyread.ui.views.reader_shell"):
        settings = _reader_settings_for_book(context, book)

    assert settings == ReaderSettings()
    assert any(
        "book-42" in record.getMessage() and "vertical_fit_width" in record.getMessage()
        for record in caplog.records
    ), "Expected reader settings load failure to log book uuid and the missing column"


def test_reader_progress_for_book_logs_when_repo_raises(caplog: pytest.LogCaptureFixture) -> None:
    from joyread.ui.views.reader_shell import _reader_progress_for_book

    library = _FailingLibrary(sqlite3.OperationalError("disk image is malformed"))
    context = _fake_context(library)
    book = _fake_book("book-99")

    with caplog.at_level(logging.ERROR, logger="joyread.ui.views.reader_shell"):
        progress = _reader_progress_for_book(context, book)

    assert progress is None
    assert any("book-99" in record.getMessage() for record in caplog.records)


def test_database_interpreter_logs_callback_exception(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    interpreter = DatabaseInterpreter(tmp_path / "logging.sqlite3")
    try:
        future = interpreter.submit(
            lambda _connection: (_ for _ in ()).throw(
                sqlite3.OperationalError("no such column: vertical_fit_width")
            ),
            DatabasePriority.NORMAL,
        )
        with caplog.at_level(logging.ERROR, logger="joyread.infrastructure.database.database_interpreter"):
            with pytest.raises(sqlite3.OperationalError):
                future.result(timeout=5)
    finally:
        interpreter.close()

    assert any(
        isinstance(record.exc_info and record.exc_info[1], sqlite3.OperationalError)
        and "vertical_fit_width" in str(record.exc_info[1])
        for record in caplog.records
    ), "DB interpreter must log the exception before forwarding it on the future"


def test_task_service_logs_callback_exception(caplog: pytest.LogCaptureFixture) -> None:
    # QRunnable + QSignal cross-thread delivery requires a live application
    # event loop on the test (receiver) thread, otherwise on_failure never
    # fires under offscreen/headless tests. Use QApplication (not
    # QCoreApplication) so subsequent tests in the same session can reuse
    # ``QApplication.instance()`` for widget setup.
    import sys

    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv or ["test"])
    assert isinstance(app, QCoreApplication)

    service = TaskService(max_workers=1)

    failures: list[Exception] = []

    def boom() -> None:
        raise RuntimeError("simulated task failure")

    with caplog.at_level(logging.WARNING, logger="joyread.infrastructure.qt_task_service"):
        handle = service.submit("logging-test", boom, on_failure=failures.append)
        from time import perf_counter, sleep

        deadline = perf_counter() + 5.0
        while not failures and perf_counter() < deadline:
            app.processEvents()
            sleep(0.01)

    assert failures, "on_failure callback should have fired"
    assert handle.task_id.startswith("logging-test")
    assert any(
        getattr(record, "event", None) == "task.worker.failed"
        and record.levelno == logging.ERROR
        and getattr(record, "reason", None) == "simulated task failure"
        and record.exc_info is not None
        for record in caplog.records
    )

    service.shutdown()
