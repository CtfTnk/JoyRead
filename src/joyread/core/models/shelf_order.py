"""Persistent shelf preferences and pure sequence operations for manual sorting."""
from dataclasses import dataclass
from collections.abc import Iterable

from joyread.core.models.book import Book


@dataclass(frozen=True)
class ShelfOrder:
    scope: str
    sort_field: str
    ascending: bool = False
    initialized: bool = False
    book_ids: tuple[str, ...] = ()


def newest_first(books: Iterable[Book]) -> tuple[str, ...]:
    # Stable UUID ties, independent of repository row order.
    ordered = sorted(books, key=lambda book: book.uuid)
    ordered.sort(key=lambda book: book.added_at, reverse=True)
    return tuple(book.uuid for book in ordered)


def merge_order(books: Iterable[Book], saved: tuple[str, ...]) -> tuple[str, ...]:
    newest = newest_first(books)
    present = set(newest)
    known = set(saved)
    return tuple(key for key in newest if key not in known) + tuple(key for key in saved if key in present)


def move_group(order: tuple[str, ...], selected: set[str], before: str | None) -> tuple[str, ...]:
    moving = tuple(key for key in order if key in selected)
    remaining = [key for key in order if key not in selected]
    index = remaining.index(before) if before in remaining else len(remaining)
    return tuple(remaining[:index]) + moving + tuple(remaining[index:])


def replace_visible_order(full: tuple[str, ...], visible: tuple[str, ...]) -> tuple[str, ...]:
    """Keep temporarily invisible books in their slots while moving visible ones."""
    visible_ids = set(visible)
    replacements = iter(visible)
    return tuple(next(replacements) if key in visible_ids else key for key in full)
