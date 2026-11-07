"""Hidden Space without a vault: the states, the earlier password, and the ways out.

Hiding, unhiding and the vault itself are covered with a real Library in
``test_hidden_space_vault.py`` and ``test_sealing_service.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from joyread.core.services.hidden_space_service import HiddenSpaceService, HiddenSpaceState
from joyread.core.services.library_service import LibraryService
from joyread.core.vault import VaultError
from joyread.infrastructure.config.settings_store import SettingsStore
from tests.support.hidden_space_fakes import seed_legacy_password
from tests.support.in_memory_book_repository import InMemoryBookRepository


def _service(tmp_path: Path) -> tuple[HiddenSpaceService, InMemoryBookRepository, SettingsStore]:
    repo = InMemoryBookRepository()
    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=tmp_path / "storage")
    return HiddenSpaceService(store, LibraryService(repo)), repo, store


def test_a_library_starts_with_no_hidden_space(tmp_path: Path) -> None:
    service, _repo, _store = _service(tmp_path)

    assert service.state is HiddenSpaceState.NONE
    assert service.has_legacy_password is False
    assert service.verify("anything") is False


def test_the_earlier_password_can_be_checked_but_no_longer_set(tmp_path: Path) -> None:
    service, _repo, store = _service(tmp_path)
    seed_legacy_password(store, "Pass1234")

    assert service.state is HiddenSpaceState.LEGACY
    assert service.verify("Pass1234") is True
    assert service.verify("Pass0000") is False
    # Soft hiding is gone: there is no way to make a new legacy password.
    assert not hasattr(service, "initialize")
    assert not hasattr(service, "change_password")


@pytest.mark.parametrize("legacy", [False, True])
def test_hiding_without_a_vault_is_refused(tmp_path: Path, legacy: bool) -> None:
    service, repo, store = _service(tmp_path)
    if legacy:
        seed_legacy_password(store, "Pass1234")

    with pytest.raises(VaultError) as raised:
        service.hide_books(("mock-book-03",))
    with pytest.raises(VaultError):
        service.unhide_books(("mock-book-03",))

    assert raised.value.code == "no_vault"
    assert not next(book for book in repo.list_books() if book.uuid == "mock-book-03").is_hidden


def test_demoting_a_hidable_collection_drops_its_hidden_books(tmp_path: Path) -> None:
    service, repo, _store = _service(tmp_path)
    service.set_collection_hidable("collection-a", True)
    repo.set_book_hidden("mock-book-03", True)
    assert "collection-a" in next(b for b in repo.list_books() if b.uuid == "mock-book-03").collection_ids

    service.set_collection_hidable("collection-a", False)

    after = next(book for book in repo.list_books() if book.uuid == "mock-book-03")
    # The book stays hidden; only the membership goes.
    assert after.is_hidden is True
    assert "collection-a" not in after.collection_ids


def test_a_legacy_revert_unhides_everything_and_keeps_the_password(tmp_path: Path) -> None:
    service, repo, store = _service(tmp_path)
    seed_legacy_password(store, "Pass1234")
    service.set_collection_hidable("collection-a", True)
    repo.set_book_hidden("mock-book-03", True)
    repo.set_book_hidden("mock-book-06", True)

    assert service.revert_all() == {}

    assert all(not book.is_hidden for book in repo.list_books())
    assert all(not collection.is_hidable for collection in repo.list_collections())
    assert service.state is HiddenSpaceState.LEGACY  # Revert is not reset.


def test_reset_erases_a_legacy_hidden_space(tmp_path: Path) -> None:
    service, repo, store = _service(tmp_path)
    seed_legacy_password(store, "Pass1234")
    store.update(show_hidden_collection=True)
    service.set_collection_hidable("collection-a", True)
    repo.set_book_hidden("mock-book-03", True)
    repo.set_book_hidden("mock-book-06", True)

    service.reset_and_erase()

    assert {"mock-book-03", "mock-book-06"}.isdisjoint(book.uuid for book in repo.list_books())
    assert repo.list_collections() == []
    settings = store.load()
    assert settings.hidden_space_password_hash is None
    assert settings.hidden_space_password_salt is None
    assert settings.show_hidden_collection is False
    assert service.state is HiddenSpaceState.NONE
