"""Manual import-only playground for the cover editor.

Run from the project root:

    /usr/local/bin/python3 tests/playgrounds/cover_editor_import_playground.py
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys

from PIL import Image
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QFileDialog, QToolButton, QWidget

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.cover_editor import CoverEditorOverlay


def _placeholder_png() -> bytes:
    image = Image.new("RGB", (Theme.cover_width, Theme.cover_height), "#ececec")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def main() -> int:
    app = QApplication(sys.argv)
    resources = ResourceLoader()
    app.setStyleSheet(resources.load_stylesheet())

    root = QWidget()
    root.setWindowTitle("JoyRead Cover Editor Playground")
    root.resize(Theme.window_width, Theme.window_height)

    overlay = CoverEditorOverlay(resources, root)
    overlay.setGeometry(root.rect())
    overlay.open_editor(QImage.fromData(_placeholder_png()), "playground")
    browse_button = overlay.editor.findChild(QToolButton, "CoverEditorBrowseButton")
    if browse_button is not None:
        browse_button.hide()

    def import_image() -> None:
        file_path, _selected_filter = QFileDialog.getOpenFileName(
            root,
            "Import Cover Image",
            str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.webp *.gif *.bmp *.tif *.tiff)",
        )
        if file_path:
            overlay.set_source(QImage(file_path), f"import:{Path(file_path).name}")

    overlay.import_requested.connect(import_image)
    overlay.save_requested.connect(lambda state: print(f"crop_state={state}"))

    root.show()
    overlay.raise_()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
