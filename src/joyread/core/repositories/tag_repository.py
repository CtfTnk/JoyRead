"""Tag repository contracts and domain errors."""

from __future__ import annotations

from typing import Protocol

from joyread.core.models.tag import Tag


class TagNotFoundError(Exception):
    """Raised when a tag id has no corresponding row."""


class TagNameConflictError(Exception):
    """Raised when a create/rename would collide with an existing tag name
    under case-insensitive equality."""


class TagRepository(Protocol):
    def list_tags(self) -> list[Tag]:
        ...

    def get_tag(self, tag_id: str) -> Tag | None:
        ...

    def get_tag_by_normalized(self, normalized: str) -> Tag | None:
        ...

    def create(self, display_name: str) -> Tag:
        """Create a tag. Raises ``TagNameConflictError`` if the normalized
        name already exists."""
        ...

    def find_or_create(self, display_name: str) -> Tag:
        """Return an existing tag matching ``display_name`` case-insensitively,
        or create a new one. Used by the JSON import flow."""
        ...

    def rename(self, tag_id: str, new_display_name: str) -> Tag:
        """Rename a tag. Raises ``TagNotFoundError`` if the id is unknown,
        ``TagNameConflictError`` if the new name collides with another tag."""
        ...

    def delete(self, tag_id: str) -> int:
        """Delete a tag. Returns the number of books that were linked to it
        (so callers can report 'N books unlinked'). Cascade unlinks all
        ``book_tags`` rows via FK. No-op (returns 0) if the tag is absent."""
        ...

    def link_book(self, tag_id: str, book_id: str) -> None:
        ...

    def unlink_book(self, tag_id: str, book_id: str) -> None:
        ...

    def list_tag_ids_for_book(self, book_id: str) -> list[str]:
        ...
