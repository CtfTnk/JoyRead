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
from joyread.ui.widgets import reader_canvas as reader_canvas_module


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


def _prepared(
    page_index: int,
    size: tuple[int, int] = (4, 4),
    color: str = "#ff0000",
) -> PreparedReaderPage[QImage]:
    frame = QImage(size[0], size[1], QImage.Format.Format_RGB32)
    frame.fill(QColor(color))
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


def test_canvas_slides_between_ready_spreads_and_releases_old_pixmaps(canvas: ReaderCanvas) -> None:
    canvas.resize(200, 300)
    canvas.set_layout_result(_layout_with(0))
    canvas.set_page_frame(_prepared(0))
    source = canvas.capture_page_slide_frame()

    assert source is not None

    canvas.set_layout_result(_layout_with(1))
    canvas.set_page_frame(_prepared(1))

    assert canvas.start_page_slide(source, incoming_from_right=True)
    assert canvas.is_page_slide_active
    assert set(canvas._slide_source.pixmaps) == {0}
    assert set(canvas._slide_target.pixmaps) == {1}
    assert canvas._slide_offset_x == 200.0

    canvas._finish_page_slide()

    assert not canvas.is_page_slide_active
    assert set(canvas._pixmaps) == {1}


def test_canvas_skips_slide_when_target_spread_is_not_ready(canvas: ReaderCanvas) -> None:
    canvas.resize(200, 300)
    canvas.set_layout_result(_layout_with(0))
    canvas.set_page_frame(_prepared(0))
    source = canvas.capture_page_slide_frame()

    assert source is not None

    canvas.set_layout_result(_layout_with(1))

    assert not canvas.start_page_slide(source, incoming_from_right=True)
    assert not canvas.is_page_slide_active
    assert canvas._spinner_timer.isActive()


def test_canvas_slide_paints_incoming_spread_from_requested_side(canvas: ReaderCanvas) -> None:
    canvas.show()
    canvas.resize(200, 300)
    canvas.set_layout_result(_layout_with(0))
    canvas.set_page_frame(_prepared(0, color="#ff0000"))
    source = canvas.capture_page_slide_frame()
    assert source is not None
    canvas.set_layout_result(_layout_with(1))
    canvas.set_page_frame(_prepared(1, color="#0000ff"))
    assert canvas.start_page_slide(source, incoming_from_right=True)
    # Freeze timing so the rendered midpoint is deterministic.
    canvas._slide_animation.stop()
    canvas._set_slide_progress(0.5)

    midpoint = canvas.grab().toImage()

    assert midpoint.pixelColor(50, 150) == QColor("#ff0000")
    assert midpoint.pixelColor(150, 150) == QColor("#0000ff")


def test_canvas_slide_cancels_for_rapid_input_and_resize(canvas: ReaderCanvas) -> None:
    canvas.show()
    canvas.resize(200, 300)
    canvas.set_layout_result(_layout_with(0))
    canvas.set_page_frame(_prepared(0))
    source = canvas.capture_page_slide_frame()
    assert source is not None
    canvas.set_layout_result(_layout_with(1))
    canvas.set_page_frame(_prepared(1))
    assert canvas.start_page_slide(source, incoming_from_right=False)
    assert canvas._slide_offset_x == -200.0

    canvas.cancel_page_slide()

    assert not canvas.is_page_slide_active
    assert canvas._layout_result == _layout_with(1)

    source = canvas.capture_page_slide_frame()
    assert source is not None
    canvas.set_layout_result(_layout_with(2))
    canvas.set_page_frame(_prepared(2))
    assert canvas.start_page_slide(source, incoming_from_right=True)

    canvas.resize(220, 300)

    assert not canvas.is_page_slide_active


def test_canvas_pan_slide_interpolates_from_the_previous_offset(canvas: ReaderCanvas) -> None:
    canvas.resize(200, 300)
    canvas.set_layout_result(_layout_with(0), pan_x=-100.0)
    canvas.set_page_frame(_prepared(0))

    assert canvas.start_pan_slide(-40.0)
    assert canvas.is_pan_slide_active
    # The committed pan is already the destination; only the painted offset
    # travels, so the glide starts back at where the page used to sit.
    canvas._slide_animation.stop()
    canvas._set_pan_slide_progress(0.0)
    assert canvas._effective_pan_x() == -40.0

    canvas._set_pan_slide_progress(0.5)
    assert canvas._effective_pan_x() == -70.0

    canvas._finish_pan_slide()

    assert not canvas.is_pan_slide_active
    assert canvas._effective_pan_x() == -100.0


def test_canvas_pan_slide_paints_the_page_at_the_interpolated_offset(canvas: ReaderCanvas) -> None:
    """The midpoint of a glide must actually render between the two offsets,
    not snap to the committed one."""

    canvas.show()
    canvas.resize(100, 300)
    # A page wider than the viewport is what wide-pan scrolls across.
    wide = ReaderLayoutResult(
        mode=ReaderDisplayMode.WIDE_PAN,
        scale=1.0,
        page_draws=(
            PageDraw(page_index=0, rect=RectF(x=0.0, y=0.0, width=200.0, height=300.0)),
        ),
        used_area=200.0 * 300.0,
        pan_min_x=-100.0,
        pan_max_x=0.0,
    )
    canvas.set_layout_result(wide, pan_x=-100.0)
    # Two-tone so the painted offset is observable: a uniform page would
    # fill the viewport identically at either end of the glide.
    frame = QImage(200, 300, QImage.Format.Format_RGB32)
    frame.fill(QColor("#0000ff"))
    for x in range(100, 200):
        for y in range(300):
            frame.setPixelColor(x, y, QColor("#ff0000"))
    canvas.set_page_frame(PreparedReaderPage(0, frame, (200, 300), (200, 300), generation=1))

    assert canvas.start_pan_slide(0.0)
    canvas._pan_slide_animation.stop()
    canvas._set_pan_slide_progress(0.5)

    assert canvas._effective_pan_x() == -50.0

    midpoint = canvas.grab().toImage()

    # At pan -50 the page spans x=-50..150, so the viewport straddles the
    # colour boundary. At the committed -100 it would be all red.
    assert midpoint.pixelColor(10, 150) == QColor("#0000ff")
    assert midpoint.pixelColor(90, 150) == QColor("#ff0000")


def test_canvas_pan_slide_is_skipped_when_the_offset_did_not_move(canvas: ReaderCanvas) -> None:
    canvas.resize(200, 300)
    canvas.set_layout_result(_layout_with(0), pan_x=-100.0)
    canvas.set_page_frame(_prepared(0))

    assert not canvas.start_pan_slide(-100.0)
    assert not canvas.is_pan_slide_active


def test_canvas_pan_slide_survives_a_sharper_frame_but_not_a_resize(canvas: ReaderCanvas) -> None:
    """A pan glide paints the live spread at an interpolated offset rather
    than frozen copies, so a replacement image can land mid-glide. A resize
    still invalidates the geometry it is travelling across."""

    canvas.show()
    canvas.resize(200, 300)
    canvas.set_layout_result(_layout_with(0), pan_x=-100.0)
    canvas.set_page_frame(_prepared(0))
    assert canvas.start_pan_slide(-40.0)

    canvas.set_page_frame(_prepared(0, size=(8, 8), color="#00ff00"))

    assert canvas.is_pan_slide_active

    canvas.resize(220, 300)

    assert not canvas.is_pan_slide_active


def test_canvas_page_slide_and_pan_slide_never_run_together(canvas: ReaderCanvas) -> None:
    canvas.resize(200, 300)
    canvas.set_layout_result(_layout_with(0), pan_x=-100.0)
    canvas.set_page_frame(_prepared(0))
    assert canvas.start_pan_slide(-40.0)
    source = canvas.capture_page_slide_frame()
    assert source is not None

    canvas.set_layout_result(_layout_with(1))
    canvas.set_page_frame(_prepared(1))

    assert canvas.start_page_slide(source, incoming_from_right=True)
    assert canvas.is_page_slide_active
    assert not canvas.is_pan_slide_active


def test_canvas_does_not_capture_incomplete_or_failed_spreads(canvas: ReaderCanvas) -> None:
    canvas.set_layout_result(_layout_with(0))
    assert canvas.capture_page_slide_frame() is None

    canvas.set_page_frame(_prepared(0))
    canvas.set_page_failed(0)

    assert canvas.capture_page_slide_frame() is None


def test_canvas_skips_duplicate_qimage_conversion(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(reader_canvas_module, "reader_perf_enabled", lambda: True)
    widget = ReaderCanvas()
    qtbot.addWidget(widget)
    widget.set_layout_result(_layout_with(0))
    prepared = _prepared(0, (12, 16))

    widget.set_page_frame(prepared)
    widget.set_page_frame(prepared)

    snapshot = widget.performance_snapshot()
    assert snapshot["pixmap_conversions"] == 1
    assert snapshot["pixmap_duplicate_skips"] == 1

    widget.reset_performance_measurements()
    reset = widget.performance_snapshot()
    assert reset["pixmap_conversions"] == 0
    assert reset["pixmap_duplicate_skips"] == 0


def test_canvas_converts_new_same_sized_qimage_and_clears_signature(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(reader_canvas_module, "reader_perf_enabled", lambda: True)
    widget = ReaderCanvas()
    qtbot.addWidget(widget)
    widget.set_layout_result(_layout_with(0))

    widget.set_page_frame(_prepared(0, (12, 16)))
    widget.set_page_frame(_prepared(0, (12, 16)))

    assert widget.performance_snapshot()["pixmap_conversions"] == 2
    assert 0 in widget._frame_signatures

    widget.set_page_failed(0)
    assert 0 not in widget._frame_signatures


def test_performance_heartbeat_timer_is_not_created_by_default(canvas: ReaderCanvas) -> None:
    assert canvas._perf_heartbeat_timer is None


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
