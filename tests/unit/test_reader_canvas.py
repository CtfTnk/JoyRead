"""Tests for the reader canvas loading-indicator state machine."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from joyread.core.reader import (
    PageDraw,
    ReaderDisplayMode,
    ReaderLayoutResult,
    ReaderPageImage,
    RectF,
)
from joyread.ui.widgets.reader_canvas import ReaderCanvas


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


def _png_bytes(size: tuple[int, int] = (4, 4)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "#ff0000").save(buffer, format="PNG")
    return buffer.getvalue()


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

    canvas.set_page_image(ReaderPageImage(page_index=0, image_bytes=_png_bytes(), dimensions=(4, 4)))

    assert not canvas._spinner_timer.isActive()


def test_spinner_keeps_running_until_every_visible_page_has_a_pixmap(canvas: ReaderCanvas) -> None:
    canvas.set_layout_result(_layout_with(0, 1))
    canvas.set_page_image(ReaderPageImage(page_index=0, image_bytes=_png_bytes(), dimensions=(4, 4)))

    # Page 1 is still missing on a SPREAD: indicator must remain active so
    # the user can see *which* slot is still loading.
    assert canvas._spinner_timer.isActive()

    canvas.set_page_image(ReaderPageImage(page_index=1, image_bytes=_png_bytes(), dimensions=(4, 4)))
    assert not canvas._spinner_timer.isActive()


def test_spinner_resumes_when_layout_advances_to_a_new_unloaded_page(canvas: ReaderCanvas) -> None:
    canvas.set_layout_result(_layout_with(0))
    canvas.set_page_image(ReaderPageImage(page_index=0, image_bytes=_png_bytes(), dimensions=(4, 4)))
    assert not canvas._spinner_timer.isActive()

    # Navigating forward selects an unloaded page; the indicator must come
    # back without requiring an extra "loading" call on the canvas.
    canvas.set_layout_result(_layout_with(5))

    assert canvas._spinner_timer.isActive()


def test_spinner_phase_advances_when_timer_fires(canvas: ReaderCanvas) -> None:
    canvas.set_layout_result(_layout_with(0))
    initial_phase = canvas._spinner_phase

    canvas._tick_spinner()

    assert canvas._spinner_phase != initial_phase
    assert 0.0 <= canvas._spinner_phase < 360.0
