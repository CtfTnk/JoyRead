"""The seam that lets Core services log a correlated operation without
importing Infrastructure.

``joyread.core.operation_logging`` holds the Qt-free half of what used to be
bundled into ``joyread.infrastructure.logging.logging_service`` (which
imports PySide6 at module scope for its Qt message-handler bridge). Before
this split, no Core module could reach ``operation_scope``/``log_event``
without violating "Core has no PySide imports", so Core services duplicated
the pattern by hand instead. These tests pin that the seam exists, is real
delegation rather than a diverged copy, and stays Qt-free.
"""

from __future__ import annotations

import re
from pathlib import Path

from joyread.core import operation_logging
from joyread.core.operation_context import bind_operation, create_operation
from joyread.infrastructure.logging import logging_service


SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "joyread"


def test_operation_logging_module_has_no_qt_import() -> None:
    """Static, not import-time: PySide6 may already be loaded by the time this
    test runs (other modules import it), so checking sys.modules would not
    prove this module doesn't need it."""

    text = (SOURCE_ROOT / "core" / "operation_logging.py").read_text(encoding="utf-8")
    qt_import = re.compile(r"^\s*(?:from|import)\s+(?:PySide6|PyQt\d?)\b", re.MULTILINE)
    assert not qt_import.search(text)


def test_infrastructure_reexports_are_the_same_functions_not_copies() -> None:
    assert logging_service.log_event is operation_logging.log_event
    assert logging_service.operation_scope is operation_logging.operation_scope
    assert logging_service.log_timed_block is operation_logging.log_timed_block
    assert logging_service.describe_callback is operation_logging.describe_callback
    assert logging_service.get_logger is operation_logging.get_logger


def test_core_operation_scope_emits_a_correlated_terminal_event(caplog) -> None:  # noqa: ANN001
    import logging

    logger = logging.getLogger("joyread.core.operation_logging.test")
    with caplog.at_level(logging.INFO, logger=logger.name):
        with operation_logging.operation_scope(logger, "core.example") as operation:
            assert operation.category == "core"

    events = [record.__dict__.get("event") for record in caplog.records]
    assert "core.example.started" in events
    assert "core.example.finished" in events


def test_operation_scope_honors_an_already_bound_operation() -> None:
    import logging

    logger = logging.getLogger("joyread.core.operation_logging.test.parent")
    parent = create_operation("core.parent", category="core")
    with bind_operation(parent):
        with operation_logging.operation_scope(logger, "core.child") as child:
            assert child.parent_operation_id == parent.operation_id
