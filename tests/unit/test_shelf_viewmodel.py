from pathlib import Path

from joyread.core.repositories.mock_book_repository import MockBookRepository
from joyread.core.services.library_service import LibraryService
from joyread.core.services.task_service import TaskHandle
from joyread.core.services.thumbnail_service import DetailThumbnailBatch, DetailThumbnailItem
from joyread.infrastructure.config.settings_store import AppSettings, SettingsStore
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

    assert len(vm.books) == 15
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


def test_set_favourite_applies_same_state_to_multiple_books() -> None:
    vm = make_viewmodel()
    vm.load_books()
    first, second = vm.visible_books[:2]

    vm.set_favourite((first.uuid, second.uuid), True)

    updated = {book.uuid: book for book in vm.books}
    assert updated[first.uuid].is_favourite is True
    assert updated[second.uuid].is_favourite is True

    vm.load_books()
    reloaded = {book.uuid: book for book in vm.books}
    assert reloaded[first.uuid].is_favourite is True
    assert reloaded[second.uuid].is_favourite is True


def test_collection_commands_validate_and_reload_state() -> None:
    vm = make_viewmodel()
    failures: list[str] = []
    changed: list[str | None] = []
    vm.collection_failed.connect(failures.append)
    vm.collections_changed.connect(changed.append)
    vm.load_books()
    first, second = vm.visible_books[:2]

    vm.create_collection("  ")
    assert failures == ["Collection name cannot be empty."]

    vm.create_collection(" Reading Queue ")
    created = vm.collections[-1]
    assert created.name == "Reading Queue"
    assert vm.current_shelf == collection_shelf_key(created.uuid)
    assert changed[-1] == collection_shelf_key(created.uuid)

    vm.add_books_to_collection((first.uuid, second.uuid), created.uuid)
    books_by_uuid = {book.uuid: book for book in vm.books}
    assert created.uuid in books_by_uuid[first.uuid].collection_ids
    assert created.uuid in books_by_uuid[second.uuid].collection_ids

    vm.rename_collection(created.uuid, "Finished")
    renamed = next(collection for collection in vm.collections if collection.uuid == created.uuid)
    assert renamed.name == "Finished"

    vm.delete_collection(created.uuid)
    assert all(collection.uuid != created.uuid for collection in vm.collections)
    assert vm.current_shelf == ShelfKey.ALL.value


def test_delete_books_clears_selection_detail_and_reloads_books() -> None:
    vm = make_viewmodel()
    vm.load_books()
    first, second = vm.visible_books[:2]
    deleted: list[tuple[str, ...]] = []
    vm.books_deleted.connect(deleted.append)

    vm.select_book(first.uuid)
    vm.select_book(second.uuid, additive=True)
    vm.show_detail(first.uuid)
    vm.delete_books((first.uuid, second.uuid))

    remaining_ids = {book.uuid for book in vm.books}
    assert first.uuid not in remaining_ids
    assert second.uuid not in remaining_ids
    assert vm.selected_book_ids == set()
    assert vm.detail_book_uuid is None
    assert deleted == [(first.uuid, second.uuid)]


def test_shelf_preferences_round_trip_through_settings_store(tmp_path: Path) -> None:
    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=tmp_path / "storage")
    settings = AppSettings(
        storage_location=str(tmp_path / "storage"),
        shelf_sort_field=SortField.AUTHOR.value,
        shelf_sort_ascending=True,
        shelf_file_filter=FileFilter.EPUB.value,
        shelf_view_mode=ViewMode.LIST.value,
    )
    store.save(settings)

    vm = ShelfViewModel(LibraryService(MockBookRepository()), settings=store.load(), settings_store=store)

    assert vm.sort_field == SortField.AUTHOR
    assert vm.sort_ascending is True
    assert vm.file_filter == FileFilter.EPUB
    assert vm.view_mode == ViewMode.LIST

    vm.set_sort(SortField.TITLE.value, ascending=False)
    vm.set_filter(FileFilter.CBZ.value)
    vm.set_view_mode(ViewMode.GRID.value)
    persisted = store.load()

    assert persisted.shelf_sort_field == SortField.TITLE.value
    assert persisted.shelf_sort_ascending is False
    assert persisted.shelf_file_filter == FileFilter.CBZ.value
    assert persisted.shelf_view_mode == ViewMode.GRID.value


def test_shelf_preferences_fall_back_when_settings_are_stale(tmp_path: Path) -> None:
    settings = AppSettings(
        storage_location=str(tmp_path / "storage"),
        shelf_sort_field="Bad Sort",
        shelf_file_filter="BAD",
        shelf_view_mode="bad",
    )

    vm = ShelfViewModel(LibraryService(MockBookRepository()), settings=settings)

    assert vm.sort_field == SortField.ADD_TIME
    assert vm.file_filter == FileFilter.ALL
    assert vm.view_mode == ViewMode.GRID


def test_detail_page_state_tracks_visible_book() -> None:
    vm = make_viewmodel()
    vm.load_books()
    first = vm.visible_books[0]

    vm.show_detail(first.uuid)
    assert vm.detail_book_uuid == first.uuid

    vm.set_filter(FileFilter.EPUB.value)
    assert vm.detail_book_uuid is None


class RecordingTaskService:
    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.callbacks = []
        self.success_callbacks = []

    def submit(self, name, callback, *, on_success=None, on_failure=None):  # noqa: ANN001
        del on_failure
        self.submitted.append(name)
        self.callbacks.append(callback)
        self.success_callbacks.append(on_success)
        return TaskHandle(task_id=name)

    def complete(self, index: int = -1) -> None:
        result = self.callbacks[index]()
        callback = self.success_callbacks[index]
        if callback is not None:
            callback(result)


class FakeThumbnailService:
    def __init__(self) -> None:
        self.coverable_ids = {"mock-book-01", "mock-book-15"}

    def can_generate_from(self, book) -> bool:  # noqa: ANN001
        return book.uuid in self.coverable_ids

    def existing_cover_path(self, book, size):  # noqa: ANN001
        del book, size
        return None

    def generate_cover(self, book, size):  # noqa: ANN001
        del size
        return Path(f"/tmp/{book.uuid}.png")

    def generate_detail_thumbnail_batch(self, book, start_index, batch_size, size):  # noqa: ANN001
        del size
        items = tuple(
            DetailThumbnailItem(page_index=index, image_bytes=f"page-{index}".encode())
            for index in range(start_index, start_index + batch_size)
        )
        return DetailThumbnailBatch(
            book_uuid=book.uuid,
            start_index=start_index,
            next_index=start_index + batch_size,
            has_more=start_index == 0,
            items=items,
        )


def test_load_books_does_not_queue_all_covers_until_view_requests_visible_books() -> None:
    task_service = RecordingTaskService()
    vm = ShelfViewModel(
        LibraryService(MockBookRepository()),
        FakeThumbnailService(),  # type: ignore[arg-type]
        task_service,  # type: ignore[arg-type]
        cover_size=(200, 284),
    )

    vm.load_books()
    assert task_service.submitted == []

    vm.request_covers_for_books(book.uuid for book in vm.visible_books)

    assert sorted(task_service.submitted) == ["cover-mock-book-01", "cover-mock-book-15"]


def test_detail_open_does_not_submit_per_page_tasks_and_batches_on_demand() -> None:
    task_service = RecordingTaskService()
    vm = ShelfViewModel(
        LibraryService(MockBookRepository()),
        FakeThumbnailService(),  # type: ignore[arg-type]
        task_service,  # type: ignore[arg-type]
        cover_size=(200, 284),
    )
    vm.load_books()
    book = next(book for book in vm.books if book.uuid == "mock-book-15")

    vm.show_detail(book.uuid)
    assert task_service.submitted == []

    vm.request_next_detail_thumbnail_batch(book.uuid, (100, 142))
    vm.request_next_detail_thumbnail_batch(book.uuid, (100, 142))

    assert task_service.submitted == [f"detail-thumbnail-batch-{book.uuid}-0"]


def test_detail_batch_results_emit_items_and_allow_next_batch() -> None:
    task_service = RecordingTaskService()
    vm = ShelfViewModel(
        LibraryService(MockBookRepository()),
        FakeThumbnailService(),  # type: ignore[arg-type]
        task_service,  # type: ignore[arg-type]
        cover_size=(200, 284),
    )
    emitted: list[tuple[str, int, bytes]] = []
    finished: list[tuple[str, int, bool]] = []
    vm.page_thumbnail_ready.connect(lambda book_uuid, page_index, data: emitted.append((book_uuid, page_index, data)))
    vm.detail_thumbnail_batch_finished.connect(
        lambda book_uuid, next_index, has_more: finished.append((book_uuid, next_index, has_more))
    )
    vm.load_books()
    book = next(book for book in vm.books if book.uuid == "mock-book-15")
    vm.show_detail(book.uuid)

    vm.request_next_detail_thumbnail_batch(book.uuid, (100, 142))
    task_service.complete()
    vm.request_next_detail_thumbnail_batch(book.uuid, (100, 142))

    assert len(emitted) == 14
    assert emitted[0] == (book.uuid, 0, b"page-0")
    assert finished == [(book.uuid, 14, True)]
    assert task_service.submitted == [
        f"detail-thumbnail-batch-{book.uuid}-0",
        f"detail-thumbnail-batch-{book.uuid}-14",
    ]


def test_stale_detail_batch_results_are_ignored_after_switching_books() -> None:
    task_service = RecordingTaskService()
    vm = ShelfViewModel(
        LibraryService(MockBookRepository()),
        FakeThumbnailService(),  # type: ignore[arg-type]
        task_service,  # type: ignore[arg-type]
        cover_size=(200, 284),
    )
    emitted: list[tuple[str, int, bytes]] = []
    vm.page_thumbnail_ready.connect(lambda book_uuid, page_index, data: emitted.append((book_uuid, page_index, data)))
    vm.load_books()
    first = next(book for book in vm.books if book.uuid == "mock-book-15")
    second = next(book for book in vm.books if book.uuid == "mock-book-01")

    vm.show_detail(first.uuid)
    vm.request_next_detail_thumbnail_batch(first.uuid, (100, 142))
    vm.show_detail(second.uuid)
    task_service.complete(0)

    assert emitted == []
