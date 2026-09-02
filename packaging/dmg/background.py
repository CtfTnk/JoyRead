"""Render the .dmg window background.

Generated rather than checked in: it is derived from the app's own palette, it
is a few lines of geometry, and a binary blob in the tree would carry no
provenance and quietly drift from the window size ``build_dmg.py`` asks for.
Those two must agree -- Finder pins the background at its natural size and
crops whatever does not fit -- so both live here.

Rendered at 1x and 2x; ``build_dmg.py`` combines them into a HiDPI TIFF.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Must match --window-size in build_dmg.py.
WIDTH = 640
HEIGHT = 400

# The app's own neutrals, from ui/resources/styles/theme.py. Flat rather than
# a gradient: ten levels of grey spread over 800px bands visibly, and dithering
# a background Finder may rescale is not worth it.
CANVAS = (252, 252, 252)
TEXT = (26, 26, 26)
TEXT_MUTED = (109, 109, 109)
ARROW = (201, 201, 201)

# Icon centres, shared with build_dmg.py's --icon / --app-drop-link.
APP_CENTRE = (170, 205)
APPLICATIONS_CENTRE = (470, 205)

_FONT_CANDIDATES = (
    "/System/Library/Fonts/SFNSDisplay.ttf",
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)


def _font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _centred(draw: ImageDraw.ImageDraw, y: int, text: str, font, fill, scale: int) -> None:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(
        ((WIDTH * scale - (right - left)) / 2 - left, y * scale - top),
        text,
        font=font,
        fill=fill,
    )


def render(path: Path, scale: int = 1) -> Path:
    """Draw the background at ``scale`` and write it to ``path``."""
    width, height = WIDTH * scale, HEIGHT * scale
    image = Image.new("RGB", (width, height), CANVAS)
    draw = ImageDraw.Draw(image)

    _centred(draw, 72, "JoyRead", _font(34 * scale), TEXT, scale)
    _centred(
        draw,
        118,
        "Drag JoyRead into your Applications folder",
        _font(14 * scale),
        TEXT_MUTED,
        scale,
    )

    # The arrow, stopping clear of both icon boxes (128pt icons, so 64pt each
    # side of centre) with a little air.
    y = APP_CENTRE[1] * scale
    start = (APP_CENTRE[0] + 82) * scale
    end = (APPLICATIONS_CENTRE[0] - 82) * scale
    head = 13 * scale
    draw.line([(start, y), (end - head, y)], fill=ARROW, width=max(1, 3 * scale))
    draw.polygon(
        [(end, y), (end - head, y - head * 0.62), (end - head, y + head * 0.62)],
        fill=ARROW,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    print(render(here / "background-preview.png", scale=2))
