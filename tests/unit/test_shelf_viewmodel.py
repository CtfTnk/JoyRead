from joyread.core.repositories.mock_book_repository import MockBookRepository
from joyread.core.services.library_service import LibraryService
from joyread.ui.viewmodels.shelf_viewmodel import (
    FileFilter,
    ShelfKey,
    ShelfViewModel,
    SortField,
    ViewMode,
    collection_shelf_key,
)


def make_viewmodel() -> ShelfViewModel:
    return ShelfViewModel(LibraryService(MockBookRepository()))


def test_load_books_populates_books_and_collections() -> None:
    vm = make_viewmodel()

    vm.load_books()

    assert len(vm.books) == 14
    assert vm.collections[0].uuid == "collection-a"
    assert vm.page_title == "All"


def test_search_filter_and_sort_affect_visible_books() -> None:
    vm = make_viewmodel()
    vm.load_books()

    vm.set_search_query("spy")
    assert [book.title for book in vm.visible_books] == ["Spy x Family Vol. 1"]

    vm.set_search_query("")
    vm.set_filter(FileFilter.EPUB.value)
    assert {book.file_format for book in vm.visible_books} == {"EPUB"}

    vm.set_filter(FileFilter.ALL.value)
    vm.set_sort(SortField.TITLE.value, ascending=True)
    titles = [book.title for book in vm.visible_books]
    assert titles == sorted(titles, key=str.lower)


def test_shelf_filters_recent_favourites_and_collection() -> None:
    vm = make_viewmodel()
    vm.load_books()

    vm.set_current_shelf(ShelfKey.RECENT.value)
    assert all(book.last_read_at is not None for book in vm.visible_books)

    vm.set_current_shelf(ShelfKey.FAVOURITES.value)
    assert all(book.is_favourite for book in vm.visible_books)

    vm.set_current_shelf(collection_shelf_key("collection-a"))
    assert vm.page_title == "A Collection"
    assert all("collection-a" in book.collection_ids for book in vm.visible_books)


def test_view_mode_selection_and_favourite_toggle() -> None:
    vm = make_viewmodel()
    vm.load_books()
    first, second = vm.visible_books[:2]

    vm.set_view_mode(ViewMode.LIST.value)
    assert vm.view_mode == ViewMode.LIST

    vm.select_book(first.uuid)
    assert vm.selected_book_ids == {first.uuid}

    vm.select_book(second.uuid, additive=True)
    assert vm.selected_book_ids == {first.uuid, second.uuid}

    vm.select_book(first.uuid, additive=True)
    assert vm.selected_book_ids == {second.uuid}

    original = next(book for book in vm.books if book.uuid == second.uuid)
    vm.toggle_favourite(second.uuid)
    updated = next(book for book in vm.books if book.uuid == second.uuid)
    assert updated.is_favourite is (not original.is_favourite)
