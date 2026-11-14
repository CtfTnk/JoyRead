"""Qt-free rules for the Library's language and tag filter.

A book has exactly one language, so selected languages are alternatives: an
AND across them could never match anything. Tags are additive labels and keep
their narrowing meaning -- a book must carry every selected tag. The two groups
then intersect.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from joyread.core.models.archive_metadata import UNKNOWN_LANGUAGE_TAG


@dataclass(frozen=True)
class BookFilter:
    tag_ids: tuple[str, ...] = ()
    language_codes: tuple[str, ...] = ()

    @classmethod
    def of(cls, tag_ids: Iterable[str], language_codes: Iterable[str]) -> "BookFilter":
        return cls(
            tag_ids=tuple(dict.fromkeys(tag_id for tag_id in tag_ids if tag_id)),
            language_codes=tuple(dict.fromkeys(code for code in language_codes if code)),
        )

    @property
    def is_active(self) -> bool:
        return bool(self.tag_ids or self.language_codes)

    def matches_tags(self, book_tag_ids: Collection[str]) -> bool:
        return set(self.tag_ids).issubset(book_tag_ids)

    def matches_language(self, language_code: str) -> bool:
        return not self.language_codes or language_code in self.language_codes

    def matches(self, language_code: str, book_tag_ids: Collection[str]) -> bool:
        return self.matches_language(language_code) and self.matches_tags(book_tag_ids)


@dataclass(frozen=True)
class BookFilterPreview:
    """What a filter would show if it were confirmed now.

    ``books_by_language`` honours the selected tags but not the selected
    languages, so each count answers "how many books would this language
    give me" even while another language is selected.
    """

    matching_books: int
    books_by_language: Mapping[str, int]


def summarize_book_filter(
    books: Iterable[tuple[str, Collection[str]]],
    book_filter: BookFilter,
) -> BookFilterPreview:
    """Preview *book_filter* over ``(language code, tag ids)`` pairs."""

    matching_books = 0
    books_by_language: Counter[str] = Counter()
    for language_code, book_tag_ids in books:
        if not book_filter.matches_tags(book_tag_ids):
            continue
        books_by_language[language_code] += 1
        if book_filter.matches_language(language_code):
            matching_books += 1
    return BookFilterPreview(matching_books, dict(books_by_language))


def filterable_language_code(language_tag: str | None, known_codes: Collection[str]) -> str:
    """Map a stored language onto a code the filter offers.

    The book repository already reads a code outside the ``languages`` table
    as "Unknown"; the filter has to agree or that book could never be found.
    """

    return language_tag if language_tag in known_codes else UNKNOWN_LANGUAGE_TAG


__all__ = [
    "BookFilter",
    "BookFilterPreview",
    "filterable_language_code",
    "summarize_book_filter",
]
