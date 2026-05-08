from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image

from joyread.app.app_context import create_app_context
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.views.reader_window import ReaderWindow


def test_reader_window_matches_figma_shell_geometry(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "reader.cbz"
    image = tmp_path / "001.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")
    context = create_app_context()
    window = ReaderWindow(context, source)
    qtbot.addWidget(window)

    window.show()
    window.resize(Theme.reader_width, Theme.reader_height)

    assert window.width() == Theme.reader_width
    assert window.height() == Theme.reader_height
    assert window.minimumWidth() == Theme.reader_min_width
    assert window.minimumHeight() == Theme.reader_min_height
    assert window.header.height() == Theme.reader_banner_height
    assert window.footer.height() == Theme.reader_footer_height
    assert window.left_arrow.size().width() == Theme.reader_side_button_width
    assert window.left_arrow.size().height() == Theme.reader_side_button_height

    window.close()
    context.close()
