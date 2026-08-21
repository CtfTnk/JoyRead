"""Icon rasterization shared by the tag browser and the drag overlay."""

from __future__ import annotations

import pytest
from PySide6.QtGui import QImage

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.widgets import icon_paint


@pytest.fixture()
def icon(qtbot) -> str:
    return str(ResourceLoader().icon_path("icon_read.svg"))


def test_glyphs_are_rasterized_at_the_screen_ratio(icon, monkeypatch) -> None:
    """`QIcon.pixmap(size)` alone renders at 1x and is then upscaled.

    JoyRead's v1.0 target hardware is retina-only, and the drag overlay draws
    the largest icons in the app (32px in a 72px disc), where that softness is
    most visible.
    """

    monkeypatch.setattr(icon_paint, "_device_pixel_ratio", lambda: 2.0)

    pixmap = icon_paint.tinted_pixmap(icon, 32, color="white")

    assert pixmap.devicePixelRatio() == 2.0
    assert pixmap.width() == 64  # 32 logical px, rasterized at 2x
    # The logical size is what callers lay out against, and must not change.
    assert pixmap.width() / pixmap.devicePixelRatio() == 32


def test_tinting_replaces_the_ink_and_keeps_the_shape(icon) -> None:
    plain = icon_paint.tinted_pixmap(icon, 32).toImage()
    white = icon_paint.tinted_pixmap(icon, 32, color="white").toImage()

    assert plain.size() == white.size()
    opaque = [
        (x, y)
        for y in range(white.height())
        for x in range(white.width())
        if white.pixelColor(x, y).alpha() > 200
    ]
    assert opaque, "the glyph should have solid pixels"
    # Same coverage, different ink. Alpha is compared separately: antialiased
    # edge pixels are white at partial alpha, which is correct, not a miss.
    for x, y in opaque:
        assert plain.pixelColor(x, y).alpha() > 200
        assert white.pixelColor(x, y).getRgb()[:3] == (255, 255, 255)


def test_fading_preserves_the_ratio(icon, monkeypatch) -> None:
    """A faded copy must not silently drop back to 1x."""

    monkeypatch.setattr(icon_paint, "_device_pixel_ratio", lambda: 2.0)

    faded = icon_paint.faded_pixmap(icon, 16, 0.4)

    assert faded.devicePixelRatio() == 2.0
    assert faded.width() == 32


def test_a_ratio_of_one_is_unchanged(icon, monkeypatch) -> None:
    monkeypatch.setattr(icon_paint, "_device_pixel_ratio", lambda: 1.0)

    pixmap = icon_paint.tinted_pixmap(icon, 32, color="white")

    assert pixmap.width() == 32
    assert isinstance(pixmap.toImage(), QImage)
