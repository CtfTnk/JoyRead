"""Pure mathematical manga page layout decisions."""

from __future__ import annotations

import logging

from joyread.core.reader.models import (
    PageDraw,
    ReaderDirection,
    ReaderDisplayMode,
    ReaderFitMode,
    ReaderLayoutResult,
    ReaderLayoutSettings,
    RectF,
    SizeF,
)


logger = logging.getLogger(__name__)


class SmartLayoutEngine:
    """Selects reader layout from viewport and page dimensions only.

    The engine deliberately does not know about Qt, archive sessions, or image
    bytes. That keeps resize-triggered calculations cheap and deterministic.
    """

    WIDE_ASPECT_THRESHOLD = 1.6
    LETTERBOX_HEIGHT_RATIO_THRESHOLD = 0.20

    def __init__(self) -> None:
        # Layout runs on every viewport resize and page step; logging each pass
        # would flood the log. Track the last-emitted mode and only log on
        # transitions (single -> double, double -> wide-pan, etc.).
        self._last_logged_mode: ReaderDisplayMode | None = None

    def calculate(
        self,
        viewport_size: SizeF,
        page1_size: SizeF,
        page2_size: SizeF | None = None,
        settings: ReaderLayoutSettings | None = None,
        *,
        page1_index: int = 0,
        page2_index: int | None = None,
    ) -> ReaderLayoutResult:
        settings = settings or ReaderLayoutSettings()
        if not viewport_size.is_valid or not page1_size.is_valid:
            return ReaderLayoutResult(ReaderDisplayMode.SINGLE, 1.0, (), 0.0)

        if settings.direction == ReaderDirection.TOP_TO_BOTTOM:
            result = self._single_result(
                viewport_size,
                page1_size,
                settings,
                page1_index=page1_index,
                force_fit=ReaderFitMode.FIT_HEIGHT,
            )
            self._log_mode_transition(result.mode)
            return result

        wide = self._wide_pan_result_if_needed(
            viewport_size,
            page1_size,
            settings,
            page1_index=page1_index,
        )
        if wide is not None:
            self._log_mode_transition(wide.mode)
            return wide

        single = self._single_result(viewport_size, page1_size, settings, page1_index=page1_index)
        if (
            settings.always_one_page
            or page2_size is None
            or not page2_size.is_valid
            or page2_index is None
        ):
            self._log_mode_transition(single.mode)
            return single

        double = self._double_result(
            viewport_size,
            page1_size,
            page2_size,
            settings,
            page1_index=page1_index,
            page2_index=page2_index,
        )
        chosen = double if double.used_area > single.used_area else single
        self._log_mode_transition(chosen.mode)
        return chosen

    def calculate_vertical(
        self,
        viewport_size: SizeF,
        pages: tuple[tuple[int, SizeF], ...],
        settings: ReaderLayoutSettings,
        *,
        anchor_index: int,
        scroll_y: float,
    ) -> ReaderLayoutResult:
        if not viewport_size.is_valid or not pages:
            return ReaderLayoutResult(ReaderDisplayMode.SINGLE, 1.0, (), 0.0)
        self._log_mode_transition(ReaderDisplayMode.SINGLE)

        page_sizes = {index: size for index, size in pages if size.is_valid}
        anchor_page = page_sizes.get(anchor_index)
        if anchor_page is None:
            return ReaderLayoutResult(ReaderDisplayMode.SINGLE, 1.0, (), 0.0)

        gap = float(settings.page_spacing if settings.vertical_custom_enabled else 0)
        draws: list[PageDraw] = []
        used_area = 0.0
        rects = _vertical_page_rects(viewport_size, page_sizes, settings, anchor_index, scroll_y, gap)
        for page_index in sorted(page_sizes):
            rect = rects[page_index]
            draws.append(PageDraw(page_index, rect))
            used_area += rect.width * rect.height
        anchor_rect = rects[anchor_index]
        return ReaderLayoutResult(
            mode=ReaderDisplayMode.SINGLE,
            scale=anchor_rect.height / anchor_page.height,
            page_draws=tuple(draws),
            used_area=used_area,
        )

    def _wide_pan_result_if_needed(
        self,
        viewport: SizeF,
        page: SizeF,
        settings: ReaderLayoutSettings,
        *,
        page1_index: int,
    ) -> ReaderLayoutResult | None:
        if page.width / page.height <= self.WIDE_ASPECT_THRESHOLD:
            return None

        fit_screen_scale = min(viewport.width / page.width, viewport.height / page.height)
        fit_screen_height = page.height * fit_screen_scale
        vertical_letterbox = max(0.0, viewport.height - fit_screen_height)
        if vertical_letterbox <= viewport.height * self.LETTERBOX_HEIGHT_RATIO_THRESHOLD:
            return None

        scale = viewport.height / page.height
        draw_width = page.width * scale
        rect = RectF(
            x=(viewport.width - draw_width) / 2.0,
            y=0.0,
            width=draw_width,
            height=viewport.height,
        )
        overflow = max(0.0, draw_width - viewport.width)
        # Store pan as an offset added to the centered rect. Negative pans move
        # the wide page left, positive pans move it right.
        return ReaderLayoutResult(
            mode=ReaderDisplayMode.WIDE_PAN,
            scale=scale,
            page_draws=(PageDraw(page1_index, rect),),
            used_area=viewport.width * viewport.height,
            pan_min_x=-(overflow / 2.0),
            pan_max_x=overflow / 2.0,
        )

    def _single_result(
        self,
        viewport: SizeF,
        page: SizeF,
        settings: ReaderLayoutSettings,
        *,
        page1_index: int,
        force_fit: ReaderFitMode | None = None,
    ) -> ReaderLayoutResult:
        fit_mode = force_fit or _effective_fit_mode(settings)
        scale = _scale_for_mode(viewport, page, fit_mode)
        width = page.width * scale
        height = page.height * scale
        rect = RectF(
            x=(viewport.width - width) / 2.0,
            y=(viewport.height - height) / 2.0,
            width=width,
            height=height,
        )
        return ReaderLayoutResult(
            mode=ReaderDisplayMode.SINGLE,
            scale=scale,
            page_draws=(PageDraw(page1_index, rect),),
            used_area=width * height,
        )

    def _double_result(
        self,
        viewport: SizeF,
        page1: SizeF,
        page2: SizeF,
        settings: ReaderLayoutSettings,
        *,
        page1_index: int,
        page2_index: int,
    ) -> ReaderLayoutResult:
        page2_width_at_page1_height = page2.width * (page1.height / page2.height)
        combined = SizeF(page1.width + page2_width_at_page1_height, page1.height)
        scale = _scale_for_mode(viewport, combined, _effective_fit_mode(settings))
        page1_width = page1.width * scale
        page2_width = page2_width_at_page1_height * scale
        height = page1.height * scale
        left = (viewport.width - (page1_width + page2_width)) / 2.0
        top = (viewport.height - height) / 2.0

        widths = {page1_index: page1_width, page2_index: page2_width}
        ordered_indexes = sorted(
            widths,
            reverse=settings.direction == ReaderDirection.RIGHT_TO_LEFT,
        )
        left_index, right_index = ordered_indexes
        left_width = widths[left_index]
        right_width = widths[right_index]
        draws = (
            PageDraw(left_index, RectF(left, top, left_width, height)),
            PageDraw(right_index, RectF(left + left_width, top, right_width, height)),
        )
        return ReaderLayoutResult(
            mode=ReaderDisplayMode.DOUBLE,
            scale=scale,
            page_draws=draws,
            used_area=(page1_width * height) + (page2_width * height),
        )

    def _log_mode_transition(self, mode: ReaderDisplayMode) -> None:
        if mode == self._last_logged_mode:
            return
        logger.debug("Layout mode -> %s", mode.value)
        self._last_logged_mode = mode


def _effective_fit_mode(settings: ReaderLayoutSettings) -> ReaderFitMode:
    if settings.custom_enabled and settings.fit_mode != ReaderFitMode.AUTO:
        return settings.fit_mode
    return ReaderFitMode.FIT_PAGE


def _scale_for_mode(viewport: SizeF, content: SizeF, fit_mode: ReaderFitMode) -> float:
    if fit_mode == ReaderFitMode.FIT_HEIGHT:
        return viewport.height / content.height
    if fit_mode == ReaderFitMode.FIT_WIDTH:
        return viewport.width / content.width
    return min(viewport.width / content.width, viewport.height / content.height)


def _vertical_page_rects(
    viewport: SizeF,
    page_sizes: dict[int, SizeF],
    settings: ReaderLayoutSettings,
    anchor_index: int,
    scroll_y: float,
    gap: float,
) -> dict[int, RectF]:
    rects: dict[int, RectF] = {}
    heights: dict[int, float] = {}
    widths: dict[int, float] = {}
    for page_index, page in page_sizes.items():
        if settings.vertical_custom_enabled and settings.vertical_fit_width:
            scale = viewport.width / page.width
            widths[page_index] = viewport.width
            heights[page_index] = page.height * scale
        else:
            target_height = viewport.height * _vertical_zoom(settings)
            scale = target_height / page.height
            widths[page_index] = page.width * scale
            heights[page_index] = target_height

    indexes = sorted(page_sizes)
    anchor_position = indexes.index(anchor_index)
    y_by_index = {anchor_index: scroll_y}

    y = scroll_y
    for page_index in indexes[anchor_position + 1 :]:
        previous_index = indexes[indexes.index(page_index) - 1]
        y += heights[previous_index] + gap
        y_by_index[page_index] = y

    y = scroll_y
    for page_index in reversed(indexes[:anchor_position]):
        y -= heights[page_index] + gap
        y_by_index[page_index] = y

    for page_index in indexes:
        width = widths[page_index]
        rects[page_index] = RectF(
            x=(viewport.width - width) / 2.0,
            y=y_by_index[page_index],
            width=width,
            height=heights[page_index],
        )
    return rects


def _vertical_zoom(settings: ReaderLayoutSettings) -> float:
    if not settings.vertical_custom_enabled:
        return 1.0
    return max(25, min(200, int(settings.vertical_zoom_percent))) / 100.0
