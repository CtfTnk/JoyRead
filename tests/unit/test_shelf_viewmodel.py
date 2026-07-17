from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock

from tests.support.in_memory_book_repository import InMemoryBookRepository
from joyread.core.models.tag import Tag
from joyread.core.services.library_service import LibraryService
from joyread.core.services.archive_extraction_pool import ArchiveExtractionPool
from joyread.core.services.cache_service import CacheService
from joyread.core.services.task_service import TaskHandle, TaskStatus
from joyread.core.services.thumbnail_service import DetailThumbnailItem, ThumbnailSourceHandle
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
    return ShelfViewModel(LibraryService(InMemoryBookRepository()))


class _FakeShelfTagService:
    def __init__(self, links: dict[str, tuple[str, ...]]) -> None:
        self._links = links
        tag_ids = sorted({tag_id for tag_ids in links.values() for tag_id in tag_ids})
        self._tags = [Tag(tag_id, tag_id.removeprefix("tag-").title()) for tag_id in tag_ids]

    def list_tags(self) -> list[Tag]:
        return sorted(self._tags, key=lambda tag: tag.name_normalized)

    def list_tag_ids_for_books(self, book_ids: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
        return {book_id: self._links.get(book_id, ()) for book_id in book_ids}

    def set_book_tag_ids(self, book_id: str, tag_ids: tuple[str, ...]) -> None:
        self._links[book_id] = tuple(dict.fromkeys(tag_id for tag_id in tag_ids if tag_id))
        known_ids = {tag.tag_id for tag in self._tags}
        missing_ids = [tag_id for tag_id in self._links[book_id] if tag_id not in known_ids]
        self._tags.extend(Tag(tag_id, tag_id.removeprefix("tag-").title()) for tag_id in missing_ids)


def make_viewmodel_with_tag_links(links: dict[str, tuple[str, ...]]) -> ShelfViewModel:
    return ShelfViewModel(
        LibraryService(InMemoryBookRepository()),
        tag_service=_FakeShelfTagService(links),  # type: ignore[arg-type]
    )


def test_load_books_populates_books_and_collections() -> None:
    vm = make_viewmodel()

    vm.load_books()

    assert len(vm.books) == 15
    assert vm.collections[0].uuid == "collection-a"
    assert [(language.iso_code, language.plain_text) for language in vm.languages] == [
        ("en", "English"),
        ("zh", "Chinese"),
        ("ja", "Japanese"),
        ("und", "Unknown"),
    ]
    assert vm.page_title == "All"


def test_search_filter_and_sort_affect_visible_books() -> None:
    vm = make_viewmodel()
    vm.load_books()

    vm.set_search_query("spy")
    assert [book.title for book in vm.visible_books] == ["Spy x Family Vol. 1"]

    vm.set_search_query("endo")
    assert [book.title for book in vm.visible_books] == ["Spy x Family Vol. 1"]

    vm.set_search_query("spy frieren")
    assert [book.title for book in vm.visible_books] == ["Spy x Family Vol. 1", "Frieren Beyond Journey"]

    vm.set_search_query("")
    vm.set_filter(FileFilter.EPUB.value)
    assert {book.file_format for book in vm.visible_books} == {"EPUB"}

    vm.set_filter(FileFilter.ALL.value)
    vm.set_sort(SortField.TITLE.value, ascending=True)
    titles = [book.title for book in vm.visible_books]
    assert titles == sorted(titles, key=str.lower)


def test_search_uses_only_title_and_author_fields() -> None:
    vm = make_viewmodel()
    vm.load_books()

    for query in ("cbz", "comic", "english", "1lib"):
        vm.set_search_query(query)
        assert vm.visible_books == []


def test_search_preserves_current_sort_order() -> None:
    vm = make_viewmodel()
    vm.load_books()

    vm.set_sort(SortField.TITLE.value, ascending=True)
    vm.set_search_query("yuki ryoko")

    titles = [book.title for book in vm.visible_books]
    assert titles == sorted(titles, key=str.lower)
    assert set(titles) == {
        "Akane-banashi Story 148",
        "Delicious in Dungeon v14",
        "Dungeon Meshi Archive",
        "Mushishi Volume Notes",
    }


def test_search_cache_refreshes_after_book_metadata_update() -> None:
    vm = make_viewmodel()
    vm.load_books()

    vm.set_search_query("omega")
    assert vm.visible_books == []

    vm.update_book_metadata("mock-book-01", title="Omega Search")

    assert [book.uuid for book in vm.visible_books] == ["mock-book-01"]


def test_tag_filter_requires_all_selected_tags() -> None:
    vm = make_viewmodel_with_tag_links(
        {
            "mock-book-01": ("tag-action", "tag-comedy"),
            "mock-book-03": ("tag-action",),
            "mock-book-09": ("tag-comedy",),
        }
    )
    vm.load_books()

    vm.set_tag_filter_ids(("tag-action", "tag-comedy"))

    assert vm.tag_filter_active is True
    assert {book.uuid for book in vm.visible_books} == {"mock-book-01"}


def test_tag_filter_composes_with_current_collection() -> None:
    vm = make_viewmodel_with_tag_links(
        {
            "mock-book-01": ("tag-comedy",),
            "mock-book-03": ("tag-action",),
            "mock-book-09": ("tag-comedy",),
            "mock-book-02": ("tag-comedy",),
        }
    )
    vm.load_books()
    vm.set_current_shelf(collection_shelf_key("collection-a"))

    vm.set_tag_filter_ids(("tag-comedy",))

    assert {book.uuid for book in vm.visible_books} == {"mock-book-01", "mock-book-09"}


def test_tag_filter_composes_with_hidden_shelf() -> None:
    vm = make_viewmodel_with_tag_links({"mock-book-03": ("tag-action",), "mock-book-04": ("tag-comedy",)})
    vm.load_books()
    vm.hide_books(["mock-book-03"])
    vm.hide_books(["mock-book-04"])
    vm.set_current_shelf(ShelfKey.HIDDEN.value)

    vm.set_tag_filter_ids(("tag-action",))

    assert {book.uuid for book in vm.visible_books} == {"mock-book-03"}


def test_tags_for_book_returns_available_tags_in_tag_sort_order() -> None:
    vm = make_viewmodel_with_tag_links(
        {
            "mock-book-01": ("tag-comedy", "tag-action"),
        }
    )
    vm.load_books()

    assert [tag.name for tag in vm.available_tags] == ["Action", "Comedy"]
    assert [tag.name for tag in vm.tags_for_book("mock-book-01")] == ["Action", "Comedy"]
    assert vm.tag_ids_for_book("mock-book-01") == ("tag-comedy", "tag-action")


def test_set_book_tag_ids_updates_visible_detail_tags() -> None:
    vm = make_viewmodel_with_tag_links({"mock-book-01": ("tag-action",)})
    vm.load_books()

    vm.set_book_tag_ids("mock-book-01", ("tag-comedy", "tag-action", "tag-comedy"))

    assert vm.tag_ids_for_book("mock-book-01") == ("tag-comedy", "tag-action")
    assert [tag.name for tag in vm.tags_for_book("mock-book-01")] == ["Action", "Comedy"]


def test_set_book_tag_ids_composes_with_active_filter_and_hides_detail() -> None:
    vm = make_viewmodel_with_tag_links({"mock-book-01": ("tag-action",)})
    vm.load_books()
    vm.show_detail("mock-book-01")
    vm.set_tag_filter_ids(("tag-action",))

    vm.set_book_tag_ids("mock-book-01", ())

    assert "mock-book-01" not in {book.uuid for book in vm.visible_books}
    assert vm.detail_book_uuid is None


def test_set_book_tag_ids_failure_is_logged_and_emitted() -> None:
    class FailingTagService(_FakeShelfTagService):
        def set_book_tag_ids(self, book_id: str, tag_ids: tuple[str, ...]) -> None:
            raise RuntimeError("tag write failed")

    service = FailingTagService({"mock-book-01": ("tag-action",)})
    vm = ShelfViewModel(
        LibraryService(InMemoryBookRepository()),
        tag_service=service,  # type: ignore[arg-type]
    )
    emitted: list[str] = []
    vm.book_tags_failed.connect(emitted.append)
    vm.load_books()

    vm.set_book_tag_ids("mock-book-01", ("tag-comedy",))

    assert emitted == ["tag write failed"]
    assert vm.tag_ids_for_book("mock-book-01") == ("tag-action",)


def test_tag_filter_is_not_persisted_in_shelf_preferences(tmp_path: Path) -> None:
    store = SettingsStore("JoyReadTest", "JoyRead", support_root=tmp_path / "support")
    settings = store.load()
    vm = ShelfViewModel(
        LibraryService(InMemoryBookRepository()),
        settings=settings,
        settings_store=store,
        tag_service=_FakeShelfTagService({"mock-book-01": ("tag-action",)}),  # type: ignore[arg-type]
    )
    vm.load_books()

    vm.set_tag_filter_ids(("tag-action",))
    fresh = ShelfViewModel(
        LibraryService(InMemoryBookRepository()),
        settings=store.load(),
        settings_store=store,
        tag_service=_FakeShelfTagService({"mock-book-01": ("tag-action",)}),  # type: ignore[arg-type]
    )

    assert vm.tag_filter_active is True
    assert fresh.tag_filter_active is False


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


def test_reader_progress_update_refreshes_visible_state() -> None:
    vm = make_viewmodel()
    vm.load_books()
    book = vm.visible_books[0]
    events: list[None] = []
    vm.state_changed.connect(lambda: events.append(None))

    vm.apply_reader_progress(book.uuid, page_index=3, progress_percent=75.0)

    updated = next(candidate for candidate in vm.books if candidate.uuid == book.uuid)
    assert updated.progress == 0.75
    assert updated.last_read_at is not None
    assert events


def test_open_book_at_emits_page_index_for_valid_book() -> None:
    vm = make_viewmodel()
    vm.load_books()
    target = vm.visible_books[0]
    emitted: list[tuple[str, int]] = []
    vm.book_open_at_requested.connect(lambda book_uuid, page_index: emitted.append((book_uuid, page_index)))

    vm.open_book_at(target.uuid, 5)
    vm.open_book_at("missing", 3)

    assert emitted == [(target.uuid, 5)]


def test_open_book_emits_missing_signal_for_missing_book() -> None:
    vm = make_viewmodel()
    vm.load_books()
    missing = next(book for book in vm.books if book.is_missing)
    opened: list[str] = []
    missing_events: list[str] = []
    vm.book_open_requested.connect(opened.append)
    vm.missing_book_requested.connect(missing_events.append)

    vm.open_book(missing.uuid)

    assert opened == []
    assert missing_events == [missing.uuid]


def test_show_detail_emits_missing_signal_for_missing_book() -> None:
    vm = make_viewmodel()
    vm.load_books()
    missing = next(book for book in vm.books if book.is_missing)
    missing_events: list[str] = []
    vm.missing_book_requested.connect(missing_events.append)

    vm.show_detail(missing.uuid)

    assert vm.detail_book_uuid is None
    assert missing_events == [missing.uuid]


def test_recent_shelf_ignores_user_sort_and_uses_latest_read_first() -> None:
    vm = make_viewmodel()
    vm.load_books()
    older, newer = vm.books[:2]
    now = datetime.now()
    vm.books = [
        replace(older, title="Zeta", last_read_at=now - timedelta(days=1)),
        replace(newer, title="Alpha", last_read_at=now),
        *vm.books[2:],
    ]

    vm.set_current_shelf(ShelfKey.RECENT.value)
    vm.set_sort(SortField.TITLE.value, ascending=True)

    assert vm.visible_books[0].uuid == newer.uuid


def test_reader_progress_update_moves_book_to_front_of_recent() -> None:
    vm = make_viewmodel()
    vm.load_books()
    now = datetime.now()
    vm.books = [
        replace(book, last_read_at=now - timedelta(days=index + 1))
        for index, book in enumerate(vm.books)
    ]
    target = vm.books[-1]

    vm.set_current_shelf(ShelfKey.RECENT.value)

    vm.apply_reader_progress(target.uuid, page_index=4, progress_percent=80.0)

    assert vm.visible_books[0].uuid == target.uuid


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


def test_update_book_metadata_persists_detail_edits_after_reload() -> None:
    vm = make_viewmodel()
    vm.load_books()
    book = vm.visible_books[0]

    vm.update_book_title(book.uuid, "  Edited Detail Title  ")
    vm.update_book_author(book.uuid, "  Edited Author  ")
    vm.update_book_language(book.uuid, "ja")

    updated = next(candidate for candidate in vm.books if candidate.uuid == book.uuid)
    assert updated.title == "Edited Detail Title"
    assert updated.author == "Edited Author"
    assert updated.language_tag == "ja"
    assert updated.language_name == "Japanese"

    vm.load_books()
    reloaded = next(candidate for candidate in vm.books if candidate.uuid == book.uuid)
    assert reloaded.title == "Edited Detail Title"
    assert reloaded.author == "Edited Author"
    assert reloaded.language_tag == "ja"
    assert reloaded.language_name == "Japanese"


def test_update_book_language_rejects_unknown_code() -> None:
    vm = make_viewmodel()
    failures: list[str] = []
    vm.book_metadata_failed.connect(failures.append)
    vm.load_books()
    book = vm.visible_books[0]

    vm.update_book_language(book.uuid, "bad")

    updated = next(candidate for candidate in vm.books if candidate.uuid == book.uuid)
    assert updated.language_tag == book.language_tag
    assert failures == ["Unknown language code: bad"]


def test_update_book_language_failure_reloads_books() -> None:
    repository = FailingLanguageUpdateRepository()
    vm = ShelfViewModel(LibraryService(repository))
    failures: list[str] = []
    vm.book_metadata_failed.connect(failures.append)
    vm.load_books()
    book = vm.visible_books[0]

    vm.update_book_language(book.uuid, "zh")

    reloaded = next(candidate for candidate in vm.books if candidate.uuid == book.uuid)
    assert reloaded.language_tag == "ja"
    assert reloaded.language_name == "Japanese"
    assert failures == ["write failed"]


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


def test_remove_books_from_collection_shelf_keeps_library_records() -> None:
    vm = make_viewmodel()
    vm.load_books()
    vm.set_current_shelf(collection_shelf_key("collection-a"))
    first, second = vm.visible_books[:2]

    vm.select_book(first.uuid)
    vm.select_book(second.uuid, additive=True)
    vm.show_detail(first.uuid)
    vm.remove_books_from_current_shelf((first.uuid, second.uuid))

    books_by_uuid = {book.uuid: book for book in vm.books}
    assert first.uuid in books_by_uuid
    assert second.uuid in books_by_uuid
    assert "collection-a" not in books_by_uuid[first.uuid].collection_ids
    assert "collection-a" not in books_by_uuid[second.uuid].collection_ids
    assert first.uuid not in {book.uuid for book in vm.visible_books}
    assert second.uuid not in {book.uuid for book in vm.visible_books}
    assert vm.selected_book_ids == set()
    assert vm.detail_book_uuid is None


def test_remove_books_from_recent_shelf_preserves_progress_and_library_records() -> None:
    vm = make_viewmodel()
    vm.load_books()
    vm.set_current_shelf(ShelfKey.RECENT.value)
    first, second = vm.visible_books[:2]
    original_progress = {first.uuid: first.progress, second.uuid: second.progress}

    vm.select_book(first.uuid)
    vm.select_book(second.uuid, additive=True)
    vm.show_detail(first.uuid)
    vm.remove_books_from_current_shelf((first.uuid, second.uuid))

    books_by_uuid = {book.uuid: book for book in vm.books}
    assert first.uuid in books_by_uuid
    assert second.uuid in books_by_uuid
    assert books_by_uuid[first.uuid].last_read_at is None
    assert books_by_uuid[second.uuid].last_read_at is None
    assert books_by_uuid[first.uuid].progress == original_progress[first.uuid]
    assert books_by_uuid[second.uuid].progress == original_progress[second.uuid]
    assert first.uuid not in {book.uuid for book in vm.visible_books}
    assert second.uuid not in {book.uuid for book in vm.visible_books}
    assert vm.selected_book_ids == set()
    assert vm.detail_book_uuid is None


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

    vm = ShelfViewModel(LibraryService(InMemoryBookRepository()), settings=store.load(), settings_store=store)

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

    vm = ShelfViewModel(LibraryService(InMemoryBookRepository()), settings=settings)

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
        handle = TaskHandle(task_id=name)
        handle.status = TaskStatus.RUNNING
        return handle

    def complete(self, index: int = -1) -> None:
        result = self.callbacks[index]()
        callback = self.success_callbacks[index]
        if callback is not None:
            callback(result)


class FailingLanguageUpdateRepository(InMemoryBookRepository):
    def update_book_metadata(
        self,
        book_id: str,
        *,
        title: str | None = None,
        author: str | None = None,
        language_tag: str | None = None,
    ) -> None:
        if language_tag is not None:
            self._books = [
                replace(book, language_tag="ja", language_name="Japanese")
                if book.uuid == book_id
                else book
                for book in self._books
            ]
            raise RuntimeError("write failed")
        super().update_book_metadata(
            book_id,
            title=title,
            author=author,
            language_tag=language_tag,
        )


class FakeThumbnailService:
    def __init__(self) -> None:
        self.coverable_ids = {"mock-book-01", "mock-book-15"}
        self.cache_service = CacheService(
            ArchiveExtractionPool(None, 0),
            reader_page_cache_max_bytes=1024,
            thumbnail_cache_max_bytes=1024,
        )

    def can_generate_from(self, book) -> bool:  # noqa: ANN001
        return book.uuid in self.coverable_ids

    def existing_cover_path(self, book, size):  # noqa: ANN001
        del book, size
        return None

    def generate_cover(self, book, size):  # noqa: ANN001
        del size
        return Path(f"/tmp/{book.uuid}.png")

    def issue_thumbnail_cache_client(self, client_id):  # noqa: ANN001
        return self.cache_service.issue_thumbnail_client(client_id)

    def open_thumbnail_source(self, book):  # noqa: ANN001
        session = type("FakeThumbnailSession", (), {"page_count": 28})()
        return ThumbnailSourceHandle(
            source_id=f"source:{book.uuid}",
            page_count=28,
            suffix=".cbz",
            session=session,  # type: ignore[arg-type]
            access_lock=RLock(),
            source_path=Path(book.file_path),
            nested_archive_max_depth=2,
            archive_global_file_max_depth=100,
        )

    def stream_thumbnails(self, source, page_indices, size, emit_item):  # noqa: ANN001
        del source, size
        for index in page_indices:
            emit_item(DetailThumbnailItem(page_index=index, image_bytes=f"page-{index}".encode()))


def test_load_books_does_not_queue_all_covers_until_view_requests_visible_books() -> None:
    task_service = RecordingTaskService()
    vm = ShelfViewModel(
        LibraryService(InMemoryBookRepository()),
        FakeThumbnailService(),  # type: ignore[arg-type]
        task_service,  # type: ignore[arg-type]
        cover_size=(200, 284),
    )

    vm.load_books()
    assert task_service.submitted == []

    vm.request_covers_for_books(book.uuid for book in vm.visible_books)

    assert sorted(task_service.submitted) == ["cover-mock-book-01", "cover-mock-book-15"]


def test_set_book_cover_path_updates_state_and_emits_cover_even_for_same_path() -> None:
    repo = InMemoryBookRepository()
    vm = ShelfViewModel(LibraryService(repo))
    vm.load_books()
    emitted: list[tuple[str, Path]] = []
    vm.cover_ready.connect(lambda book_uuid, path: emitted.append((book_uuid, path)))
    cover_path = Path("/tmp/mock-book-01-custom-170x241.png")

    vm.set_book_cover_path("mock-book-01", cover_path)
    vm.set_book_cover_path("mock-book-01", cover_path)

    book = next(book for book in vm.books if book.uuid == "mock-book-01")
    assert book.cover_thumbnail_path == str(cover_path)
    assert repo.get_book("mock-book-01").cover_thumbnail_path == str(cover_path)
    assert emitted == [("mock-book-01", cover_path), ("mock-book-01", cover_path)]


def test_detail_open_submits_one_source_task_and_waits_for_viewport_interest() -> None:
    task_service = RecordingTaskService()
    vm = ShelfViewModel(
        LibraryService(InMemoryBookRepository()),
        FakeThumbnailService(),  # type: ignore[arg-type]
        task_service,  # type: ignore[arg-type]
        cover_size=(200, 284),
    )
    vm.load_books()
    book = next(book for book in vm.books if book.uuid == "mock-book-15")

    vm.show_detail(book.uuid)
    assert task_service.submitted == []

    vm.set_detail_thumbnail_interest(book.uuid, (0,), (1,), (100, 142))
    vm.set_detail_thumbnail_interest(book.uuid, (0,), (1,), (100, 142))

    assert task_service.submitted == [f"detail-thumbnail-source-{book.uuid}"]


def test_detail_stream_emits_each_visible_item_and_advances_serially() -> None:
    task_service = RecordingTaskService()
    vm = ShelfViewModel(
        LibraryService(InMemoryBookRepository()),
        FakeThumbnailService(),  # type: ignore[arg-type]
        task_service,  # type: ignore[arg-type]
        cover_size=(200, 284),
    )
    emitted: list[tuple[str, int, bytes]] = []
    vm.page_thumbnail_ready.connect(lambda book_uuid, page_index, data: emitted.append((book_uuid, page_index, data)))
    vm.load_books()
    book = next(book for book in vm.books if book.uuid == "mock-book-15")
    vm.show_detail(book.uuid)

    vm.set_detail_thumbnail_interest(book.uuid, (0, 1), (2,), (100, 142))
    task_service.complete()
    task_service.complete()

    assert emitted[0] == (book.uuid, 0, b"page-0")
    assert task_service.submitted[:3] == [
        f"detail-thumbnail-source-{book.uuid}",
        "detail-thumbnail-0",
        "detail-thumbnail-1",
    ]


def test_detail_close_releases_pins_but_keeps_shared_lru_bytes() -> None:
    task_service = RecordingTaskService()
    thumbnail_service = FakeThumbnailService()
    vm = ShelfViewModel(
        LibraryService(InMemoryBookRepository()),
        thumbnail_service,  # type: ignore[arg-type]
        task_service,  # type: ignore[arg-type]
        cover_size=(200, 284),
    )
    vm.load_books()
    book = next(book for book in vm.books if book.uuid == "mock-book-15")
    vm.show_detail(book.uuid)
    vm.set_detail_thumbnail_interest(book.uuid, (0,), (), (100, 142))
    task_service.complete()
    task_service.complete()

    cache = thumbnail_service.cache_service.thumbnail_cache
    assert cache.current_bytes > 0
    assert vm._detail_stream is not None
    cache_client = vm._detail_stream._cache
    assert cache_client.pins

    vm.hide_detail()

    assert cache_client.pins == frozenset()
    assert cache.current_bytes > 0


def test_stale_detail_source_results_are_ignored_after_switching_books() -> None:
    task_service = RecordingTaskService()
    vm = ShelfViewModel(
        LibraryService(InMemoryBookRepository()),
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
    vm.set_detail_thumbnail_interest(first.uuid, (0,), (), (100, 142))
    vm.show_detail(second.uuid)
    task_service.complete(0)

    assert emitted == []


# ---------------------------------------------------------------------------
# Hidden Space surface


def test_hidden_books_are_excluded_from_all_recent_favourites_and_normal_collections() -> None:
    vm = make_viewmodel()
    vm.load_books()
    # mock-book-03 is favourited, has a last_read_at, and sits in
    # ``collection-a`` (a normal user collection) — three different filter
    # surfaces that should all exclude it once hidden.
    vm.hide_books(["mock-book-03"])

    visible_all = {book.uuid for book in vm.visible_books}
    assert "mock-book-03" not in visible_all

    vm.set_current_shelf(ShelfKey.RECENT.value)
    assert all(book.uuid != "mock-book-03" for book in vm.visible_books)

    vm.set_current_shelf(ShelfKey.FAVOURITES.value)
    assert all(book.uuid != "mock-book-03" for book in vm.visible_books)

    vm.set_current_shelf(collection_shelf_key("collection-a"))
    assert all(book.uuid != "mock-book-03" for book in vm.visible_books)


def test_hidden_shelf_shows_only_hidden_books() -> None:
    vm = make_viewmodel()
    vm.load_books()
    vm.hide_books(["mock-book-03", "mock-book-06"])

    vm.set_current_shelf(ShelfKey.HIDDEN.value)
    visible = {book.uuid for book in vm.visible_books}
    assert visible == {"mock-book-03", "mock-book-06"}
    assert vm.page_title == "Hidden"


def test_hidable_collection_keeps_hidden_books_visible_when_navigated_into() -> None:
    vm = make_viewmodel()
    vm.load_books()
    # Mark collection-a hidable first so the cascade in ``hide_books``
    # keeps the membership.
    vm.set_collection_hidable("collection-a", True)
    vm.hide_books(["mock-book-03"])

    vm.set_current_shelf(collection_shelf_key("collection-a"))
    visible = {book.uuid for book in vm.visible_books}
    assert "mock-book-03" in visible


def test_visible_collections_drops_hidable_when_toggle_is_off() -> None:
    vm = make_viewmodel()
    vm.load_books()
    vm.set_collection_hidable("collection-a", True)

    # Toggle defaults to False: hidable collections should disappear from
    # the sidebar entirely.
    assert vm.show_hidden_collection is False
    assert all(not collection.is_hidable for collection in vm.visible_collections)

    vm.set_show_hidden_collection(True)
    visible_ids = {collection.uuid for collection in vm.visible_collections}
    assert "collection-a" in visible_ids


def test_set_show_hidden_collection_off_redirects_away_from_hidden_shelf() -> None:
    vm = make_viewmodel()
    vm.load_books()
    vm.set_show_hidden_collection(True)
    vm.set_current_shelf(ShelfKey.HIDDEN.value)
    assert vm.current_shelf == ShelfKey.HIDDEN.value

    vm.set_show_hidden_collection(False)

    # Sitting on the Hidden shelf when the toggle flips off would leave
    # an empty page; the VM redirects to All.
    assert vm.current_shelf == ShelfKey.ALL.value


def test_set_show_hidden_collection_off_redirects_away_from_hidable_collection() -> None:
    vm = make_viewmodel()
    vm.load_books()
    vm.set_show_hidden_collection(True)
    vm.set_collection_hidable("collection-a", True)
    vm.set_current_shelf(collection_shelf_key("collection-a"))

    vm.set_show_hidden_collection(False)

    assert vm.current_shelf == ShelfKey.ALL.value


def test_unhide_books_restores_visibility_on_all_shelf() -> None:
    vm = make_viewmodel()
    vm.load_books()
    vm.hide_books(["mock-book-03"])
    assert "mock-book-03" not in {book.uuid for book in vm.visible_books}

    vm.unhide_books(["mock-book-03"])
    assert "mock-book-03" in {book.uuid for book in vm.visible_books}


def test_hidden_books_cannot_be_favourited() -> None:
    vm = make_viewmodel()
    vm.load_books()
    vm.hide_books(["mock-book-04"])
    errors: list[str] = []
    vm.favourite_failed.connect(errors.append)

    vm.set_favourite(["mock-book-04"], True)

    refreshed = next(book for book in vm.books if book.uuid == "mock-book-04")
    assert refreshed.is_favourite is False
    assert errors and "Hidden" in errors[0]


def test_hidden_books_blocked_from_normal_collections_with_error_message() -> None:
    vm = make_viewmodel()
    vm.load_books()
    vm.hide_books(["mock-book-04"])
    errors: list[str] = []
    vm.collection_failed.connect(errors.append)

    # ``collection-a`` is a normal (non-hidable) collection in the
    # in-memory fixture; the add must be rejected with a clear message
    # rather than silently inserted and filtered away later.
    vm.add_books_to_collection(["mock-book-04"], "collection-a")

    refreshed = next(book for book in vm.books if book.uuid == "mock-book-04")
    assert "collection-a" not in refreshed.collection_ids
    assert errors and "hidden" in errors[0].lower()


def test_hidden_books_can_be_added_to_hidable_collections() -> None:
    vm = make_viewmodel()
    vm.load_books()
    vm.set_collection_hidable("collection-a", True)
    vm.hide_books(["mock-book-04"])

    vm.add_books_to_collection(["mock-book-04"], "collection-a")

    refreshed = next(book for book in vm.books if book.uuid == "mock-book-04")
    assert "collection-a" in refreshed.collection_ids
