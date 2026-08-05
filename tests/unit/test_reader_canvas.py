"""Tests for the reader canvas loading-indicator state machine."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage

from joyread.app.reader_page_pipeline import PreparedReaderPage
from joyread.core.reader import (
    PageDraw,
    ReaderDisplayMode,
    ReaderLayoutResult,
    RectF,
)
from joyread.ui.widgets.reader_canvas import ReaderCanvas
from joyread.ui.widgets.reader_canvas import _status_text_is_clipped, _wrapped_status_text_rect


def _layout_with(*page_indices: int) -> ReaderLayoutResult:
    draws = tuple(
        PageDraw(page_index=index, rect=RectF(x=0.0, y=0.0, width=200.0, height=300.0))
        for index in page_indices
    )
    return ReaderLayoutResult(
        mode=ReaderDisplayMode.SINGLE,
        scale=1.0,
        page_draws=draws,
        used_area=200.0 * 300.0 * len(draws),
    )


def _prepared(page_index: int, size: tuple[int, int] = (4, 4)) -> PreparedReaderPage[QImage]:
    frame = QImage(size[0], size[1], QImage.Format.Format_RGB32)
    frame.fill(QColor("#ff0000"))
    return PreparedReaderPage(page_index, frame, size, size, generation=1)


@pytest.fixture()
def canvas(qtbot):
    widget = ReaderCanvas()
    qtbot.addWidget(widget)
    return widget


def test_spinner_is_idle_until_a_layout_with_missing_pages_arrives(canvas: ReaderCanvas) -> None:
    # No layout yet ⇒ no spinner work pending.
    assert not canvas._spinner_timer.isActive()


def test_spinner_starts_when_layout_has_unloaded_pages(canvas: ReaderCanvas) -> None:
    canvas.set_layout_result(_layout_with(0, 1))

    assert canvas._spinner_timer.isActive()


def test_spinner_stops_when_every_drawn_page_has_a_pixmap(canvas: ReaderCanvas) -> None:
    canvas.set_layout_result(_layout_with(0))
    assert canvas._spinner_timer.isActive()

    canvas.set_page_frame(_prepared(0))

    assert not canvas._spinner_timer.isActive()


def test_spinner_keeps_running_until_every_visible_page_has_a_pixmap(canvas: ReaderCanvas) -> None:
    canvas.set_layout_result(_layout_with(0, 1))
    canvas.set_page_frame(_prepared(0))

    # Page 1 is still missing on a SPREAD: indicator must remain active so
    # the user can see *which* slot is still loading.
    assert canvas._spinner_timer.isActive()

    canvas.set_page_frame(_prepared(1))
    assert not canvas._spinner_timer.isActive()


def test_spinner_resumes_when_layout_advances_to_a_new_unloaded_page(canvas: ReaderCanvas) -> None:
    canvas.set_layout_result(_layout_with(0))
    canvas.set_page_frame(_prepared(0))
    assert not canvas._spinner_timer.isActive()

    # Navigating forward selects an unloaded page; the indicator must come
    # back without requiring an extra "loading" call on the canvas.
    canvas.set_layout_result(_layout_with(5))

    assert canvas._spinner_timer.isActive()


def test_failed_page_stops_spinner_and_failure_is_cleared_with_layout(canvas: ReaderCanvas) -> None:
    canvas.set_layout_result(_layout_with(3))
    assert canvas._spinner_timer.isActive()

    canvas.set_page_failed(3)

    assert not canvas._spinner_timer.isActive()
    assert canvas._failed_pages == {3}

    canvas.set_layout_result(None)
    canvas.set_layout_result(_layout_with(3))

    assert canvas._failed_pages == set()
    assert canvas._spinner_timer.isActive()


def test_spinner_phase_advances_when_timer_fires(canvas: ReaderCanvas) -> None:
    canvas.set_layout_result(_layout_with(0))
    initial_phase = canvas._spinner_phase

    canvas._tick_spinner()

    assert canvas._spinner_phase != initial_phase
    assert 0.0 <= canvas._spinner_phase < 360.0


def test_canvas_ignores_non_visible_page_images_and_prunes_old_pixmaps(canvas: ReaderCanvas) -> None:
    canvas.set_layout_result(_layout_with(0))

    canvas.set_page_frame(_prepared(1))
    assert canvas._pixmaps == {}

    canvas.set_page_frame(_prepared(0))
    assert set(canvas._pixmaps) == {0}

    canvas.set_layout_result(_layout_with(1))
    assert canvas._pixmaps == {}
    assert canvas._spinner_timer.isActive()


def test_canvas_keeps_resident_page_image_that_arrives_before_layout(canvas: ReaderCanvas) -> None:
    canvas.set_page_frame(_prepared(0))
    assert set(canvas._pixmaps) == {0}

    canvas.set_layout_result(_layout_with(0))

    assert set(canvas._pixmaps) == {0}
    assert not canvas._spinner_timer.isActive()


def test_canvas_replaces_existing_pixmap_with_newly_prepared_frame(canvas: ReaderCanvas) -> None:
    canvas.set_layout_result(_layout_with(0))
    canvas.set_page_frame(_prepared(0, (4, 4)))

    canvas.set_page_frame(_prepared(0, (12, 16)))

    replacement = canvas._pixmaps[0]
    assert replacement.width() == 12
    assert replacement.height() == 16


def test_canvas_clear_pages_drops_pixmaps_layout_and_spinner(canvas: ReaderCanvas) -> None:
    canvas.set_layout_result(_layout_with(0))
    assert canvas._spinner_timer.isActive()

    canvas.clear_pages()

    assert canvas._layout_result is None
    assert canvas._pixmaps == {}
    assert not canvas._spinner_timer.isActive()


def test_status_text_rect_wraps_long_reader_messages(canvas: ReaderCanvas) -> None:
    canvas.resize(320, 200)
    message = (
        "Could not load images because the archive is encrypted and no password "
        "was provided. Please enter the password to continue reading."
    )

    rect = _wrapped_status_text_rect(canvas.fontMetrics(), QRectF(canvas.rect()), message)

    assert rect.width() <= 320 * 0.72 + 1
    assert rect.height() > canvas.fontMetrics().height()


def test_status_text_tooltip_is_set_only_when_message_is_clipped(canvas: ReaderCanvas) -> None:
    canvas.resize(180, 80)
    message = "Could not load page 602: " + "very/long/path/" * 20

    canvas.set_status_text(message)

    assert _status_text_is_clipped(canvas.fontMetrics(), QRectF(canvas.rect()), message)
    assert canvas.toolTip() == message

    canvas.resize(800, 500)
    canvas.set_status_text("No readable pages.")

    assert canvas.toolTip() == ""
