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

    #: Auto-mode pairing budget, expressed as the fraction of its solo size a
    #: page is still allowed to keep once a companion joins it.
    #:
    #: The old rule paired whenever the two pages together covered more screen
    #: than the primary alone. That is an aggregate test, and it produced two
    #: defects. It was *asymmetric* -- it compared only against the primary, so
    #: the same pair paired or not depending on which page arrived first -- and
    #: it happily shrank a page that would have filled the screen alone, as
    #: long as the total went up. A landscape page beside a portrait page is
    #: the case users notice.
    #:
    #: Because both pages are scaled to a common height, they shrink by the
    #: same factor, so "how much does pairing cost each page" is a single
    #: number. Comparing it to a floor is symmetric by construction (the
    #: combined aspect is the sum of the two, and addition commutes) rather
    #: than symmetric by taking a max.
    PAIR_SHRINK_FLOOR = 0.85

    #: Release threshold for the same measure. Resizing across a single
    #: boundary would flip the layout back and forth, so a pair already on
    #: screen is kept until it is meaningfully worse than the floor. Applies
    #: only while the same two pages stay on screen -- see ``sticky_mode``.
    PAIR_SHRINK_RELEASE = 0.78

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
        sticky_mode: ReaderDisplayMode | None = None,
    ) -> ReaderLayoutResult:
        """Choose a layout for one or two pages.

        ``sticky_mode`` is the mode currently on screen **for these same
        pages**, or ``None``. It exists only to damp the pairing decision
        across a resize, and the caller must not pass a mode carried over from
        a different spread: doing so would make the choice depend on what was
        read before it, which is the order-dependence this engine is
        deliberately free of.
        """

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
            _effective_always_one_page(settings)
            or page2_size is None
            or not page2_size.is_valid
            or page2_index is None
        ):
            self._log_mode_transition(single.mode)
            return single

        if _effective_fit_mode(settings) != ReaderFitMode.FIT_PAGE:
            # FIT_WIDTH / FIT_HEIGHT keep the original aggregate-area rule,
            # wide-page veto included. The shrink measure below is derived from
            # fit-inside scaling and does not describe what those modes do, so
            # both new rules are scoped out of them rather than one of them
            # leaking in.
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

        # Both auto-mode rules read only the three aspect ratios, so they run
        # before the spread geometry is built and skip it entirely on reject --
        # this path runs on every resize tick.
        if not self._pairing_allowed(page1_size, page2_size):
            self._log_mode_transition(single.mode)
            return single
        if not self._pair_is_worth_it(
            viewport_size, page1_size, page2_size, sticky_mode=sticky_mode
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
        self._log_mode_transition(double.mode)
        return double

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

    def _pairing_allowed(self, page1: SizeF, page2: SizeF) -> bool:
        """Reject a spread containing a page wide enough to stand alone.

        Checked on aspect alone, not on how the page happens to fit the current
        window, and applied to both sides. A page this wide is usually a
        double-page scan already; pairing it would both shrink it and deny it
        the wide-pan treatment it gets when it is the primary, so the same
        image would render completely differently depending on which side of a
        page turn it landed on.

        Deliberately explicit rather than left to the shrink budget below. The
        budget would reject most of these anyway, but not all: in a very wide
        window a 1.7-aspect page pairs at almost no cost, and the rule would
        then depend on the window rather than on the page.

        The comparison mirrors `_wide_pan_result_if_needed`'s exactly, so the
        threshold itself is pairable. A page *at* the threshold never wide-pans,
        and vetoing it here would leave it with neither treatment -- shrunk into
        a spread it was excluded from for a benefit it does not receive.
        """

        return (
            _aspect(page1) <= self.WIDE_ASPECT_THRESHOLD
            and _aspect(page2) <= self.WIDE_ASPECT_THRESHOLD
        )

    def _pair_is_worth_it(
        self,
        viewport: SizeF,
        page1: SizeF,
        page2: SizeF,
        *,
        sticky_mode: ReaderDisplayMode | None,
    ) -> bool:
        """Whether pairing costs each page less than the allowed shrink."""

        floor = (
            self.PAIR_SHRINK_RELEASE
            if sticky_mode == ReaderDisplayMode.DOUBLE
            else self.PAIR_SHRINK_FLOOR
        )
        return _pair_shrink(viewport, page1, page2) >= floor

    def _log_mode_transition(self, mode: ReaderDisplayMode) -> None:
        if mode == self._last_logged_mode:
            return
        logger.debug("Layout mode -> %s", mode.value)
        self._last_logged_mode = mode


def _aspect(size: SizeF) -> float:
    return size.width / size.height


def _pair_shrink(viewport: SizeF, page1: SizeF, page2: SizeF) -> float:
    """Fraction of its solo drawn size each page keeps when the two are paired.

    Under fit-inside scaling the drawn height of content with aspect ``a`` is
    ``H * min(1, V / a)`` where ``V`` is the viewport aspect. Two pages laid
    side by side share a height, so their combined aspect is exactly the sum of
    theirs -- and both shrink by the same factor, which is what makes this one
    number rather than two.

    The worst-affected page is the one that had the most to lose, hence the
    ``max`` over the solo factors.
    """

    viewport_aspect = _aspect(viewport)
    a1 = _aspect(page1)
    a2 = _aspect(page2)
    paired = min(1.0, viewport_aspect / (a1 + a2))
    solo = max(min(1.0, viewport_aspect / a1), min(1.0, viewport_aspect / a2))
    return paired / solo


def _effective_fit_mode(settings: ReaderLayoutSettings) -> ReaderFitMode:
    if settings.custom_enabled and settings.fit_mode != ReaderFitMode.AUTO:
        return settings.fit_mode
    return ReaderFitMode.FIT_PAGE


def _effective_always_one_page(settings: ReaderLayoutSettings) -> bool:
    # ``always_one_page`` is a horizontal custom setting — turning the
    # "Enable Custom" master switch off must neutralise it, otherwise the
    # value persists invisibly behind a greyed-out row and forces single
    # page mode even though the user thinks they're back on auto-spreads.
    return settings.custom_enabled and settings.always_one_page


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
