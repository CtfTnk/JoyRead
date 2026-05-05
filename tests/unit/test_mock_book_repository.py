from pathlib import Path

from joyread.core.archive import ArchiveImageService
from joyread.core.repositories.mock_book_repository import MockBookRepository


def test_mock_repository_returns_stable_varied_books() -> None:
    repository = MockBookRepository()

    books = repository.list_books()

    assert len(books) == 15
    assert books[0].uuid == "mock-book-01"
    assert {book.file_format for book in books} >= {"CBZ", "PDF", "EPUB"}
    assert max(book.page_count for book in books) > min(book.page_count for book in books)
    assert any(book.is_favourite for book in books)
    assert any(book.is_missing for book in books)
    assert repository.list_collections()[0].name == "A Collection"


def test_mock_repository_resolves_bundled_cbz_fixture() -> None:
    repository = MockBookRepository()

    sample_book = next(book for book in repository.list_books() if book.uuid == "mock-book-15")

    assert sample_book.file_format == "CBZ"
    assert sample_book.page_count == 4
    assert sample_book.collection_ids == ()
    assert Path(sample_book.file_path).exists()
    assert ArchiveImageService().open(sample_book.file_path).page_count == sample_book.page_count
