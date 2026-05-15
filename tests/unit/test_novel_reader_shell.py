"""Skeleton tests for the novel reader shell + window routing."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

from joyread.app.app_context import create_app_context
from joyread.core.models.book import Book
from joyread.core.reader import ReaderDirection
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.views.main_window import NOVEL_FORMATS, MainWindow, _is_novel_source
from joyread.ui.views.novel_reader_shell import NovelReaderShellWidget
from joyread.ui.views.novel_reader_window import NovelReaderWindow


def test_novel_format_routing_recognises_epub() -> None:
    assert ".epub" in NOVEL_FORMATS
    assert _is_novel_source(Path("/tmp/book.epub"))
    assert _is_novel_source(Path("/tmp/book.EPUB"))
    assert not _is_novel_source(Path("/tmp/book.cbz"))


def test_novel_reader_window_chrome_matches_skeleton_layout(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "novel.epub"
    source.write_bytes(b"placeholder")
    context = create_app_context()
    window = NovelReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    assert window.width() == Theme.reader_width
    assert window.height() == Theme.reader_height
    assert window.minimumWidth() == Theme.reader_min_width
    assert window.header.height() == Theme.reader_banner_height
    assert window.footer.height() == Theme.reader_footer_height
    # Custom toggle is exposed on the novel header but never on manga.
    assert window.header.custom_button.isVisible()
    # Manga-specific footer controls are hidden in novel mode.
    assert window.footer.direction_switch.isHidden()
    assert window.footer.effect_switch.isHidden()
    assert window.footer.shift_button.isHidden()
    # Footer gear duplicates the header gear; only the header trigger remains.
    assert window.footer.settings_button.isHidden()
    # Slider fills left-to-right so progress mirrors top-to-bottom scrolling.
    assert window.footer.slider.reading_direction == ReaderDirection.TOP_TO_BOTTOM
    # Content area hides its own scrollbar; the footer slider owns progress.
    assert window.content_area.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    # Custom panel exists but starts hidden.
    assert window.custom_panel.isHidden()

    window.close()
    context.close()


def test_novel_content_area_right_click_wakes_chrome(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "novel.epub"
    source.write_bytes(b"placeholder")
    context = create_app_context()
    window = NovelReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()
    qtbot.wait(0)

    # Force the chrome hidden then send a right-click on the viewport.
    window.shell._control_interaction_active = lambda: False  # type: ignore[method-assign]
    window.shell._hide_inactive_controls()
    assert window.header.isHidden()
    assert window.footer.isHidden()

    qtbot.mouseClick(
        window.content_area.viewport(),
        Qt.MouseButton.RightButton,
        pos=QPoint(window.content_area.viewport().width() // 2, window.content_area.viewport().height() // 2),
    )

    assert window.header.isVisible()
    assert window.footer.isVisible()

    window.close()
    context.close()


def test_novel_content_area_edge_mouse_move_reveals_paddles(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "novel.epub"
    source.write_bytes(b"placeholder")
    context = create_app_context()
    window = NovelReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()
    qtbot.wait(0)

    window.shell._control_interaction_active = lambda: False  # type: ignore[method-assign]
    window.shell._hide_inactive_controls()
    assert window.left_arrow.isHidden()
    assert window.right_arrow.isHidden()

    # Drive the shell's edge-reveal handler directly with a viewport-local
    # point near the left edge; qtbot.mouseMove can be flaky on headless
    # macOS CI but the signal contract is what we want to pin.
    window.shell._handle_content_mouse_move(QPoint(2, window.shell.height() // 2))
    assert window.left_arrow.isVisible()

    window.shell._hide_inactive_controls()
    assert window.right_arrow.isHidden()
    window.shell._handle_content_mouse_move(QPoint(window.shell.width() - 2, window.shell.height() // 2))
    assert window.right_arrow.isVisible()

    window.close()
    context.close()


def test_novel_reader_custom_panel_opens_and_closes_on_escape(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "novel.epub"
    source.write_bytes(b"placeholder")
    context = create_app_context()
    window = NovelReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    window.shell._toggle_custom_panel()

    assert window.custom_panel.isVisible()
    assert window.custom_panel.geometry().getRect() == (
        Theme.reader_width - Theme.novel_custom_panel_width,
        0,
        Theme.novel_custom_panel_width,
        Theme.reader_height,
    )

    qtbot.keyClick(window, Qt.Key.Key_Escape)
    assert window.custom_panel.isHidden()

    window.close()
    context.close()


def test_novel_reader_custom_panel_closes_on_outside_click(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "novel.epub"
    source.write_bytes(b"placeholder")
    context = create_app_context()
    window = NovelReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    window.shell._toggle_custom_panel()
    assert window.custom_panel.isVisible()

    qtbot.mouseClick(window.content_area, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))

    assert window.custom_panel.isHidden()

    window.close()
    context.close()


def test_novel_slider_round_trips_with_content_area_scroll(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "novel.epub"
    source.write_bytes(b"placeholder")
    context = create_app_context()
    window = NovelReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()
    qtbot.wait(0)

    # The placeholder body is long enough to force a scrollable viewport.
    assert window.content_area.is_scrollable()

    # Slider → content area: setting the slider scrolls the body.
    window.footer.slider.setValue(50)
    qtbot.wait(0)
    assert window.content_area.scroll_percentage() == pytest.approx(0.5, abs=0.05)

    # Content area → slider: drive the underlying scrollbar directly
    # (set_scroll_percentage intentionally suppresses the round-trip
    # signal to break re-entrancy, but a real user scroll fires it).
    bar = window.content_area.verticalScrollBar()
    bar.setValue(int(bar.maximum() * 0.9))
    qtbot.wait(0)
    assert window.footer.slider.value() == pytest.approx(90, abs=2)

    window.close()
    context.close()


def test_novel_custom_panel_signals_drive_content_area(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "novel.epub"
    source.write_bytes(b"placeholder")
    context = create_app_context()
    window = NovelReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    # Toggle the switch — content area should acknowledge via its status line.
    window.custom_panel.enable_switch.set_checked(True)
    qtbot.wait(0)
    assert "ON" in window.content_area._status_label.text()

    # Spin font size up via the embedded spinner; body label font size follows.
    window.custom_panel.font_size_control.set_value(24)
    qtbot.wait(0)
    assert window.content_area._body_label.font().pointSize() == 24

    window.close()
    context.close()


def test_main_window_routes_epub_book_to_novel_reader(qtbot, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    context = create_app_context()
    novel_path = tmp_path / "story.epub"
    novel_path.write_bytes(b"placeholder")
    book = _novel_book(novel_path)

    window = MainWindow(context)
    qtbot.addWidget(window)
    # Replace the shelf after MainWindow.__init__ has run load_books();
    # the routing decision in open_reader_for_book reads ``books`` live.
    context.shelf_viewmodel.books = [book]
    context.settings_store.update(individual_read_window=True)

    window.open_reader_for_book(book.uuid)
    assert len(window._reader_windows) == 1
    assert isinstance(window._reader_windows[0], NovelReaderWindow)

    window._reader_windows[0].close()
    qtbot.wait(0)
    window.close()
    context.close()


def test_main_window_routes_epub_file_open_to_novel_window(qtbot, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    context = create_app_context()
    novel_path = tmp_path / "anything.epub"
    novel_path.write_bytes(b"placeholder")

    window = MainWindow(context)
    qtbot.addWidget(window)

    window.open_reader_for_file(novel_path, import_mode=True)

    assert len(window._reader_windows) == 1
    assert isinstance(window._reader_windows[0], NovelReaderWindow)

    window._reader_windows[0].close()
    qtbot.wait(0)
    window.close()
    context.close()


def _novel_book(source: Path) -> Book:
    now = datetime.now()
    return Book(
        uuid="novel-book",
        title="Test Novel",
        author=None,
        language_tag="en",
        language_name="English",
        book_type="Novel",
        file_format=source.suffix.lstrip(".").upper(),
        file_path=str(source),
        progress=0.0,
        cover_thumbnail_path=None,
        added_at=now,
        updated_at=now,
        last_read_at=None,
        is_favourite=False,
        original_file_name=source.name,
    )
