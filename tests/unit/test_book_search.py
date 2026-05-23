from joyread.core.search import (
    build_book_search_document,
    matches_book_search,
    parse_book_search_query,
)


def _matches(title: str, author: str | None, query: str) -> bool:
    document = build_book_search_document("book-1", title, author)
    return matches_book_search(document, parse_book_search_query(query))


def test_book_search_matches_title_and_author() -> None:
    assert _matches("Spy x Family Vol. 1", "Tatsuya Endo", "spy")
    assert _matches("Spy x Family Vol. 1", "Tatsuya Endo", "endo")


def test_book_search_uses_unordered_any_segment_matching() -> None:
    assert _matches("Alpha Book", None, "missing alpha")
    assert _matches("Alpha Book", "Beta Author", "beta missing")
    assert not _matches("Alpha Book", "Beta Author", "missing absent")


def test_book_search_does_not_split_multi_character_terms_into_letters() -> None:
    assert _matches("ABC Egg", None, "abc")
    assert not _matches("A B C Egg", None, "abc")


def test_book_search_single_latin_character_matches_only_standalone_terms() -> None:
    assert _matches("A Book", None, "a")
    assert _matches("Spy x Family", None, "x")
    assert not _matches("Apple Class", None, "a")
    assert not _matches("Apple Class", None, "c")


def test_book_search_matches_cjk_substrings_without_spaces() -> None:
    assert _matches("我的漫画书", None, "漫")
    assert _matches("我的漫画书", None, "画书")


def test_book_search_uses_unicode_normalization_and_casefolding() -> None:
    assert _matches("Ｆｒｉｅｒｅｎ", None, "frieren")
    assert _matches("Straße Notes", None, "STRASSE")
