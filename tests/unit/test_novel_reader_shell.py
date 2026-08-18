"""Tests for the novel reader shell, viewmodel, and main_window routing."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt

from joyread.app.app_context import create_app_context
from joyread.core.models.book import Book
from joyread.core.reader import ReaderDirection
from joyread.ui.resources.styles.theme import Theme
from joyread.app.windows.manager import ApplicationWindowManager
from joyread.app.windows.requests import StandaloneReaderRequest
from joyread.ui.views.novel_reader_provider import create_novel_reader_provider
from joyread.ui.views.novel_reader_shell import NovelReaderShellWidget
from joyread.ui.views.novel_reader_window import NovelReaderWindow
from joyread.ui.views.reader_window import ReaderWindow
from joyread.ui.widgets.reader_topic_panel import ReaderTopicMode
from joyread.ui.widgets.window_chrome import StoplightControlsWidget, TitleControlGroup

from tests.support.epub_fixtures import write_tiny_epub


def _wait_for_chapter(qtbot, window) -> None:  # noqa: ANN001
    """Block until the viewmodel finishes loading the resume chapter."""
    qtbot.waitUntil(lambda: not window.shell.viewmodel.is_loading, timeout=2000)
    qtbot.waitUntil(lambda: window.shell.viewmodel.chapter_count > 0, timeout=2000)


def test_a_wired_provider_routes_epub_to_the_novel_window(qtbot, tmp_path: Path) -> None:  # noqa: ARG001
    """The other half of ``test_epub_gate.py``: with the provider supplied,
    the manager builds a novel window for ``.epub`` and leaves every other
    format on the manga/PDF path."""

    context = create_app_context()
    manager = ApplicationWindowManager(
        context,
        novel_reader_provider=create_novel_reader_provider(),
    )

    novel = write_tiny_epub(tmp_path / "routed.epub")
    novel_window = manager._create_reader_window(  # noqa: SLF001
        StandaloneReaderRequest(path=novel, book=None, title=None, start_page_index=None)
    )
    assert isinstance(novel_window, NovelReaderWindow)

    comic = tmp_path / "routed.cbz"
    comic.write_bytes(b"not really a cbz")
    comic_window = manager._create_reader_window(  # noqa: SLF001
        StandaloneReaderRequest(path=comic, book=None, title=None, start_page_index=None)
    )
    assert isinstance(comic_window, ReaderWindow)

    novel_window.close()
    comic_window.close()
    context.close()


def test_novel_reader_window_chrome_matches_skeleton_layout(qtbot, tmp_path: Path) -> None:
    source = write_tiny_epub(tmp_path / "novel.epub")
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


def test_novel_reader_window_uses_shared_title_control_modes(qtbot, tmp_path: Path) -> None:
    source = write_tiny_epub(tmp_path / "novel.epub")
    context = create_app_context()
    context.settings_viewmodel.set_inspect_non_native_title_control(True)
    window = NovelReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    title_controls = window.header.findChild(TitleControlGroup, "TitleControlGroup")
    stoplights = window.header.findChild(StoplightControlsWidget)

    assert title_controls is not None
    assert stoplights is not None
    assert title_controls.isVisible()
    assert stoplights.isHidden()

    window.close()
    context.close()


def test_novel_reader_loads_chapter_and_populates_toc(qtbot, tmp_path: Path) -> None:
    source = write_tiny_epub(
        tmp_path / "novel.epub",
        title="Sample Novel",
        chapter_titles=("Prologue", "Chapter One", "Epilogue"),
    )
    context = create_app_context()
    window = NovelReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    _wait_for_chapter(qtbot, window)

    assert window.shell.viewmodel.title == "Sample Novel"
    assert window.shell.viewmodel.chapter_count == 3
    assert window.shell.viewmodel.current_index == 0
    # TOC items reach the topic panel via the viewmodel.
    assert window.shell.viewmodel.can_use_contents
    assert window.header.detail_button.isEnabled()
    # Footer indicator reflects spine state.
    assert window.footer.page_indicator.text() == "1/3"

    window.close()
    context.close()


def test_novel_reader_topic_contents_seek_changes_chapter(qtbot, tmp_path: Path) -> None:
    source = write_tiny_epub(
        tmp_path / "novel.epub",
        chapter_titles=("Prologue", "Chapter 1", "Chapter 2", "Epilogue"),
    )
    context = create_app_context()
    window = NovelReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    _wait_for_chapter(qtbot, window)
    # Simulate the topic panel emitting contents_selected for spine 2.
    window.topic_panel.contents_selected.emit(2)
    qtbot.waitUntil(lambda: window.shell.viewmodel.current_index == 2, timeout=2000)
    assert window.footer.page_indicator.text() == "3/4"

    window.close()
    context.close()


def test_novel_reader_paddle_buttons_advance_chapter(qtbot, tmp_path: Path) -> None:
    source = write_tiny_epub(
        tmp_path / "novel.epub",
        chapter_titles=("Prologue", "Chapter 1", "Epilogue"),
    )
    context = create_app_context()
    window = NovelReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    _wait_for_chapter(qtbot, window)

    window.right_arrow.click()
    qtbot.waitUntil(lambda: window.shell.viewmodel.current_index == 1, timeout=2000)
    window.right_arrow.click()
    qtbot.waitUntil(lambda: window.shell.viewmodel.current_index == 2, timeout=2000)
    window.left_arrow.click()
    qtbot.waitUntil(lambda: window.shell.viewmodel.current_index == 1, timeout=2000)

    window.close()
    context.close()


def test_novel_reader_custom_panel_opens_and_closes_on_escape(qtbot, tmp_path: Path) -> None:
    source = write_tiny_epub(tmp_path / "novel.epub")
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
    source = write_tiny_epub(tmp_path / "novel.epub")
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


def test_novel_reader_topic_panel_opens_contents_after_load(qtbot, tmp_path: Path) -> None:
    source = write_tiny_epub(
        tmp_path / "novel.epub",
        chapter_titles=("Prologue", "Chapter 1", "Epilogue"),
    )
    context = create_app_context()
    window = NovelReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    _wait_for_chapter(qtbot, window)
    window.shell._show_topic_panel(ReaderTopicMode.CONTENTS)
    assert window.topic_panel.isVisible()
    assert window.topic_panel.mode == ReaderTopicMode.CONTENTS

    qtbot.keyClick(window, Qt.Key.Key_Escape)
    assert window.topic_panel.isHidden()

    window.close()
    context.close()


def test_novel_content_area_right_click_wakes_chrome(qtbot, tmp_path: Path) -> None:
    source = write_tiny_epub(tmp_path / "novel.epub")
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
    source = write_tiny_epub(tmp_path / "novel.epub")
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


def test_novel_custom_panel_font_size_changes_default_stylesheet(qtbot, tmp_path: Path) -> None:
    source = write_tiny_epub(tmp_path / "novel.epub")
    context = create_app_context()
    window = NovelReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    _wait_for_chapter(qtbot, window)

    # Custom off → default stylesheet contains no explicit font-size rule.
    css_off = window.content_area.document().defaultStyleSheet()
    assert "font-size" not in css_off

    window.custom_panel.enable_switch.set_checked(True)
    window.custom_panel.font_size_control.set_value(24)
    qtbot.wait(0)
    css_on = window.content_area.document().defaultStyleSheet()
    assert "font-size: 24pt" in css_on

    # Toggle off → font-size override drops out of the stylesheet again.
    window.custom_panel.enable_switch.set_checked(False)
    qtbot.wait(0)
    css_off_again = window.content_area.document().defaultStyleSheet()
    assert "font-size" not in css_off_again

    window.close()
    context.close()


def test_novel_disable_custom_keeps_chosen_font_for_next_toggle(qtbot, tmp_path: Path) -> None:
    source = write_tiny_epub(tmp_path / "novel.epub")
    context = create_app_context()
    window = NovelReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()
    _wait_for_chapter(qtbot, window)

    window.custom_panel.enable_switch.set_checked(True)
    window.custom_panel.font_size_control.set_value(28)
    qtbot.wait(0)
    assert "font-size: 28pt" in window.content_area.document().defaultStyleSheet()

    # Toggle off — Font Size row is visually disabled while custom is off.
    window.custom_panel.enable_switch.set_checked(False)
    qtbot.wait(0)
    assert "font-size" not in window.content_area.document().defaultStyleSheet()
    assert not window.custom_panel.font_size_row.isEnabled()

    # Toggle back on — 28pt returns without re-input.
    window.custom_panel.enable_switch.set_checked(True)
    qtbot.wait(0)
    assert "font-size: 28pt" in window.content_area.document().defaultStyleSheet()
    assert window.custom_panel.font_size_row.isEnabled()

    window.close()
    context.close()


def test_novel_disable_css_toggle_flips_resource_handling(qtbot, tmp_path: Path) -> None:
    source = write_tiny_epub(tmp_path / "novel.epub")
    context = create_app_context()
    window = NovelReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()
    _wait_for_chapter(qtbot, window)

    # Default off: EPUB CSS would be served through the asset reader.
    assert not window.content_area._disable_css  # noqa: SLF001

    # Toggle on via the panel signal → flag flips, content re-renders.
    window.custom_panel.disable_css_switch.set_checked(True)
    qtbot.wait(0)
    assert window.content_area._disable_css  # noqa: SLF001

    # Toggle back off → flag clears, re-render happens again.
    window.custom_panel.disable_css_switch.set_checked(False)
    qtbot.wait(0)
    assert not window.content_area._disable_css  # noqa: SLF001

    window.close()
    context.close()


def test_novel_reader_resumes_from_saved_chapter(qtbot, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    context = create_app_context()
    novel_path = write_tiny_epub(
        tmp_path / "story.epub",
        chapter_titles=("A", "B", "C", "D"),
    )
    book = _novel_book(novel_path)
    # Seed the shelf so library_service has a row to persist against.
    context.shelf_viewmodel.books = [book]

    window = NovelReaderWindow(context, novel_path, book=book)
    qtbot.addWidget(window)
    window.show()
    _wait_for_chapter(qtbot, window)

    # Seek to chapter 2, wait for persistence to settle.
    window.shell.viewmodel.seek(2)
    qtbot.waitUntil(lambda: window.shell.viewmodel.current_index == 2, timeout=2000)
    qtbot.waitUntil(
        lambda: (
            context.library_service.get_progress(book.uuid) is not None
            and context.library_service.get_progress(book.uuid).page_index == 2
        ),
        timeout=2000,
    )
    window.shell.cancel()
    window.close()
    qtbot.wait(0)

    # Reopen — viewmodel reads progress from library_service and resumes.
    window2 = NovelReaderWindow(context, novel_path, book=book)
    qtbot.addWidget(window2)
    window2.show()
    _wait_for_chapter(qtbot, window2)
    qtbot.waitUntil(lambda: window2.shell.viewmodel.current_index == 2, timeout=2000)
    assert window2.footer.page_indicator.text() == "3/4"

    window2.close()
    context.close()


def test_novel_reader_add_and_delete_bookmark_roundtrip(qtbot, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    context = create_app_context()
    novel_path = write_tiny_epub(
        tmp_path / "story.epub",
        chapter_titles=("Prologue", "Chapter 1", "Epilogue"),
    )
    book = _novel_book(novel_path)
    context.shelf_viewmodel.books = [book]

    window = NovelReaderWindow(context, novel_path, book=book)
    qtbot.addWidget(window)
    window.show()
    _wait_for_chapter(qtbot, window)

    window.shell.viewmodel.seek(1)
    qtbot.waitUntil(lambda: window.shell.viewmodel.current_index == 1, timeout=2000)
    window.shell.viewmodel.add_bookmark()
    qtbot.waitUntil(
        lambda: len(window.shell.viewmodel._bookmarks) == 1,  # noqa: SLF001
        timeout=2000,
    )
    bookmark = window.shell.viewmodel._bookmarks[0]  # noqa: SLF001
    # Bookmark name defaults to the TOC chapter title.
    assert bookmark.name == "Chapter 1"
    assert bookmark.page_index == 1

    # Seek away then click the bookmark via the topic panel signal.
    window.shell.viewmodel.seek(2)
    qtbot.waitUntil(lambda: window.shell.viewmodel.current_index == 2, timeout=2000)
    window.topic_panel.bookmark_selected.emit(bookmark.page_index)
    qtbot.waitUntil(lambda: window.shell.viewmodel.current_index == 1, timeout=2000)

    window.shell.viewmodel.delete_bookmark(bookmark.uuid)
    qtbot.waitUntil(
        lambda: len(window.shell.viewmodel._bookmarks) == 0,  # noqa: SLF001
        timeout=2000,
    )

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


def test_novel_reader_topic_selection_closes_the_panel(qtbot, tmp_path: Path) -> None:
    """A selection means "take me there", so the panel should stop covering
    the chapter the user just chose."""

    source = write_tiny_epub(
        tmp_path / "novel.epub",
        chapter_titles=("Prologue", "Chapter 1", "Chapter 2", "Epilogue"),
    )
    context = create_app_context()
    window = NovelReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    _wait_for_chapter(qtbot, window)
    window.shell._show_topic_panel(ReaderTopicMode.CONTENTS)  # noqa: SLF001
    assert window.topic_panel.isVisible()

    window.topic_panel.contents_selected.emit(2)
    qtbot.waitUntil(lambda: window.shell.viewmodel.current_index == 2, timeout=2000)

    assert window.topic_panel.isHidden(), "selecting a target must dismiss the panel"

    window.close()
    context.close()
