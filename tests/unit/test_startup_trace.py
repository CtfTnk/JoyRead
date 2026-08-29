"""The startup trace has to be right before any of its numbers can be trusted."""

from __future__ import annotations

import logging

import pytest

from joyread.app import startup_trace


@pytest.fixture(autouse=True)
def _clean_trace():
    startup_trace.reset()
    yield
    startup_trace.reset()


def test_the_origin_is_recorded_at_import_not_at_first_mark() -> None:
    """``origin`` is the module's own import instant.

    If it were recorded lazily, every figure would be relative to whenever the
    first caller happened to ask, which is exactly the window being measured.
    """

    assert startup_trace.origin_epoch() > 0
    assert startup_trace.elapsed_ms() > 0


def test_marks_are_ordered_and_carry_their_own_stage() -> None:
    startup_trace.mark("first")
    startup_trace.mark("second")

    recorded = startup_trace.milestones()

    assert [milestone.name for milestone in recorded] == ["first", "second"]
    assert recorded[0].elapsed_ms <= recorded[1].elapsed_ms
    assert recorded[1].stage_ms == pytest.approx(
        recorded[1].elapsed_ms - recorded[0].elapsed_ms
    )


def test_the_first_observation_of_a_name_wins() -> None:
    """``create_application()`` is re-entrant.

    Tests and embedded callers build repeatedly against one QApplication, and a
    later re-entry must not overwrite the real startup timings.
    """

    first = startup_trace.mark("window_shown")
    repeat = startup_trace.mark("window_shown")

    assert first is not None
    assert repeat is None
    assert [milestone.name for milestone in startup_trace.milestones()] == ["window_shown"]


def test_flush_emits_each_milestone_exactly_once(caplog) -> None:  # noqa: ANN001
    """Flush is called at several stage boundaries and from the paint probe."""

    logger = logging.getLogger("joyread.test.startup_trace")
    startup_trace.mark("context_ready")

    with caplog.at_level(logging.INFO, logger=logger.name):
        assert startup_trace.flush_to_log(logger) == 1
        assert startup_trace.flush_to_log(logger) == 0
        startup_trace.mark("first_paint")
        assert startup_trace.flush_to_log(logger) == 1

    emitted = [record for record in caplog.records if record.name == logger.name]
    assert [record.milestone for record in emitted] == ["context_ready", "first_paint"]


def test_the_first_flushed_line_carries_the_origin_epoch(caplog) -> None:  # noqa: ANN001
    """The harness pairs this with its own spawn time.

    That difference is the PyInstaller loader plus interpreter init, which the
    process cannot observe from the inside.
    """

    logger = logging.getLogger("joyread.test.startup_trace.epoch")
    startup_trace.mark("origin")
    startup_trace.mark("qt_app_created")

    with caplog.at_level(logging.INFO, logger=logger.name):
        startup_trace.flush_to_log(logger)

    emitted = [record for record in caplog.records if record.name == logger.name]
    assert getattr(emitted[0], "origin_epoch", None) == startup_trace.origin_epoch()
    assert not hasattr(emitted[1], "origin_epoch")
    # Also rendered into the message: a secondary process exits before file
    # logging exists, and the early stderr handler emits text, not JSON.
    assert "origin_epoch=" in emitted[0].getMessage()
    assert "origin_epoch=" not in emitted[1].getMessage()


def test_flushed_records_are_structured_events(caplog) -> None:  # noqa: ANN001
    logger = logging.getLogger("joyread.test.startup_trace.fields")
    startup_trace.mark("resources_ready")

    with caplog.at_level(logging.INFO, logger=logger.name):
        startup_trace.flush_to_log(logger)

    record = next(record for record in caplog.records if record.name == logger.name)
    assert record.event == "startup.milestone"
    assert record.category == "process"
    assert record.milestone == "resources_ready"
    assert isinstance(record.elapsed_ms, float)
    assert isinstance(record.stage_ms, float)


def test_the_trace_module_stays_free_of_qt_and_joyread_imports() -> None:
    """Anything this module pulls in is cost paid before the origin it claims.

    It is imported as the first executable statement of ``app/main.py``
    precisely so that instant is early; a Qt or joyread dependency here would
    move the origin past the window being measured.
    """

    source = (
        __import__("pathlib").Path(startup_trace.__file__).read_text(encoding="utf-8")
    )
    assert "PySide6" not in source
    assert "import joyread" not in source
    assert "from joyread" not in source
