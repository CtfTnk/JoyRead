"""Migration 15 and the vault repository.

Migration 15 must be invisible to an existing Library: every row keeps its
meaning, and nothing reads the new tables until a book is sealed.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from joyread.core.models.storage_layout import (
    STORAGE_KIND_CANONICAL,
    STORAGE_KIND_SEALED,
    STORAGE_KIND_VERBATIM,
    is_content_addressed,
    storage_target,
)
from joyread.core.models.vault import VaultAlreadyExistsError, VaultJobDirection, VaultJobPhase
from joyread.core.repositories.sqlite_vault_repository import SqliteVaultRepository
from joyread.core.vault import (
    SLOT_TYPE_PASSPHRASE,
    SLOT_TYPE_RECOVERY,
    RecoveryCode,
    ScryptCost,
    UnlockFailedError,
    VaultPrivateKey,
    create_passphrase_slot,
    create_recovery_slot,
    unlock_with_passphrase,
    unlock_with_recovery_code,
)
from joyread.infrastructure.database import LATEST_SCHEMA_VERSION, apply_migrations
from joyread.infrastructure.database.database_interpreter import DatabaseInterpreter, DatabasePriority
from joyread.infrastructure.database.migrations import MIGRATIONS, _apply_callable_migration
from joyread.infrastructure.database.sqlite_connection import open_sqlite_connection


FAST = ScryptCost(n=1 << 14)
NOW = "2026-11-07T00:00:00"


def _migrate_to(connection: sqlite3.Connection, last_version: int) -> None:
    connection.create_function("joyread_basename", 1, lambda value: Path(str(value or "book")).name)
    connection.create_function("joyread_storage_relative", 1, lambda value: value)
    for version, migration in MIGRATIONS:
        if version > last_version:
            break
        if isinstance(migration, str):
            connection.executescript(
                f"BEGIN; {migration} INSERT INTO schema_migrations(version) VALUES ({version}); COMMIT;"
            )
        else:
            _apply_callable_migration(connection, version, migration)


def _insert_book(connection: sqlite3.Connection, book_id: str, file_id: str, kind: str) -> None:
    connection.execute(
        """
        INSERT INTO book_files(
            file_id, original_path, original_file_name, storage_path, file_format,
            hash_algorithm, source_hash, stored_hash, storage_kind, state, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'CBZ', 'sha256', ?, ?, ?, 'healthy', ?, ?)
        """,
        (file_id, f"/src/{file_id}.cbz", f"{file_id}.cbz", f"Books/{file_id}.cbz",
         f"source-{file_id}", f"stored-{file_id}", kind, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO books(book_id, file_id, title, author, language_tag, book_type,
                          cover_path, is_favourite, created_at, updated_at)
        VALUES (?, ?, 'Title', 'Author', 'und', 'manga', NULL, 0, ?, ?)
        """,
        (book_id, file_id, NOW, NOW),
    )


def test_migration_15_keeps_every_row_and_learns_sealed(tmp_path: Path) -> None:
    connection = open_sqlite_connection(tmp_path / "v14.sqlite3")
    try:
        _migrate_to(connection, 14)
        _insert_book(connection, "book-1", "file-1", STORAGE_KIND_VERBATIM)
        _insert_book(connection, "book-2", "file-2", STORAGE_KIND_CANONICAL)
        connection.execute("UPDATE books SET is_hidden = 1 WHERE book_id = 'book-2'")
        before = [tuple(row) for row in connection.execute("SELECT * FROM book_files ORDER BY file_id")]

        apply_migrations(connection)

        after = [tuple(row) for row in connection.execute("SELECT * FROM book_files ORDER BY file_id")]
        linked = connection.execute(
            "SELECT book_id, file_id, is_hidden FROM books ORDER BY book_id"
        ).fetchall()
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}

        assert after == before
        assert [tuple(row) for row in linked] == [("book-1", "file-1", 0), ("book-2", "file-2", 1)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {"vaults", "vault_slots", "vault_jobs"} <= tables
        assert {
            "idx_book_files_source_hash", "idx_book_files_stored_hash", "idx_book_files_state",
            "idx_vault_jobs_book",
        } <= indexes
        assert max(row["version"] for row in connection.execute("SELECT version FROM schema_migrations")) == 15

        _insert_book(connection, "book-3", "file-3", STORAGE_KIND_SEALED)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_book(connection, "book-4", "file-4", "encrypted")
        # The rebuild must not have weakened the books -> book_files cascade.
        connection.execute("DELETE FROM book_files WHERE file_id = 'file-1'")
        assert connection.execute("SELECT 1 FROM books WHERE book_id = 'book-1'").fetchone() is None
    finally:
        connection.close()


def test_a_fresh_library_is_created_at_version_15(tmp_path: Path) -> None:
    database = DatabaseInterpreter(tmp_path / "fresh.sqlite3")
    try:
        database.execute(apply_migrations, DatabasePriority.CRITICAL)
        versions = database.execute(
            lambda connection: [row["version"] for row in connection.execute("SELECT version FROM schema_migrations")]
        )
        assert LATEST_SCHEMA_VERSION == 15
        assert max(versions) == 15
    finally:
        database.close()


def test_sealed_files_are_addressed_by_file_id() -> None:
    books = Path("/library/Books")

    assert storage_target(books, storage_kind=STORAGE_KIND_SEALED, stored_hash="ab" * 32,
                          file_id="f1e2d3", suffix=".jrl") == books / "f1" / "f1e2d3.jrl"
    assert storage_target(books, storage_kind=STORAGE_KIND_VERBATIM, stored_hash="ab" * 32,
                          file_id="f1e2d3", suffix=".cbz") == books / "ab" / f"{'ab' * 32}.cbz"
    assert is_content_addressed(STORAGE_KIND_VERBATIM)
    assert not is_content_addressed(STORAGE_KIND_CANONICAL)
    assert not is_content_addressed(STORAGE_KIND_SEALED)
    assert not is_content_addressed("some-future-kind")


@pytest.fixture
def database(tmp_path: Path):  # noqa: ANN201 - fixture
    interpreter = DatabaseInterpreter(tmp_path / "joyread.sqlite3")
    interpreter.execute(apply_migrations, DatabasePriority.CRITICAL)
    yield interpreter
    interpreter.close()


def test_a_vault_round_trips_and_unlocks_from_storage(database: DatabaseInterpreter) -> None:
    repository = SqliteVaultRepository(database)
    key = VaultPrivateKey.generate()
    code = RecoveryCode.generate()
    vault_id = "vault-1"
    slots = [
        create_passphrase_slot(vault_id, key, "first passphrase!", cost=FAST),
        create_recovery_slot(vault_id, key, code),
    ]

    created = repository.create_vault(key.public_key, slots, vault_id=vault_id)
    stored = repository.get_vault()

    assert stored == created
    assert stored.public_key == key.public_key
    loaded = repository.list_slots(vault_id)
    assert {slot.slot_type for slot in loaded} == {SLOT_TYPE_PASSPHRASE, SLOT_TYPE_RECOVERY}
    assert unlock_with_passphrase(vault_id, loaded, "first passphrase!", stored.public_key).raw() == key.raw()
    assert unlock_with_recovery_code(vault_id, loaded, code, stored.public_key).raw() == key.raw()


def test_one_vault_per_library_and_never_without_a_slot(database: DatabaseInterpreter) -> None:
    repository = SqliteVaultRepository(database)
    key = VaultPrivateKey.generate()
    repository.create_vault(key.public_key, [create_recovery_slot("v", key, RecoveryCode.generate())], vault_id="v")

    with pytest.raises(VaultAlreadyExistsError):
        repository.create_vault(key.public_key, [create_recovery_slot("w", key, RecoveryCode.generate())], vault_id="w")
    with pytest.raises(ValueError):
        repository.create_vault(key.public_key, [], vault_id="x")
    assert repository.get_vault().vault_id == "v"


def test_replacing_the_passphrase_slot_leaves_the_recovery_slot(database: DatabaseInterpreter) -> None:
    repository = SqliteVaultRepository(database)
    key = VaultPrivateKey.generate()
    code = RecoveryCode.generate()
    repository.create_vault(
        key.public_key,
        [create_passphrase_slot("v", key, "old passphrase!!", cost=FAST), create_recovery_slot("v", key, code)],
        vault_id="v",
    )

    repository.replace_slots("v", SLOT_TYPE_PASSPHRASE, create_passphrase_slot("v", key, "new passphrase!!", cost=FAST))
    slots = repository.list_slots("v")

    assert sum(slot.slot_type == SLOT_TYPE_PASSPHRASE for slot in slots) == 1
    with pytest.raises(UnlockFailedError):
        unlock_with_passphrase("v", slots, "old passphrase!!", key.public_key)
    assert unlock_with_passphrase("v", slots, "new passphrase!!", key.public_key).raw() == key.raw()
    assert unlock_with_recovery_code("v", slots, code, key.public_key).raw() == key.raw()

    with pytest.raises(ValueError):
        repository.replace_slots("v", SLOT_TYPE_RECOVERY, create_passphrase_slot("v", key, "mismatched type", cost=FAST))
    with pytest.raises(LookupError):
        repository.replace_slots("gone", SLOT_TYPE_RECOVERY, create_recovery_slot("gone", key, code))


def test_deleting_the_vault_removes_its_slots(database: DatabaseInterpreter) -> None:
    repository = SqliteVaultRepository(database)
    key = VaultPrivateKey.generate()
    repository.create_vault(key.public_key, [create_recovery_slot("v", key, RecoveryCode.generate())], vault_id="v")

    repository.delete_vault("v")

    assert repository.get_vault() is None
    assert repository.list_slots("v") == []


def test_jobs_queue_one_per_book_and_vanish_with_the_book(database: DatabaseInterpreter) -> None:
    repository = SqliteVaultRepository(database)
    database.execute(lambda connection: _insert_book(connection, "book-1", "file-1", STORAGE_KIND_VERBATIM))
    database.execute(lambda connection: _insert_book(connection, "book-2", "file-2", STORAGE_KIND_SEALED))

    seal = repository.enqueue_job("book-1", "file-1", VaultJobDirection.SEAL)
    unseal = repository.enqueue_job("book-2", "file-2", VaultJobDirection.UNSEAL)

    assert seal.phase is VaultJobPhase.QUEUED and seal.attempts == 0
    assert repository.get_job_for_book("book-1") == seal
    with pytest.raises(sqlite3.IntegrityError):
        repository.enqueue_job("book-1", "file-1", VaultJobDirection.SEAL)

    assert repository.update_job(
        seal.job_id,
        phase=VaultJobPhase.WRITTEN,
        partial_path=None,
        target_path="Books/fi/file-1.jrl",
        previous_path="Books/aa/aaaa.cbz",
        expected_hash="cafe",
        count_attempt=True,
    )
    written = repository.get_job(seal.job_id)
    assert (written.phase, written.target_path, written.previous_path, written.expected_hash, written.attempts) == (
        VaultJobPhase.WRITTEN, "Books/fi/file-1.jrl", "Books/aa/aaaa.cbz", "cafe", 1,
    )
    assert [job.job_id for job in repository.list_jobs(phases=[VaultJobPhase.QUEUED])] == [unseal.job_id]
    assert [job.job_id for job in repository.list_jobs(direction=VaultJobDirection.SEAL)] == [seal.job_id]
    assert repository.list_jobs(phases=[]) == []

    database.execute(lambda connection: connection.execute("DELETE FROM books WHERE book_id = 'book-1'"))

    assert repository.get_job(seal.job_id) is None
    assert repository.update_job(seal.job_id, phase=VaultJobPhase.COMMITTED) is False
    repository.delete_job(unseal.job_id)
    assert repository.list_jobs() == []
