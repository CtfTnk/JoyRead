"""Hidden Space once a vault exists: setup, the legacy upgrade, revert and reset."""

from __future__ import annotations

from pathlib import Path

import pytest

from joyread.core.repositories.sqlite_sealing_repository import UnhideOutcome
from joyread.core.services.hidden_space_service import (
    HiddenSpacePasswordError,
    HiddenSpaceService,
    HiddenSpaceState,
)
from joyread.core.services.library_service import LibraryService
from joyread.core.vault import VaultError
from joyread.infrastructure.config.settings_store import SettingsStore
from joyread.infrastructure.database import DatabasePriority
from tests.support.vault_library import VaultLibrary, build_vault_library, sample_pages, write_cbz


@pytest.fixture
def library(tmp_path: Path):
    lib = build_vault_library(tmp_path)
    yield lib
    lib.close()


@pytest.fixture
def settings_store(tmp_path: Path) -> SettingsStore:
    # Explicit roots: the defaults point a source checkout at its own
    # .joyread_support, the developer's real settings.
    return SettingsStore(support_root=tmp_path / "settings", default_storage_root=tmp_path / "storage")


@pytest.fixture
def hidden_space(library: VaultLibrary, settings_store: SettingsStore) -> HiddenSpaceService:
    return HiddenSpaceService(
        settings_store,
        LibraryService(library.books),
        vault=library.vault,
        sealing=library.sealing,
    )


def _books(library: VaultLibrary, tmp_path: Path, count: int = 2) -> list[str]:
    return [
        library.import_file(write_cbz(tmp_path / "incoming" / f"Book {index}.cbz", sample_pages(2)))
        for index in range(count)
    ]


def _confirm(draft) -> str:  # noqa: ANN001
    return draft.recovery_code.formatted()


def test_the_state_follows_setup_and_locking(hidden_space: HiddenSpaceService) -> None:
    assert hidden_space.state is HiddenSpaceState.NONE

    draft = hidden_space.prepare_vault("1234", "1234")
    assert hidden_space.state is HiddenSpaceState.NONE
    hidden_space.create_vault(draft, _confirm(draft))
    assert hidden_space.state is HiddenSpaceState.UNLOCKED

    hidden_space.lock()
    assert hidden_space.state is HiddenSpaceState.LOCKED
    hidden_space.unlock("1234")
    assert hidden_space.state is HiddenSpaceState.UNLOCKED


def test_without_a_vault_hiding_is_still_only_a_flag(
    hidden_space: HiddenSpaceService, library: VaultLibrary, tmp_path: Path
) -> None:
    book, _other = _books(library, tmp_path)
    hidden_space.initialize("abcd", "abcd")

    assert hidden_space.state is HiddenSpaceState.LEGACY
    assert hidden_space.hide_books((book,)) == ()

    assert library.file_row(book)["is_hidden"] == 1
    assert library.sealing.jobs_by_book() == {}
    assert library.file_row(book)["storage_kind"] == "verbatim"


def test_the_upgrade_keeps_the_old_password_and_seals_what_was_hidden(
    hidden_space: HiddenSpaceService,
    library: VaultLibrary,
    settings_store: SettingsStore,
    tmp_path: Path,
) -> None:
    hidden, visible = _books(library, tmp_path)
    hidden_space.initialize("abcd1234", "abcd1234")
    hidden_space.hide_books((hidden,))
    with pytest.raises(VaultError):
        hidden_space.prepare_vault("other", "other")  # Must upgrade, not start over.

    with pytest.raises(HiddenSpacePasswordError):
        hidden_space.prepare_upgrade("wrong")
    draft = hidden_space.prepare_upgrade("abcd1234")
    assert settings_store.load().hidden_space_password_hash is not None  # Nothing changed yet.

    assert hidden_space.complete_upgrade(draft, _confirm(draft)) == 1

    settings = settings_store.load()
    assert settings.hidden_space_password_hash is None and settings.hidden_space_password_salt is None
    assert hidden_space.state is HiddenSpaceState.UNLOCKED
    library.sealing.run_pending()
    assert library.file_row(hidden)["storage_kind"] == "sealed"
    assert library.file_row(visible)["storage_kind"] == "verbatim"
    hidden_space.lock()
    hidden_space.unlock("abcd1234")  # The same password, now a vault passphrase.


def test_hiding_and_unhiding_go_through_sealing_once_a_vault_exists(
    hidden_space: HiddenSpaceService, library: VaultLibrary, tmp_path: Path
) -> None:
    book, _other = _books(library, tmp_path)
    draft = hidden_space.prepare_vault("1234", "1234")
    hidden_space.create_vault(draft, _confirm(draft))

    assert hidden_space.hide_books((book,)) == (book,)
    library.sealing.run_pending()
    assert library.file_row(book)["storage_kind"] == "sealed"

    assert hidden_space.unhide_books((book,), wait_seconds=0) == {book: UnhideOutcome.UNSEALING}
    library.sealing.run_pending()
    row = library.file_row(book)
    assert (row["is_hidden"], row["storage_kind"]) == (0, "canonical")


def test_setting_up_a_vault_seals_books_already_hidden(
    hidden_space: HiddenSpaceService, library: VaultLibrary, tmp_path: Path
) -> None:
    book, _other = _books(library, tmp_path)
    library.books.set_book_hidden(book, True)

    draft = hidden_space.prepare_vault("1234", "1234")
    assert hidden_space.create_vault(draft, _confirm(draft)) == 1


def test_revert_decrypts_everything_and_keeps_collection_memberships(
    hidden_space: HiddenSpaceService, library: VaultLibrary, tmp_path: Path
) -> None:
    first, second = _books(library, tmp_path)
    service = LibraryService(library.books)
    collection = service.create_collection("Private shelf")
    service.add_books_to_collection((first, second), collection.uuid)
    hidden_space.set_collection_hidable(collection.uuid, True)
    draft = hidden_space.prepare_vault("1234", "1234")
    hidden_space.create_vault(draft, _confirm(draft))
    hidden_space.hide_books((first, second))
    library.sealing.run_pending()

    outcomes = hidden_space.revert_all(wait_seconds=0)

    assert set(outcomes.values()) == {UnhideOutcome.UNSEALING}
    assert collection.uuid not in service.list_hidable_collection_ids()
    library.sealing.run_pending()
    for book in (first, second):
        row = library.file_row(book)
        assert (row["is_hidden"], row["storage_kind"]) == (0, "canonical")
        assert collection.uuid in service.get_book(book).collection_ids


def test_reset_deletes_the_vault_and_every_sealed_book(
    hidden_space: HiddenSpaceService, library: VaultLibrary, settings_store: SettingsStore, tmp_path: Path
) -> None:
    hidden, visible = _books(library, tmp_path)
    draft = hidden_space.prepare_vault("1234", "1234")
    hidden_space.create_vault(draft, _confirm(draft))
    hidden_space.hide_books((hidden,))
    library.sealing.run_pending()
    sealed = library.stored_path(hidden)

    hidden_space.reset_and_erase()

    assert hidden_space.state is HiddenSpaceState.NONE
    assert not library.keyring.is_unlocked
    assert not sealed.exists()
    assert library.file_row(hidden) is None
    assert library.file_row(visible) is not None
    assert not settings_store.load().show_hidden_collection
    remaining = library.database.execute(
        lambda connection: connection.execute("SELECT COUNT(*) FROM vault_slots").fetchone()[0],
        DatabasePriority.HIGH,
    )
    assert remaining == 0
