"""Book controls retain identity and update only the presentation that changed."""
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import QGraphicsOpacityEffect

from joyread.core.services.library_service import LibraryService
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.viewmodels.shelf_viewmodel import ShelfViewModel, SortField, ViewMode
from joyread.ui.views.shelf_view import ShelfView
from joyread.ui.widgets.book_grid import BookGridWidget
from joyread.ui.widgets.book_list import BookListWidget
from tests.support.in_memory_book_repository import InMemoryBookRepository


@pytest.fixture(params=[BookGridWidget, BookListWidget], ids=["grid", "list"])
def book_view(request, qtbot):
    view = request.param(ResourceLoader())
    qtbot.addWidget(view)
    view.resize(1000, 600)
    view.show()
    return view


def controls(view):
    return view._cards if isinstance(view, BookGridWidget) else view._rows


def books():
    base = InMemoryBookRepository().list_books()[0]
    return [replace(base, uuid=f"book-{i}", title=f"UniqueBook{i}", author="Author", is_missing=False, is_unavailable=False) for i in range(4)]


def write_cover(path: Path, color: str):
    image = QImage(40, 60, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    assert image.save(str(path))


def test_reorder_and_membership_changes_preserve_surviving_controls(book_view, qtbot):
    initial = books()
    book_view.set_books(initial, {initial[1].uuid})
    qtbot.wait(1)
    originals = dict(controls(book_view))
    opened = []
    book_view.book_opened.connect(opened.append)
    reordered = list(reversed(initial))
    book_view.set_books(reordered, {initial[1].uuid})
    qtbot.wait(1)
    assert controls(book_view) == originals
    layout = book_view._layout
    assert [layout.itemAt(i).widget().book.uuid for i in range(4)] == [book.uuid for book in reordered]
    # Native geometry follows layout order rather than stale creation order.
    positions = [(originals[book.uuid].y(), originals[book.uuid].x()) for book in reordered]
    assert positions == sorted(positions)
    book_view.set_books(reordered[1:], set())
    removed = reordered[0]
    assert removed.uuid not in controls(book_view)
    assert all(controls(book_view)[book.uuid] is originals[book.uuid] for book in reordered[1:])
    added = replace(initial[0], uuid="new", title="New")
    book_view.set_books([added, *reordered[1:]], set())
    assert all(controls(book_view)[book.uuid] is originals[book.uuid] for book in reordered[1:])
    originals[initial[0].uuid].book_opened.emit(initial[0].uuid)
    assert opened == [initial[0].uuid]  # reconciliation never duplicates wiring
    book_view.set_books([], set())
    assert not controls(book_view)
    book_view.set_books(initial, set())
    assert len(controls(book_view)) == 4


def test_selection_does_not_refresh_content_or_covers(book_view, monkeypatch, tmp_path):
    initial = books()
    path = tmp_path / "cover.png"
    write_cover(path, "red")
    paths = {book.uuid: path for book in initial}
    book_view.set_books(initial, {initial[0].uuid}, paths)
    changed = []
    def unexpected(*args, **kwargs):
        pytest.fail("Selection should not update book data or reload a cover")
    for key, control in controls(book_view).items():
        monkeypatch.setattr(control, "set_book", unexpected)
        monkeypatch.setattr(control, "set_cover_path", unexpected)
        original = control.set_selected
        def record(value, key=key, original=original):
            changed.append((key, value))
            original(value)
        monkeypatch.setattr(control, "set_selected", record)
    book_view.set_books(initial, {initial[1].uuid}, paths)
    assert set(changed) == {(initial[0].uuid, False), (initial[1].uuid, True)}
    changed.clear()
    book_view.set_books(initial, {initial[1].uuid}, paths)
    assert not changed


def test_metadata_and_availability_update_existing_control(book_view):
    initial = books()
    book_view.set_books(initial, set())
    widget = controls(book_view)[initial[0].uuid]
    updated = replace(initial[0], title="Updated title", author="Updated author", progress=0.85, is_missing=True)
    book_view.set_books([updated, *initial[1:]], set())
    assert controls(book_view)[updated.uuid] is widget
    assert widget._title.full_text == "Updated title"
    assert widget._progress.progress_percent == 85
    assert isinstance(widget.graphicsEffect(), QGraphicsOpacityEffect)
    if isinstance(book_view, BookListWidget):
        assert widget._author.text() == "Updated author"
        assert widget._progress_percent_label.text() == "85%"
    healthy = replace(updated, is_missing=False)
    book_view.set_books([healthy, *initial[1:]], set())
    assert not isinstance(widget.graphicsEffect(), QGraphicsOpacityEffect)


def test_same_path_cover_edit_refreshes_but_ordinary_render_reuses(book_view, tmp_path, monkeypatch):
    import os
    import joyread.ui.widgets.book_card as card_module
    initial = books()
    path = tmp_path / "cover.png"
    write_cover(path, "red")
    paths = {book.uuid: path for book in initial}
    calls = []
    original = card_module.load_thumbnail_pixmap
    def load(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(card_module, "load_thumbnail_pixmap", load)
    book_view.set_books(initial, set(), paths)
    before = controls(book_view)[initial[0].uuid]._cover._pixmap.cacheKey()
    calls.clear()
    book_view.set_books(list(reversed(initial)), set(), paths)
    assert not calls
    assert controls(book_view)[initial[0].uuid]._cover._pixmap.cacheKey() == before
    stamp = path.stat()
    write_cover(path, "blue")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    book_view.set_cover_path(initial[0].uuid, path)
    assert calls == [path]
    assert controls(book_view)[initial[0].uuid]._cover._pixmap.toImage().pixelColor(0, 0) == QColor("blue")
    calls.clear()
    book_view.set_books(initial, set(), paths)
    assert not calls
    book_view.set_books(initial[1:], set(), paths)
    book_view.set_books(initial, set(), paths)
    assert calls == [path]  # only the reappearing book needs an image again
    book_view.set_books(initial, set(), {})
    assert all(control._cover.loaded_path is None for control in controls(book_view).values())


def test_shelf_coalesces_cover_requests_and_skips_selection_and_sort(qtbot, monkeypatch):
    initial = books()
    vm = ShelfViewModel(LibraryService(InMemoryBookRepository(books=initial)))
    vm.load_books()
    requests = []
    monkeypatch.setattr(vm, "request_covers_for_books", lambda ids: requests.append(set(ids)))
    shelf = ShelfView(vm, ResourceLoader())
    qtbot.addWidget(shelf)
    shelf.show()
    shelf.render()
    shelf.render()
    qtbot.wait(1)
    assert requests == [{book.uuid for book in initial}]
    requests.clear()
    vm.select_book(initial[0].uuid)
    vm.set_sort(SortField.TITLE.value, ascending=True)
    vm.set_view_mode(ViewMode.LIST.value)
    qtbot.wait(1)
    assert not requests
    # A burst of membership changes resolves only the final requested book.
    vm.set_search_query("UniqueBook1")
    vm.set_search_query("UniqueBook2")
    qtbot.wait(1)
    assert requests == [{initial[2].uuid}]
    requests.clear()
    vm.set_search_query("no results")
    qtbot.wait(1)
    assert not requests
    # Restoring results queues the books whose covers must be resolved again.
    vm.set_search_query("")
    qtbot.wait(1)
    assert requests == [{book.uuid for book in initial}]


def test_clearing_cover_map_requests_resolution_again(qtbot, monkeypatch, tmp_path):
    initial = books()
    vm = ShelfViewModel(LibraryService(InMemoryBookRepository(books=initial)))
    vm.load_books()
    requests = []
    monkeypatch.setattr(vm, "request_covers_for_books", lambda ids: requests.append(set(ids)))
    shelf = ShelfView(vm, ResourceLoader())
    qtbot.addWidget(shelf)
    vm._cover_paths = {book.uuid: tmp_path / f"{book.uuid}.png" for book in initial}
    shelf.render()
    qtbot.wait(1)
    requests.clear()
    vm._cover_paths.clear()
    shelf.render()
    qtbot.wait(1)
    assert requests == [{book.uuid for book in initial}]
