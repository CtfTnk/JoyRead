"""Startup content marks must describe visible data, not only empty shells."""

from __future__ import annotations

from PySide6.QtGui import QColor, QImage

from joyread.app import startup_trace
from joyread.app.reader_page_pipeline import PreparedReaderPage
from joyread.core.reader import PageDraw, ReaderDisplayMode, ReaderLayoutResult, RectF
from joyread.core.services.library_service import LibraryService
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.viewmodels.shelf_viewmodel import ShelfViewModel
from joyread.ui.views.shelf_view import ShelfView
from joyread.ui.widgets.reader_canvas import ReaderCanvas
from tests.support.in_memory_book_repository import InMemoryBookRepository


def _mark_names() -> set[str]:
    return {mark.name for mark in startup_trace.milestones()}


def test_reader_page_mark_waits_until_an_actual_frame_is_painted(qtbot) -> None:
    startup_trace.reset()
    try:
        canvas = ReaderCanvas()
        qtbot.addWidget(canvas)
        canvas.resize(240, 340)
        canvas.set_layout_result(
            ReaderLayoutResult(
                mode=ReaderDisplayMode.SINGLE,
                scale=1.0,
                page_draws=(PageDraw(0, RectF(0, 0, 200, 300)),),
                used_area=200 * 300,
            )
        )
        canvas.show()
        qtbot.wait(50)
        assert "reader_first_page_paint" not in _mark_names()

        frame = QImage(10, 10, QImage.Format.Format_RGB32)
        frame.fill(QColor("#336699"))
        canvas.set_page_frame(PreparedReaderPage(0, frame, (10, 10), (10, 10), generation=1))
        qtbot.waitUntil(lambda: "reader_first_page_paint" in _mark_names(), timeout=2000)
    finally:
        startup_trace.reset()


def test_reader_failed_page_marks_an_error_instead_of_readable_content(qtbot) -> None:
    startup_trace.reset()
    try:
        canvas = ReaderCanvas()
        qtbot.addWidget(canvas)
        canvas.resize(240, 340)
        canvas.set_layout_result(
            ReaderLayoutResult(
                mode=ReaderDisplayMode.SINGLE,
                scale=1.0,
                page_draws=(PageDraw(0, RectF(0, 0, 200, 300)),),
                used_area=200 * 300,
            )
        )
        canvas.set_page_failed(0)
        canvas.show()
        qtbot.waitUntil(lambda: "reader_error_visible" in _mark_names(), timeout=2000)
        assert "reader_first_page_paint" not in _mark_names()
    finally:
        startup_trace.reset()


def test_library_book_mark_waits_for_visible_book_card(qtbot) -> None:
    startup_trace.reset()
    try:
        vm = ShelfViewModel(LibraryService(InMemoryBookRepository()))
        vm.load_books()
        shelf = ShelfView(vm, ResourceLoader())
        qtbot.addWidget(shelf)
        shelf.resize(1000, 720)
        shelf.render()
        assert "library_first_book_paint" not in _mark_names()
        shelf.show()
        qtbot.waitUntil(lambda: "library_first_book_paint" in _mark_names(), timeout=3000)
    finally:
        startup_trace.reset()


def test_empty_library_has_a_distinct_ready_mark(qtbot) -> None:
    startup_trace.reset()
    try:
        vm = ShelfViewModel(LibraryService(InMemoryBookRepository(books=[])))
        vm.load_books()
        shelf = ShelfView(vm, ResourceLoader())
        qtbot.addWidget(shelf)
        shelf.resize(1000, 720)
        shelf.render()
        shelf.show()
        qtbot.waitUntil(lambda: "library_ready_empty" in _mark_names(), timeout=3000)
        assert "library_first_book_paint" not in _mark_names()
    finally:
        startup_trace.reset()


def test_library_error_surface_has_a_distinct_mark(qtbot) -> None:
    startup_trace.reset()
    try:
        vm = ShelfViewModel(LibraryService(InMemoryBookRepository(books=[])))
        vm.error_message = "unavailable"
        shelf = ShelfView(vm, ResourceLoader())
        qtbot.addWidget(shelf)
        shelf.resize(1000, 720)
        shelf.render()
        shelf.show()
        qtbot.waitUntil(lambda: "library_error_visible" in _mark_names(), timeout=3000)
        assert "library_ready_empty" not in _mark_names()
    finally:
        startup_trace.reset()
