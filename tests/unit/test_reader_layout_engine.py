from __future__ import annotations

import pytest

from joyread.core.reader import (
    ReaderDirection,
    ReaderDisplayMode,
    ReaderFitMode,
    ReaderLayoutSettings,
    SizeF,
    SmartLayoutEngine,
)


def test_wide_page_enters_wide_pan_when_letterbox_is_large() -> None:
    result = SmartLayoutEngine().calculate(
        SizeF(1000, 800),
        SizeF(2000, 1000),
    )

    assert result.mode == ReaderDisplayMode.WIDE_PAN
    assert result.scale == pytest.approx(0.8)
    assert result.pan_min_x < 0
    assert result.pan_max_x > 0


def test_wide_aspect_without_large_letterbox_stays_standard() -> None:
    result = SmartLayoutEngine().calculate(
        SizeF(1000, 600),
        SizeF(1700, 1000),
    )

    assert result.mode == ReaderDisplayMode.SINGLE


def test_double_page_wins_when_it_uses_more_screen_area() -> None:
    result = SmartLayoutEngine().calculate(
        SizeF(1600, 900),
        SizeF(600, 900),
        SizeF(600, 900),
        page1_index=0,
        page2_index=1,
    )

    assert result.mode == ReaderDisplayMode.DOUBLE
    assert [draw.page_index for draw in result.page_draws] == [1, 0]


def test_single_page_wins_when_double_spread_is_too_small() -> None:
    result = SmartLayoutEngine().calculate(
        SizeF(700, 900),
        SizeF(600, 900),
        SizeF(600, 900),
        page1_index=0,
        page2_index=1,
    )

    assert result.mode == ReaderDisplayMode.SINGLE


def test_double_page_normalizes_secondary_height_before_fit() -> None:
    result = SmartLayoutEngine().calculate(
        SizeF(2500, 1000),
        SizeF(500, 1000),
        SizeF(1000, 500),
        ReaderLayoutSettings(direction=ReaderDirection.LEFT_TO_RIGHT),
        page1_index=10,
        page2_index=11,
    )

    assert result.mode == ReaderDisplayMode.DOUBLE
    assert result.scale == pytest.approx(1.0)
    assert result.page_draws[0].rect.width == pytest.approx(500)
    assert result.page_draws[1].rect.width == pytest.approx(2000)


def test_custom_always_one_page_prevents_double_spread() -> None:
    result = SmartLayoutEngine().calculate(
        SizeF(1600, 900),
        SizeF(600, 900),
        SizeF(600, 900),
        ReaderLayoutSettings(custom_enabled=True, always_one_page=True),
        page1_index=0,
        page2_index=1,
    )

    assert result.mode == ReaderDisplayMode.SINGLE


def test_custom_fit_height_changes_single_scale() -> None:
    result = SmartLayoutEngine().calculate(
        SizeF(1000, 800),
        SizeF(200, 400),
        settings=ReaderLayoutSettings(custom_enabled=True, fit_mode=ReaderFitMode.FIT_HEIGHT),
    )

    assert result.mode == ReaderDisplayMode.SINGLE
    assert result.scale == pytest.approx(2.0)
