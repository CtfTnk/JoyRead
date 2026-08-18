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
    """The companion is scaled to the primary's height before the pair is fit.

    Uses a companion that is shorter than the primary but still narrow enough
    to be pairable -- a 2:1 page is excluded from spreads outright, so it
    cannot exercise this geometry.
    """

    result = SmartLayoutEngine().calculate(
        SizeF(2500, 1000),
        SizeF(500, 1000),
        SizeF(1000, 800),
        ReaderLayoutSettings(direction=ReaderDirection.LEFT_TO_RIGHT),
        page1_index=10,
        page2_index=11,
    )

    assert result.mode == ReaderDisplayMode.DOUBLE
    assert result.scale == pytest.approx(1.0)
    assert result.page_draws[0].rect.width == pytest.approx(500)
    # 1000 wide at 800 tall, normalised to the primary's 1000 height.
    assert result.page_draws[1].rect.width == pytest.approx(1250)


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


def test_always_one_page_is_ignored_when_custom_disabled() -> None:
    # The horizontal "Always one page" setting only takes effect while the
    # master "Enable Custom" toggle is on. With custom off, the layout
    # engine should pick the double-spread regardless of the orphaned flag.
    result = SmartLayoutEngine().calculate(
        SizeF(1600, 900),
        SizeF(600, 900),
        SizeF(600, 900),
        ReaderLayoutSettings(custom_enabled=False, always_one_page=True),
        page1_index=0,
        page2_index=1,
    )

    assert result.mode == ReaderDisplayMode.DOUBLE


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


def _page(aspect: float, height: float = 1000.0) -> SizeF:
    return SizeF(aspect * height, height)


def _mode(viewport_aspect: float, a1: float, a2: float, sticky=None):  # noqa: ANN001, ANN201
    return SmartLayoutEngine().calculate(
        SizeF(viewport_aspect * 900.0, 900.0),
        _page(a1),
        _page(a2),
        ReaderLayoutSettings(direction=ReaderDirection.LEFT_TO_RIGHT),
        page1_index=0,
        page2_index=1,
        sticky_mode=sticky,
    ).mode


def test_a_landscape_companion_is_not_shrunk_to_join_a_portrait_page() -> None:
    """The motivating defect: pairing won on total area while making a page
    that would have filled the screen alone noticeably smaller."""

    assert _mode(16 / 9, 0.667, 1.5) == ReaderDisplayMode.SINGLE


def test_two_portrait_pages_still_pair() -> None:
    assert _mode(16 / 9, 0.667, 0.667) == ReaderDisplayMode.DOUBLE
    assert _mode(4 / 3, 0.667, 0.667) == ReaderDisplayMode.DOUBLE


def test_the_pairing_decision_does_not_depend_on_which_page_is_primary() -> None:
    """The old rule compared only against the primary, so the same two pages
    paired or not depending on which one arrived first."""

    for a1, a2 in ((0.667, 1.5), (0.667, 0.667), (0.9, 0.55), (1.2, 1.2)):
        assert _mode(16 / 9, a1, a2) == _mode(16 / 9, a2, a1), f"asymmetric at {a1},{a2}"


def test_narrowing_the_window_never_creates_a_pairing() -> None:
    """A narrower viewport tightens the budget, so the risk of over-pairing
    lies with wide windows, never with narrow ones."""

    for a1, a2 in ((0.667, 0.667), (0.667, 1.0), (0.9, 0.9), (0.5, 1.2)):
        paired_at = [v / 10 for v in range(9, 26) if _mode(v / 10, a1, a2) == ReaderDisplayMode.DOUBLE]
        if not paired_at:
            continue
        narrowest = min(paired_at)
        for viewport in (v / 10 for v in range(9, 26)):
            if viewport < narrowest:
                assert _mode(viewport, a1, a2) == ReaderDisplayMode.SINGLE, (
                    f"a narrower window ({viewport}) paired where {narrowest} did not"
                )


def test_a_wide_page_is_never_a_companion_however_well_it_would_fit() -> None:
    """Excluded on aspect alone. In a very wide window the shrink budget would
    happily accept it, which would make the rule depend on the window rather
    than on the page."""

    assert _mode(2.4, 0.6, 1.7) == ReaderDisplayMode.SINGLE
    assert _mode(2.4, 1.7, 0.6) == ReaderDisplayMode.SINGLE


def test_a_pair_on_screen_survives_a_resize_past_the_threshold() -> None:
    """Without hysteresis, dragging a window edge near the boundary flips the
    layout back and forth."""

    a1 = a2 = 0.62
    boundary = next(
        v / 100 for v in range(90, 260) if _mode(v / 100, a1, a2) == ReaderDisplayMode.DOUBLE
    )
    just_below = boundary - 0.03
    assert _mode(just_below, a1, a2) == ReaderDisplayMode.SINGLE

    assert _mode(just_below, a1, a2, sticky=ReaderDisplayMode.DOUBLE) == ReaderDisplayMode.DOUBLE


def test_hysteresis_still_gives_up_once_the_pair_is_clearly_too_small() -> None:
    assert (
        _mode(0.95, 0.667, 1.4, sticky=ReaderDisplayMode.DOUBLE) == ReaderDisplayMode.SINGLE
    )


def test_the_wide_page_veto_stays_out_of_fit_height() -> None:
    """The wide-page veto belongs to the auto path. FIT_WIDTH/FIT_HEIGHT keep
    the original aggregate-area rule whole -- a veto applied before the fit
    branch would quietly re-scope a change that is meant to leave them
    alone."""

    result = SmartLayoutEngine().calculate(
        SizeF(2000, 1000),
        SizeF(600, 1000),
        SizeF(1700, 1000),
        ReaderLayoutSettings(
            direction=ReaderDirection.LEFT_TO_RIGHT,
            custom_enabled=True,
            fit_mode=ReaderFitMode.FIT_HEIGHT,
        ),
        page1_index=0,
        page2_index=1,
    )

    assert result.mode == ReaderDisplayMode.DOUBLE


def test_a_page_at_the_wide_threshold_is_still_pairable() -> None:
    """The veto exists to protect pages that would wide-pan alone, so it has
    to draw its line in the same place `_wide_pan_result_if_needed` does.
    A stricter comparison here strands the threshold itself: no wide-pan, and
    no spread either."""

    assert _mode(4.0, 0.6, SmartLayoutEngine.WIDE_ASPECT_THRESHOLD) == ReaderDisplayMode.DOUBLE
    assert _mode(4.0, 0.6, SmartLayoutEngine.WIDE_ASPECT_THRESHOLD + 0.01) == ReaderDisplayMode.SINGLE
