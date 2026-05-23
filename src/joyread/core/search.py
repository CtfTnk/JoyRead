"""Lightweight title/author search helpers for local library filtering."""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata


@dataclass(frozen=True)
class BookSearchQuery:
    substring_terms: tuple[str, ...] = ()
    exact_terms: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.substring_terms and not self.exact_terms


@dataclass(frozen=True)
class BookSearchDocument:
    book_uuid: str
    text: str
    standalone_terms: frozenset[str]


def parse_book_search_query(query: str) -> BookSearchQuery:
    substring_terms: list[str] = []
    exact_terms: list[str] = []
    for token in _unique_terms(_split_search_terms(_normalize_text(query))):
        if _requires_standalone_match(token):
            exact_terms.append(token)
        else:
            substring_terms.append(token)
    return BookSearchQuery(tuple(substring_terms), tuple(exact_terms))


def build_book_search_document(book_uuid: str, title: str, author: str | None) -> BookSearchDocument:
    text = " ".join(part for part in (_normalize_text(title), _normalize_text(author or "")) if part)
    return BookSearchDocument(
        book_uuid=book_uuid,
        text=text,
        standalone_terms=frozenset(term for term in _split_search_terms(text) if _requires_standalone_match(term)),
    )


def matches_book_search(document: BookSearchDocument, query: BookSearchQuery) -> bool:
    if query.is_empty:
        return True
    return any(term in document.text for term in query.substring_terms) or any(
        term in document.standalone_terms for term in query.exact_terms
    )


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _split_search_terms(value: str) -> tuple[str, ...]:
    terms: list[str] = []
    current: list[str] = []
    for char in value:
        if _is_separator(char):
            if current:
                terms.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        terms.append("".join(current))
    return tuple(terms)


def _unique_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    unique: dict[str, None] = {}
    for term in terms:
        if term:
            unique.setdefault(term, None)
    return tuple(unique)


def _is_separator(char: str) -> bool:
    category = unicodedata.category(char)
    return char.isspace() or category[0] in {"Z", "P", "S", "C"}


def _requires_standalone_match(token: str) -> bool:
    return len(token) == 1 and token.isalnum() and not _is_cjk_like_char(token)


def _is_cjk_like_char(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0x2A700 <= codepoint <= 0x2B73F
        or 0x2B740 <= codepoint <= 0x2B81F
        or 0x2B820 <= codepoint <= 0x2CEAF
        or 0x2CEB0 <= codepoint <= 0x2EBEF
        or 0x30000 <= codepoint <= 0x3134F
        or 0x3040 <= codepoint <= 0x30FF
        or 0x1100 <= codepoint <= 0x11FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )
