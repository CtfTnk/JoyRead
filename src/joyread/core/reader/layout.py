"""Pure mathematical manga page layout decisions."""

from __future__ import annotations

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


class SmartLayoutEngine:
    """Selects reader layout from viewport and page dimensions only.

    The engine deliberately does not know about Qt, archive sessions, or image
    bytes. That keeps resize-triggered calculations cheap and deterministic.
    """

    WIDE_ASPECT_THRESHOLD = 1.6
    LETTERBOX_HEIGHT_RATIO_THRESHOLD = 0.20

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
            return self._single_result(
                viewport_size,
                page1_size,
                settings,
                page1_index=page1_index,
                force_fit=ReaderFitMode.FIT_HEIGHT,
            )

        wide = self._wide_pan_result_if_needed(
            viewport_size,
            page1_size,
            settings,
            page1_index=page1_index,
        )
        if wide is not None:
            return wide

        single = self._single_result(viewport_size, page1_size, settings, page1_index=page1_index)
        if (
            settings.always_one_page
            or page2_size is None
            or not page2_size.is_valid
            or page2_index is None
        ):
            return single

        double = self._double_result(
            viewport_size,
            page1_size,
            page2_size,
            settings,
            page1_index=page1_index,
            page2_index=page2_index,
        )
        return double if double.used_area > single.used_area else single

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

        page_sizes = {index: size for index, size in pages if size.is_valid}
        anchor_page = page_sizes.get(anchor_index)
        if anchor_page is None:
            return ReaderLayoutResult(ReaderDisplayMode.SINGLE, 1.0, (), 0.0)

        zoom = _vertical_zoom(settings)
        target_height = viewport_size.height * zoom
        gap = float(settings.page_spacing if settings.vertical_custom_enabled else 0)
        step = target_height + gap
        draws: list[PageDraw] = []
        used_area = 0.0
        for page_index in sorted(page_sizes):
            page = page_sizes[page_index]
            scale = target_height / page.height
            width = page.width * scale
            rect = RectF(
                x=(viewport_size.width - width) / 2.0,
                y=scroll_y + ((page_index - anchor_index) * step),
                width=width,
                height=target_height,
            )
            draws.append(PageDraw(page_index, rect))
            used_area += width * target_height
        return ReaderLayoutResult(
            mode=ReaderDisplayMode.SINGLE,
            scale=target_height / anchor_page.height,
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


def _vertical_zoom(settings: ReaderLayoutSettings) -> float:
    if not settings.vertical_custom_enabled:
        return 1.0
    return max(25, min(200, int(settings.vertical_zoom_percent))) / 100.0
