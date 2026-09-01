"""Rendered geometry guards for the Library window's responsive limits."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from joyread.app.app_context import create_app_context
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.views.main_window import MainWindow
from tests.support.in_memory_book_repository import InMemoryBookRepository


@pytest.fixture()
def window(qapp: QApplication, tmp_path, monkeypatch):  # noqa: ANN001
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path / "runtime"))
    context = create_app_context()
    main = None
    try:
        qapp.setStyleSheet(context.resources.load_stylesheet())
        main = MainWindow(context, standalone_reader_launcher=lambda _request: None)
        yield main
    finally:
        if main is not None:
            main.close()
        context.close()


def test_minimum_width_keeps_two_book_columns_visible_with_sidebar_open(window: MainWindow) -> None:
    books = InMemoryBookRepository().list_books()
    grid = window.shelf_view.grid
    grid.set_books(books, set())
    window.shelf_view.stack.setCurrentWidget(grid)
    window.resize(Theme.window_min_width, Theme.window_min_height)
    window.show()
    QApplication.processEvents()

    expected_row_width = (Theme.book_card_width * 2) + Theme.grid_min_gap

    assert window.minimumWidth() == Theme.window_min_width == 738
    assert window.sidebar.isVisible()
    assert grid.verticalScrollBar().maximum() > 0
    assert grid._available_row_width() == expected_row_width
    assert grid._calculate_columns() == 2
    assert grid._calculate_columns_for_width(expected_row_width - 1) == 1
