"""Guards against text being shorn by a container that is too short.

Fixed heights in this app come from Figma frames measured against a Latin
font. The shipped face is Noto Sans SC, whose line box is noticeably taller
at the same pixel size, so a frame that looks right in the design can still
cut the descenders off ``g``, ``j``, ``p``, ``q`` and ``y``.

These tests state the invariant directly -- a label must be given at least
``QFontMetrics.height()`` -- so changing a font, a size, or a padding fails
here instead of shipping quietly clipped text.
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QFontDatabase, QFontMetrics
from PySide6.QtWidgets import QApplication, QLabel

from joyread.infrastructure.i18n import locale_service
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme


DESCENDERS = "gjpqy"


@pytest.fixture(scope="module", autouse=True)
def _styled_app():
    app = QApplication.instance()
    assert app is not None
    resources = ResourceLoader()
    locale_service.init(resources.locale_dir(), None, "English")
    # The bundled face is the one that overflows the Figma frames; measuring a
    # substitute would make this test agree with a design that is not shipped.
    for path in resources.font_paths():
        QFontDatabase.addApplicationFont(str(path))
    app.setStyleSheet(resources.load_stylesheet())
    yield


def _label(text: str, *, style_class: str | None = None, object_name: str | None = None) -> QLabel:
    label = QLabel(text)
    if style_class is not None:
        label.setProperty("class", style_class)
    if object_name is not None:
        label.setObjectName(object_name)
    label.style().unpolish(label)
    label.style().polish(label)
    return label


def _assert_fits(label: QLabel, available: int, what: str) -> None:
    needed = QFontMetrics(label.font()).height()
    assert needed <= available, (
        f"{what}: {label.font().pixelSize()}px text needs {needed}px of line box "
        f"but is given {available}px -- {needed - available}px of descender is "
        f"cut off. Either give the frame more room or take it out of the padding."
    )


def test_book_detail_attribute_pill_fits_its_text(qtbot) -> None:
    """Language / Book type pills on the book detail panel."""

    label = _label(f"Language: English {DESCENDERS}", style_class="BookDetailPillText")
    qtbot.addWidget(label)
    available = (
        Theme.detail_attribute_height
        - (Theme.detail_attribute_border_width * 2)
        - (Theme.detail_attribute_layout_margin * 2)
    )

    _assert_fits(label, available, "BookDetail attribute pill")


def test_section_banner_fits_its_text(qtbot) -> None:
    """SectionBanner is shared by the sidebar, Settings, the reader settings
    panel, and the novel custom panel, so one clipped banner is four."""

    label = _label(f"Paging {DESCENDERS}", object_name="SidebarSectionLabel")
    qtbot.addWidget(label)
    available = (
        Theme.sidebar_section_height
        - Theme.sidebar_section_padding_top
        - Theme.sidebar_section_padding_bottom
    )

    _assert_fits(label, available, "SectionBanner label")


def test_tag_chip_fits_its_text(qtbot) -> None:
    """Tag chips carry user-entered names, so descenders are unavoidable."""

    label = _label(f"Manga {DESCENDERS}", style_class="TagChipLabel")
    qtbot.addWidget(label)
    font = label.font()
    font.setPixelSize(Theme.tag_chip_font_size)
    label.setFont(font)
    available = Theme.tag_chip_height - (Theme.tag_chip_border_width * 2)

    _assert_fits(label, available, "Tag chip label")


def test_dialog_title_fits_its_text(qtbot) -> None:
    label = _label(f"Paging {DESCENDERS}", style_class="JoyReadDialogTitle")
    qtbot.addWidget(label)

    _assert_fits(label, label.sizeHint().height(), "Dialog title")
