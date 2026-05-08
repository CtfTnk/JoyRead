from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from PIL import Image
from PySide6.QtCore import QPointF, QRectF

from joyread.app.app_context import create_app_context
from joyread.core.reader import ReaderDirection
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.views.main_window import MainWindow
from joyread.ui.views.reader_window import ReaderWindow
from joyread.ui.widgets.reader_controls import ReaderProgressSlider, _bottom_rounded_path, _top_rounded_path


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
    assert not window.header.back_button.isVisible()
    assert not window.header.back_spacer.isVisible()
    assert window.shell.mask().isEmpty()
    direction_buttons = window.footer.direction_switch._buttons
    assert list(direction_buttons) == [
        ReaderDirection.RIGHT_TO_LEFT,
        ReaderDirection.LEFT_TO_RIGHT,
        ReaderDirection.TOP_TO_BOTTOM,
    ]
    assert direction_buttons[ReaderDirection.RIGHT_TO_LEFT].toolTip() == "Right-to-left"
    assert direction_buttons[ReaderDirection.RIGHT_TO_LEFT].property("iconName") == "icon_read-from-left.svg"
    assert direction_buttons[ReaderDirection.LEFT_TO_RIGHT].toolTip() == "Left-to-right"
    assert direction_buttons[ReaderDirection.LEFT_TO_RIGHT].property("iconName") == "icon_read-from-right.svg"
    assert direction_buttons[ReaderDirection.TOP_TO_BOTTOM].toolTip() == "Top-to-down"
    assert direction_buttons[ReaderDirection.TOP_TO_BOTTOM].property("iconName") == "icon_read-from-top.svg"

    window.close()
    context.close()


def test_shelf_reader_uses_embedded_mode_when_individual_window_disabled(qtbot, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("JOYREAD_USE_MOCK_REPOSITORY", "1")
    context = create_app_context()
    window = MainWindow(context)
    qtbot.addWidget(window)
    book = context.shelf_viewmodel.books[0]

    window.open_reader_for_book(book.uuid)

    assert window._embedded_reader is not None
    assert not window._embedded_reader.header.back_button.isHidden()
    assert not window._reader_windows

    window.close()
    context.close()


def test_shelf_reader_uses_independent_mode_when_setting_enabled(qtbot, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("JOYREAD_USE_MOCK_REPOSITORY", "1")
    context = create_app_context()
    context.settings_store.update(individual_read_window=True)
    window = MainWindow(context)
    qtbot.addWidget(window)
    book = context.shelf_viewmodel.books[0]

    window.open_reader_for_book(book.uuid)

    assert window._embedded_reader is None
    assert len(window._reader_windows) == 1
    assert not window._reader_windows[0].header.back_button.isVisible()

    for reader in tuple(window._reader_windows):
        reader.close()
    window.close()
    context.close()


def test_reader_panel_paths_fill_body_without_overlapping_holes() -> None:
    rect = QRectF(0, 0, 240, Theme.reader_banner_height)
    header_path = _top_rounded_path(rect)
    footer_path = _bottom_rounded_path(rect)

    assert header_path.contains(QPointF(rect.center().x(), rect.center().y()))
    assert header_path.contains(QPointF(4, rect.bottom() - 4))
    assert not header_path.contains(QPointF(1, 1))
    assert footer_path.contains(QPointF(rect.center().x(), rect.center().y()))
    assert footer_path.contains(QPointF(4, rect.top() + 4))
    assert not footer_path.contains(QPointF(1, rect.bottom() - 1))


def test_reader_progress_slider_filled_track_mirrors_with_direction(qtbot) -> None:
    slider = ReaderProgressSlider()
    qtbot.addWidget(slider)
    slider.resize(240, Theme.reader_slider_height)
    slider.setMinimum(0)
    slider.setMaximum(10)
    slider.setValue(3)

    slider.set_reading_direction(ReaderDirection.LEFT_TO_RIGHT)
    track = slider._track_rect()
    left_to_right = slider._filled_track_rect()

    slider.set_reading_direction(ReaderDirection.RIGHT_TO_LEFT)
    right_to_left = slider._filled_track_rect()

    assert left_to_right.left() == pytest.approx(track.left())
    assert right_to_left.right() == pytest.approx(track.right())
    assert left_to_right.width() == pytest.approx(right_to_left.width())
    assert left_to_right.left() < right_to_left.left()


def test_reader_footer_updates_progress_slider_direction(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "reader.cbz"
    image = tmp_path / "001.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")
    context = create_app_context()
    window = ReaderWindow(context, source)
    qtbot.addWidget(window)

    window.footer.set_page_state(2, 10, ReaderDirection.LEFT_TO_RIGHT)
    assert window.footer.slider.reading_direction == ReaderDirection.LEFT_TO_RIGHT
    assert not window.footer.slider.invertedAppearance()

    window.footer.set_direction(ReaderDirection.RIGHT_TO_LEFT)
    assert window.footer.slider.reading_direction == ReaderDirection.RIGHT_TO_LEFT
    assert window.footer.slider.invertedAppearance()

    window.close()
    context.close()
