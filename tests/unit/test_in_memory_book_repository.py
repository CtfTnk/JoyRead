import pytest

from joyread.core.archive import ArchiveImageService
from tests.support.in_memory_book_repository import InMemoryBookRepository
from tests.support.local_corpus import local_fixture_path


_SHORT_COMIC = local_fixture_path("cbz_short", "short-comic.cbz")
_LARGE_COMIC = local_fixture_path("cbz_large", "large-comic.cbz")
requires_local_corpus = pytest.mark.skipif(
    not (_SHORT_COMIC.is_file() and _LARGE_COMIC.is_file()),
    reason="the local comic fixtures are not present in test_set/",
)


def test_in_memory_repository_returns_stable_varied_books() -> None:
    repository = InMemoryBookRepository()

    books = repository.list_books()

    assert len(books) == 15
    assert books[0].uuid == "mock-book-01"
    assert books[0].language_tag == "en"
    assert books[0].language_name == "English"
    assert {book.file_format for book in books} >= {"CBZ", "PDF", "EPUB"}
    assert max(book.page_count for book in books) > min(book.page_count for book in books)
    assert any(book.is_favourite for book in books)
    assert any(book.is_missing for book in books)
    assert repository.list_collections()[0].name == "A Collection"
    assert [(language.iso_code, language.plain_text) for language in repository.list_languages()] == [
        ("en", "English"),
        ("zh", "Chinese"),
        ("ja", "Japanese"),
        ("und", "Unknown"),
    ]


@requires_local_corpus
def test_local_comic_corpus_opens_both_configured_samples() -> None:
    service = ArchiveImageService()
    assert service.open(_SHORT_COMIC).page_count > 0
    assert service.open(_LARGE_COMIC).page_count > 0


def test_in_memory_repository_removes_collection_and_recent_membership_only() -> None:
    repository = InMemoryBookRepository()
    akane_book = next(book for book in repository.list_books() if book.uuid == "mock-book-01")

    repository.remove_book_from_collection(akane_book.uuid, "collection-a")
    repository.remove_book_from_recent(akane_book.uuid)

    updated = next(book for book in repository.list_books() if book.uuid == akane_book.uuid)
    assert updated.progress == akane_book.progress
    assert updated.collection_ids == ()
    assert updated.last_read_at is None
    assert len(repository.list_books()) == 15
