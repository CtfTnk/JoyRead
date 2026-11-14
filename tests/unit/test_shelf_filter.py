from joyread.ui.viewmodels.shelf_filter import (
    BookFilter,
    filterable_language_code,
    summarize_book_filter,
)


def test_empty_filter_matches_everything() -> None:
    assert BookFilter().is_active is False
    assert BookFilter().matches("ja", ())


def test_of_drops_blanks_and_duplicates_in_order() -> None:
    book_filter = BookFilter.of(("b", "", "a", "b"), ("ja", "ja", ""))

    assert book_filter == BookFilter(("b", "a"), ("ja",))


def test_languages_are_alternatives_and_tags_all_required() -> None:
    book_filter = BookFilter(("action", "comedy"), ("ja", "zh"))

    assert book_filter.matches("zh", ("action", "comedy", "drama"))
    assert not book_filter.matches("en", ("action", "comedy"))
    assert not book_filter.matches("ja", ("action",))


def test_summary_counts_languages_regardless_of_the_selected_language() -> None:
    books = [
        ("ja", ("action",)),
        ("ja", ("comedy",)),
        ("zh", ("action",)),
        ("en", ()),
    ]

    preview = summarize_book_filter(books, BookFilter(("action",), ("ja",)))

    assert preview.matching_books == 1
    assert preview.books_by_language == {"ja": 1, "zh": 1}


def test_unlisted_or_missing_language_is_unknown() -> None:
    known = {"en", "und"}

    assert filterable_language_code("en", known) == "en"
    assert filterable_language_code("ko", known) == "und"
    assert filterable_language_code(None, known) == "und"
