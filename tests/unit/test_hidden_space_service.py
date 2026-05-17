"""Hidden Space service: password lifecycle + visibility mutations."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from joyread.core.models.book import Book
from joyread.core.models.collection import Collection
from joyread.core.services.hidden_space_service import (
    HiddenSpacePasswordError,
    HiddenSpaceService,
)
from joyread.core.services.library_service import LibraryService
from joyread.infrastructure.config.settings_store import SettingsStore
from tests.support.in_memory_book_repository import InMemoryBookRepository


def _store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(
        support_root=tmp_path / "support",
        default_storage_root=tmp_path / "storage",
    )


def _service(tmp_path: Path, repo: InMemoryBookRepository | None = None) -> tuple[
    HiddenSpaceService,
    InMemoryBookRepository,
    SettingsStore,
]:
    repo = repo or InMemoryBookRepository()
    library = LibraryService(repo)
    service = HiddenSpaceService(_store(tmp_path), library)
    return service, repo, service._settings_store  # type: ignore[attr-defined]


def test_service_starts_uninitiated(tmp_path: Path) -> None:
    service, _repo, _store = _service(tmp_path)

    assert service.is_initialized is False
    assert service.hint is None
    assert service.verify("anything") is False


def test_initialize_rejects_weak_passwords(tmp_path: Path) -> None:
    service, _repo, _store = _service(tmp_path)

    with pytest.raises(HiddenSpacePasswordError):
        service.initialize("abc", "abc", "hint")
    with pytest.raises(HiddenSpacePasswordError):
        service.initialize("hé!!", "hé!!", "hint")  # non-ASCII / punctuation
    with pytest.raises(HiddenSpacePasswordError):
        service.initialize("Pass1234", "Mismatch", "hint")

    assert service.is_initialized is False


def test_initialize_sets_password_hint_and_toggle(tmp_path: Path) -> None:
    service, _repo, store = _service(tmp_path)

    service.initialize("Pass1234", "Pass1234", "remember the dog")

    assert service.is_initialized is True
    assert service.hint == "remember the dog"
    settings = store.load()
    # The toggle goes on automatically — setup is itself the "turn on"
    # action; we don't ask the user to flip it a second time.
    assert settings.show_hidden_collection is True
    assert settings.hidden_space_password_hash is not None
    assert settings.hidden_space_password_salt is not None
    assert service.verify("Pass1234") is True
    assert service.verify("Pass0000") is False


def test_change_password_requires_old_password(tmp_path: Path) -> None:
    service, _repo, _store = _service(tmp_path)
    service.initialize("Pass1234", "Pass1234", None)

    with pytest.raises(HiddenSpacePasswordError):
        service.change_password("wrong", "NewPass1", "NewPass1")

    service.change_password("Pass1234", "NewPass1", "NewPass1", hint="new hint")
    assert service.verify("Pass1234") is False
    assert service.verify("NewPass1") is True
    assert service.hint == "new hint"


def test_hide_book_clears_favourite_recent_and_normal_collection_membership(tmp_path: Path) -> None:
    repo = InMemoryBookRepository()
    service, repo, _store = _service(tmp_path, repo)
    service.initialize("Pass1234", "Pass1234", None)
    # mock-book-03 is favourited + has a last_read_at + sits in "collection-a"
    # (a normal collection). After hiding, all three should be cleared.
    before = next(book for book in repo.list_books() if book.uuid == "mock-book-03")
    assert before.is_favourite is True
    assert before.last_read_at is not None
    assert "collection-a" in before.collection_ids

    service.hide_book("mock-book-03")

    after = next(book for book in repo.list_books() if book.uuid == "mock-book-03")
    assert after.is_hidden is True
    assert after.is_favourite is False
    assert after.last_read_at is None
    assert "collection-a" not in after.collection_ids


def test_hide_book_keeps_membership_in_hidable_collection(tmp_path: Path) -> None:
    repo = InMemoryBookRepository()
    service, repo, _store = _service(tmp_path, repo)
    service.initialize("Pass1234", "Pass1234", None)
    # Mark "collection-a" hidable so hidden books are allowed to stay.
    service.set_collection_hidable("collection-a", True)

    service.hide_book("mock-book-03")

    after = next(book for book in repo.list_books() if book.uuid == "mock-book-03")
    assert after.is_hidden is True
    assert "collection-a" in after.collection_ids


def test_demoting_hidable_collection_drops_hidden_books(tmp_path: Path) -> None:
    repo = InMemoryBookRepository()
    service, repo, _store = _service(tmp_path, repo)
    service.initialize("Pass1234", "Pass1234", None)
    service.set_collection_hidable("collection-a", True)
    service.hide_book("mock-book-03")
    assert "collection-a" in next(b for b in repo.list_books() if b.uuid == "mock-book-03").collection_ids

    service.set_collection_hidable("collection-a", False)

    after = next(book for book in repo.list_books() if book.uuid == "mock-book-03")
    # Book remains hidden — only the membership is dropped.
    assert after.is_hidden is True
    assert "collection-a" not in after.collection_ids


def test_revert_all_clears_hidden_and_hidable_flags(tmp_path: Path) -> None:
    repo = InMemoryBookRepository()
    service, repo, _store = _service(tmp_path, repo)
    service.initialize("Pass1234", "Pass1234", None)
    service.set_collection_hidable("collection-a", True)
    service.hide_book("mock-book-03")
    service.hide_book("mock-book-06")

    service.revert_all()

    assert all(not book.is_hidden for book in repo.list_books())
    assert all(not collection.is_hidable for collection in repo.list_collections())
    # Password + toggle stay configured (user asked for revert, not reset).
    assert service.is_initialized is True


def test_reset_and_erase_deletes_hidden_books_and_hidable_collections(tmp_path: Path) -> None:
    repo = InMemoryBookRepository()
    service, repo, store = _service(tmp_path, repo)
    service.initialize("Pass1234", "Pass1234", None)
    service.set_collection_hidable("collection-a", True)
    service.hide_book("mock-book-03")
    service.hide_book("mock-book-06")
    hidden_uuids = {"mock-book-03", "mock-book-06"}

    service.reset_and_erase()

    remaining_ids = {book.uuid for book in repo.list_books()}
    assert hidden_uuids.isdisjoint(remaining_ids)
    assert repo.list_collections() == []
    settings = store.load()
    assert settings.hidden_space_password_hash is None
    assert settings.hidden_space_password_salt is None
    assert settings.hidden_space_password_hint is None
    assert settings.show_hidden_collection is False
    assert service.is_initialized is False
