from joyread.core.repositories.mock_book_repository import MockBookRepository


def test_mock_repository_returns_stable_varied_books() -> None:
    repository = MockBookRepository()

    books = repository.list_books()

    assert len(books) == 14
    assert books[0].uuid == "mock-book-01"
    assert {book.file_format for book in books} >= {"CBZ", "PDF", "EPUB"}
    assert any(book.is_favourite for book in books)
    assert any(book.is_missing for book in books)
    assert repository.list_collections()[0].name == "A Collection"
