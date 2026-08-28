from pathlib import Path

import pytest

from joyread.core.archive import ArchiveImageService
from tests.support.in_memory_book_repository import InMemoryBookRepository


# The mock repository points two of its rows at a private comic corpus under
# test_set/, which is gitignored and so absent on CI and on any fresh clone.
# This assertion is specifically *about* those real files, so it cannot be
# rewritten against a generated archive the way the thumbnail tests were --
# it skips instead. Matches the idiom already used for the RAR corpus in
# test_rar_reader_selection.py and the EPUB fixtures in tests/novel/.
_CORPUS_BOOK_IDS = frozenset({"mock-book-01", "mock-book-15"})
_CORPUS_PATHS = tuple(
    Path(book.file_path)
    for book in InMemoryBookRepository().list_books()
    if book.uuid in _CORPUS_BOOK_IDS
)
requires_local_corpus = pytest.mark.skipif(
    len(_CORPUS_PATHS) != len(_CORPUS_BOOK_IDS)
    or not all(path.is_file() for path in _CORPUS_PATHS),
    reason="the complete private test_set/ corpus required by this test is not present",
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
def test_in_memory_repository_resolves_test_set_archives() -> None:
    repository = InMemoryBookRepository()

    akane_book = next(book for book in repository.list_books() if book.uuid == "mock-book-01")
    pressure_book = next(book for book in repository.list_books() if book.uuid == "mock-book-15")

    assert akane_book.file_format == "CBZ"
    assert akane_book.page_count == 18
    assert Path(akane_book.file_path).exists()
    assert ArchiveImageService().open(akane_book.file_path).page_count == akane_book.page_count
    assert pressure_book.title == "Delicious in Dungeon v14"
    assert pressure_book.page_count == 192
    assert pressure_book.collection_ids == ()
    assert Path(pressure_book.file_path).exists()
    assert ArchiveImageService().open(pressure_book.file_path).page_count == pressure_book.page_count


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
