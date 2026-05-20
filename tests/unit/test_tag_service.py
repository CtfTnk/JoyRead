"""Tests for the TagService domain orchestration."""

from __future__ import annotations

import pytest

from joyread.core.models.tag import MAX_TAG_NAME_LENGTH, Tag, normalize_tag_name
from joyread.core.repositories.tag_repository import (
    TagNameConflictError,
    TagNotFoundError,
    TagRepository,
)
from joyread.core.services.tag_service import TagService


class _FakeTagRepository:
    def __init__(self) -> None:
        self._tags: dict[str, Tag] = {}
        self._next_id = 0
        self.linked: set[tuple[str, str]] = set()

    def _new_id(self) -> str:
        self._next_id += 1
        return f"tag-{self._next_id}"

    def list_tags(self) -> list[Tag]:
        return sorted(self._tags.values(), key=lambda t: t.name.casefold())

    def get_tag(self, tag_id: str) -> Tag | None:
        return self._tags.get(tag_id)

    def get_tag_by_normalized(self, normalized: str) -> Tag | None:
        key = normalized.casefold()
        for tag in self._tags.values():
            if tag.name_normalized == key:
                return tag
        return None

    def create(self, display_name: str) -> Tag:
        normalized = normalize_tag_name(display_name)
        existing = self.get_tag_by_normalized(normalized.casefold())
        if existing is not None:
            raise TagNameConflictError(f"A tag named '{existing.name}' already exists.")
        tag = Tag(tag_id=self._new_id(), name=normalized)
        self._tags[tag.tag_id] = tag
        return tag

    def find_or_create(self, display_name: str) -> Tag:
        normalized = normalize_tag_name(display_name)
        existing = self.get_tag_by_normalized(normalized.casefold())
        if existing is not None:
            return existing
        return self.create(display_name)

    def rename(self, tag_id: str, new_display_name: str) -> Tag:
        normalized = normalize_tag_name(new_display_name)
        current = self._tags.get(tag_id)
        if current is None:
            raise TagNotFoundError(f"Tag {tag_id} does not exist.")
        if current.name_normalized == normalized.casefold():
            renamed = Tag(tag_id=tag_id, name=normalized)
            self._tags[tag_id] = renamed
            return renamed
        collision = self.get_tag_by_normalized(normalized.casefold())
        if collision is not None:
            raise TagNameConflictError(f"A tag named '{collision.name}' already exists.")
        renamed = Tag(tag_id=tag_id, name=normalized)
        self._tags[tag_id] = renamed
        return renamed

    def delete(self, tag_id: str) -> int:
        if tag_id not in self._tags:
            return 0
        linked = sum(1 for tag, _ in self.linked if tag == tag_id)
        self.linked = {(t, b) for t, b in self.linked if t != tag_id}
        del self._tags[tag_id]
        return linked

    def link_book(self, tag_id: str, book_id: str) -> None:
        self.linked.add((tag_id, book_id))

    def unlink_book(self, tag_id: str, book_id: str) -> None:
        self.linked.discard((tag_id, book_id))

    def set_book_tag_ids(self, book_id: str, tag_ids: tuple[str, ...]) -> None:
        normalized_ids = tuple(dict.fromkeys(tag_id for tag_id in tag_ids if tag_id))
        self.linked = {(tag, book) for tag, book in self.linked if book != book_id}
        self.linked.update((tag_id, book_id) for tag_id in normalized_ids)

    def list_tag_ids_for_book(self, book_id: str) -> list[str]:
        return [tag for tag, book in self.linked if book == book_id]

    def list_tag_ids_for_books(self, book_ids: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
        def ordered_tags_for(book_id: str) -> tuple[str, ...]:
            tag_ids = [tag for tag, linked_book_id in self.linked if linked_book_id == book_id]
            return tuple(sorted(tag_ids, key=lambda tag_id: self._tags[tag_id].name_normalized))

        return {
            book_id: ordered_tags_for(book_id)
            for book_id in book_ids
        }


def test_normalize_tag_name_caps_first_character() -> None:
    assert normalize_tag_name("  comedy  ") == "Comedy"
    assert normalize_tag_name("ACTION") == "ACTION"
    assert normalize_tag_name("aBc") == "ABc"


def test_normalize_tag_name_rejects_empty_and_overlong() -> None:
    with pytest.raises(ValueError):
        normalize_tag_name("")
    with pytest.raises(ValueError):
        normalize_tag_name("   ")
    with pytest.raises(ValueError):
        normalize_tag_name("x" * (MAX_TAG_NAME_LENGTH + 1))


def test_tag_service_create_normalizes_and_persists() -> None:
    service = TagService(_FakeTagRepository())
    tag = service.create("comedy")
    assert tag.name == "Comedy"
    assert service.list_tags() == [tag]


def test_tag_service_create_raises_on_conflict() -> None:
    service = TagService(_FakeTagRepository())
    service.create("Action")
    with pytest.raises(TagNameConflictError):
        service.create("action")


def test_tag_service_create_propagates_value_error_on_empty() -> None:
    service = TagService(_FakeTagRepository())
    with pytest.raises(ValueError):
        service.create("   ")


def test_tag_service_rename_returns_updated_tag() -> None:
    service = TagService(_FakeTagRepository())
    tag = service.create("Action")
    renamed = service.rename(tag.tag_id, "drama")
    assert renamed.name == "Drama"


def test_tag_service_rename_to_conflict_raises() -> None:
    service = TagService(_FakeTagRepository())
    service.create("Comedy")
    action = service.create("Action")
    with pytest.raises(TagNameConflictError):
        service.rename(action.tag_id, "COMEDY")


def test_tag_service_find_or_create_invalid_returns_none() -> None:
    service = TagService(_FakeTagRepository())
    assert service.find_or_create("   ") is None
    assert service.find_or_create("x" * (MAX_TAG_NAME_LENGTH + 1)) is None
    assert service.list_tags() == []


def test_tag_service_find_or_create_idempotent() -> None:
    service = TagService(_FakeTagRepository())
    first = service.find_or_create("Comedy")
    again = service.find_or_create("comedy")
    assert first is not None
    assert again is not None
    assert first.tag_id == again.tag_id
    assert len(service.list_tags()) == 1


def test_tag_service_delete_returns_linked_count() -> None:
    repo = _FakeTagRepository()
    service = TagService(repo)
    tag = service.create("Comedy")
    service.link_book(tag.tag_id, "book-1")
    service.link_book(tag.tag_id, "book-2")
    assert service.delete(tag.tag_id) == 2
    assert service.list_tags() == []


def test_tag_service_lists_tag_ids_for_books() -> None:
    repo = _FakeTagRepository()
    service = TagService(repo)
    action = service.create("Action")
    comedy = service.create("Comedy")
    service.link_book(action.tag_id, "book-1")
    service.link_book(comedy.tag_id, "book-1")
    service.link_book(comedy.tag_id, "book-2")

    assert service.list_tag_ids_for_books(("book-1", "book-2", "book-3")) == {
        "book-1": (action.tag_id, comedy.tag_id),
        "book-2": (comedy.tag_id,),
        "book-3": (),
    }


def test_tag_service_replaces_book_tags() -> None:
    repo = _FakeTagRepository()
    service = TagService(repo)
    action = service.create("Action")
    comedy = service.create("Comedy")
    drama = service.create("Drama")
    service.link_book(action.tag_id, "book-1")
    service.link_book(comedy.tag_id, "book-1")

    service.set_book_tag_ids("book-1", (drama.tag_id, drama.tag_id, action.tag_id))

    assert service.list_tag_ids_for_books(("book-1",)) == {
        "book-1": (action.tag_id, drama.tag_id),
    }
