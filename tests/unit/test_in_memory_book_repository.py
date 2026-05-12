from pathlib import Path

from joyread.core.archive import ArchiveImageService
from tests.support.in_memory_book_repository import InMemoryBookRepository


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
