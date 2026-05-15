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


def test_double_page_draw_order_uses_archive_order_not_primary_order() -> None:
    right_to_left = SmartLayoutEngine().calculate(
        SizeF(1600, 900),
        SizeF(600, 900),
        SizeF(600, 900),
        ReaderLayoutSettings(direction=ReaderDirection.RIGHT_TO_LEFT),
        page1_index=1,
        page2_index=0,
    )
    left_to_right = SmartLayoutEngine().calculate(
        SizeF(1600, 900),
        SizeF(600, 900),
        SizeF(600, 900),
        ReaderLayoutSettings(direction=ReaderDirection.LEFT_TO_RIGHT),
        page1_index=1,
        page2_index=0,
    )

    assert [draw.page_index for draw in right_to_left.page_draws] == [1, 0]
    assert [draw.page_index for draw in left_to_right.page_draws] == [0, 1]


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


def test_top_to_down_single_page_uses_fit_height_not_fit_width() -> None:
    result = SmartLayoutEngine().calculate(
        SizeF(1000, 800),
        SizeF(200, 400),
        settings=ReaderLayoutSettings(direction=ReaderDirection.TOP_TO_BOTTOM),
    )

    assert result.mode == ReaderDisplayMode.SINGLE
    assert result.scale == pytest.approx(2.0)
    assert result.page_draws[0].rect.height == pytest.approx(800)


def test_vertical_stack_uses_gap_and_zoom_only_when_custom_enabled() -> None:
    engine = SmartLayoutEngine()
    pages = (
        (0, SizeF(200, 400)),
        (1, SizeF(200, 400)),
        (2, SizeF(200, 400)),
    )

    default = engine.calculate_vertical(
        SizeF(1000, 800),
        pages,
        ReaderLayoutSettings(direction=ReaderDirection.TOP_TO_BOTTOM),
        anchor_index=1,
        scroll_y=0,
    )
    custom = engine.calculate_vertical(
        SizeF(1000, 800),
        pages,
        ReaderLayoutSettings(
            direction=ReaderDirection.TOP_TO_BOTTOM,
            vertical_custom_enabled=True,
            page_spacing=20,
            vertical_zoom_percent=150,
        ),
        anchor_index=1,
        scroll_y=-100,
    )

    assert [draw.page_index for draw in default.page_draws] == [0, 1, 2]
    assert default.page_draws[1].rect.height == pytest.approx(800)
    assert default.page_draws[2].rect.y == pytest.approx(800)
    assert custom.page_draws[1].rect.height == pytest.approx(1200)
    assert custom.page_draws[1].rect.y == pytest.approx(-100)
    assert custom.page_draws[2].rect.y == pytest.approx(1120)


def test_vertical_stack_can_fit_width_when_custom_enabled() -> None:
    result = SmartLayoutEngine().calculate_vertical(
        SizeF(1000, 800),
        (
            (0, SizeF(200, 400)),
            (1, SizeF(500, 250)),
            (2, SizeF(250, 500)),
        ),
        ReaderLayoutSettings(
            direction=ReaderDirection.TOP_TO_BOTTOM,
            vertical_custom_enabled=True,
            vertical_fit_width=True,
            page_spacing=20,
            vertical_zoom_percent=50,
        ),
        anchor_index=1,
        scroll_y=10,
    )

    assert [draw.page_index for draw in result.page_draws] == [0, 1, 2]
    assert result.page_draws[1].rect.width == pytest.approx(1000)
    assert result.page_draws[1].rect.height == pytest.approx(500)
    assert result.page_draws[1].rect.x == pytest.approx(0)
    assert result.page_draws[1].rect.y == pytest.approx(10)
    assert result.page_draws[0].rect.y == pytest.approx(-2010)
    assert result.page_draws[2].rect.y == pytest.approx(530)
    assert result.scale == pytest.approx(2.0)
