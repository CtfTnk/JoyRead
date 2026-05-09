"""Pure reader state models shared by services, ViewModels, and tests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReaderDisplayMode(StrEnum):
    WIDE_PAN = "wide_pan"
    SINGLE = "single"
    DOUBLE = "double"


class ReaderDirection(StrEnum):
    RIGHT_TO_LEFT = "right_to_left"
    LEFT_TO_RIGHT = "left_to_right"
    TOP_TO_BOTTOM = "top_to_bottom"


class ReaderFitMode(StrEnum):
    AUTO = "auto"
    FIT_HEIGHT = "fit_height"
    FIT_WIDTH = "fit_width"
    FIT_PAGE = "fit_page"


class ReaderTransitionMode(StrEnum):
    NONE = "none"
    SLIDE = "slide"


@dataclass(frozen=True)
class SizeF:
    width: float
    height: float

    @property
    def is_valid(self) -> bool:
        return self.width > 0 and self.height > 0


@dataclass(frozen=True)
class RectF:
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class PageDraw:
    page_index: int
    rect: RectF


@dataclass(frozen=True)
class ReaderLayoutSettings:
    direction: ReaderDirection = ReaderDirection.RIGHT_TO_LEFT
    custom_enabled: bool = False
    always_one_page: bool = False
    fit_mode: ReaderFitMode = ReaderFitMode.AUTO
    vertical_custom_enabled: bool = False
    page_spacing: int = 0
    vertical_zoom_percent: int = 100
    transition_mode: ReaderTransitionMode = ReaderTransitionMode.NONE
    spread_offset: int = 0


@dataclass(frozen=True)
class ReaderLayoutResult:
    mode: ReaderDisplayMode
    scale: float
    page_draws: tuple[PageDraw, ...]
    used_area: float
    pan_min_x: float = 0.0
    pan_max_x: float = 0.0

    @property
    def supports_horizontal_pan(self) -> bool:
        return self.mode == ReaderDisplayMode.WIDE_PAN and self.pan_min_x < self.pan_max_x


@dataclass(frozen=True)
class ReaderSettings:
    direction: ReaderDirection = ReaderDirection.RIGHT_TO_LEFT
    vertical_custom_enabled: bool = False
    page_spacing: int = 0
    vertical_zoom_percent: int = 100
    custom_enabled: bool = False
    always_one_page: bool = False
    fit_mode: ReaderFitMode = ReaderFitMode.AUTO
    transition_mode: ReaderTransitionMode = ReaderTransitionMode.NONE
    spread_offset: int = 0

    def layout_settings(self) -> ReaderLayoutSettings:
        return ReaderLayoutSettings(
            direction=self.direction,
            custom_enabled=self.custom_enabled,
            always_one_page=self.always_one_page,
            fit_mode=self.fit_mode,
            vertical_custom_enabled=self.vertical_custom_enabled,
            page_spacing=self.page_spacing,
            vertical_zoom_percent=self.vertical_zoom_percent,
            transition_mode=self.transition_mode,
            spread_offset=self.spread_offset,
        )


@dataclass(frozen=True)
class ReaderProgress:
    page_index: int
    progress_percent: float


@dataclass(frozen=True)
class ReaderPageImage:
    page_index: int
    image_bytes: bytes
    dimensions: tuple[int, int]
