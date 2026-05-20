"""Domain orchestration around the tag repository.

Centralises validation so the same rules apply whether tags are created
from the Settings page or from a JSON import manifest. Errors are
re-raised as `ValueError` (validation) or
:class:`TagNameConflictError` / :class:`TagNotFoundError` (domain) so
callers can map them to user-visible dialogs.
"""

from __future__ import annotations

import logging

from joyread.core.models.tag import MAX_TAG_NAME_LENGTH, Tag, normalize_tag_name
from joyread.core.repositories.tag_repository import (
    TagNameConflictError,
    TagNotFoundError,
    TagRepository,
)


logger = logging.getLogger(__name__)


__all__ = [
    "TagService",
    "TagNameConflictError",
    "TagNotFoundError",
    "MAX_TAG_NAME_LENGTH",
]


class TagService:
    def __init__(self, repository: TagRepository) -> None:
        self._repository = repository

    @property
    def repository(self) -> TagRepository:
        return self._repository

    def list_tags(self) -> list[Tag]:
        return self._repository.list_tags()

    def list_tag_ids_for_books(self, book_ids: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
        return self._repository.list_tag_ids_for_books(book_ids)

    def create(self, raw_name: str) -> Tag:
        # ``normalize_tag_name`` raises ``ValueError`` on empty / overlong.
        return self._repository.create(raw_name)

    def rename(self, tag_id: str, raw_name: str) -> Tag:
        return self._repository.rename(tag_id, raw_name)

    def delete(self, tag_id: str) -> int:
        return self._repository.delete(tag_id)

    def find_or_create(self, raw_name: str) -> Tag | None:
        """Used by the import flow: silently return ``None`` for invalid
        names so one bad tag does not abort the importing item."""
        try:
            normalize_tag_name(raw_name)
        except ValueError as exc:
            logger.warning("find_or_create rejected raw=%r reason=%s", raw_name, exc)
            return None
        return self._repository.find_or_create(raw_name)

    def link_book(self, tag_id: str, book_id: str) -> None:
        self._repository.link_book(tag_id, book_id)

    def unlink_book(self, tag_id: str, book_id: str) -> None:
        self._repository.unlink_book(tag_id, book_id)
