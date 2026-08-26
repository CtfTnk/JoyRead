from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from joyread.core.services.storage_validation_service import (
    REQUIRED_SUBDIRECTORIES,
    StorageValidationCode,
    StorageValidationService,
)
from joyread.infrastructure.database import LATEST_SCHEMA_VERSION, apply_migrations
from joyread.infrastructure.database.sqlite_connection import open_sqlite_connection


def _make_library(root: Path) -> Path:
    """Create a fully migrated JoyRead database under ``root`` and return it."""

    database = root / "Database" / "joyread.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = open_sqlite_connection(database)
    apply_migrations(connection)
    connection.close()
    return database


def test_validate_full_accepts_valid_library(tmp_path: Path) -> None:
    _make_library(tmp_path)

    result = StorageValidationService().validate_full(tmp_path)

    assert result.ok
    assert result.code == StorageValidationCode.OK


def test_validate_full_migrates_empty_database(tmp_path: Path) -> None:
    # A freshly created (empty) database file is the "older/migratable" case:
    # full validation should apply migrations and accept it.
    database = tmp_path / "Database" / "joyread.sqlite3"
    database.parent.mkdir(parents=True)
    database.touch()

    result = StorageValidationService().validate_full(tmp_path)

    assert result.ok, result.message


def test_validate_full_fails_when_database_missing(tmp_path: Path) -> None:
    result = StorageValidationService().validate_full(tmp_path)

    assert not result.ok
    assert result.code == StorageValidationCode.DATABASE_MISSING


def test_validate_full_fails_on_corrupt_database(tmp_path: Path) -> None:
    database = tmp_path / "Database" / "joyread.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"this is definitely not a sqlite database" * 64)

    result = StorageValidationService().validate_full(tmp_path)

    assert not result.ok
    assert result.code in {
        StorageValidationCode.DATABASE_UNOPENABLE,
        StorageValidationCode.INTEGRITY_FAILED,
    }


def test_validate_full_fails_on_schema_too_new(tmp_path: Path) -> None:
    _make_library(tmp_path)
    database = tmp_path / "Database" / "joyread.sqlite3"
    connection = open_sqlite_connection(database)
    connection.execute(
        "INSERT INTO schema_migrations(version) VALUES (?)",
        (LATEST_SCHEMA_VERSION + 1,),
    )
    connection.close()

    result = StorageValidationService().validate_full(tmp_path)

    assert not result.ok
    assert result.code == StorageValidationCode.SCHEMA_TOO_NEW


def test_validate_full_fails_on_missing_key_table(tmp_path: Path) -> None:
    # Drift: a required table was dropped but its migration version is still
    # recorded, so apply_migrations skips it and the gap must be detected.
    _make_library(tmp_path)
    database = tmp_path / "Database" / "joyread.sqlite3"
    connection = open_sqlite_connection(database)
    connection.execute("DROP TABLE reader_settings")
    connection.close()

    result = StorageValidationService().validate_full(tmp_path)

    assert not result.ok
    assert result.code == StorageValidationCode.SCHEMA_INCOMPLETE


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses filesystem write permissions",
)
def test_validate_full_fails_on_unwritable_root(tmp_path: Path) -> None:
    root = tmp_path / "readonly"
    root.mkdir()
    os.chmod(root, 0o500)
    try:
        result = StorageValidationService().validate_full(root)
    finally:
        os.chmod(root, 0o700)

    assert not result.ok
    assert result.code == StorageValidationCode.NOT_WRITABLE


def test_validate_lightweight_accepts_valid_library(tmp_path: Path) -> None:
    _make_library(tmp_path)

    result = StorageValidationService().validate_lightweight(tmp_path)

    assert result.ok


def test_validate_lightweight_fails_when_schema_missing(tmp_path: Path) -> None:
    # Empty database file with no schema_migrations table.
    database = tmp_path / "Database" / "joyread.sqlite3"
    database.parent.mkdir(parents=True)
    database.touch()

    result = StorageValidationService().validate_lightweight(tmp_path)

    assert not result.ok
    assert result.code == StorageValidationCode.SCHEMA_MISSING


def test_validate_lightweight_fails_on_schema_too_new(tmp_path: Path) -> None:
    _make_library(tmp_path)
    database = tmp_path / "Database" / "joyread.sqlite3"
    connection = open_sqlite_connection(database)
    connection.execute(
        "INSERT INTO schema_migrations(version) VALUES (?)",
        (LATEST_SCHEMA_VERSION + 1,),
    )
    connection.close()

    result = StorageValidationService().validate_lightweight(tmp_path)

    assert not result.ok
    assert result.code == StorageValidationCode.SCHEMA_TOO_NEW


def test_validate_lightweight_fails_when_database_missing(tmp_path: Path) -> None:
    result = StorageValidationService().validate_lightweight(tmp_path)

    assert not result.ok
    assert result.code == StorageValidationCode.DATABASE_MISSING


def test_a_rejected_root_is_left_exactly_as_it_was_found(tmp_path: Path) -> None:
    """Pointing Select at an ordinary folder used to litter it with the managed
    layout on the way to reporting that it is not a JoyRead library."""

    folder = tmp_path / "not-a-library"
    folder.mkdir()

    result = StorageValidationService().validate_full(folder)

    assert result.ok is False
    assert result.code is StorageValidationCode.DATABASE_MISSING
    assert list(folder.iterdir()) == [], "a rejected folder must not be modified"


def test_an_accepted_library_gains_any_missing_managed_directory(tmp_path: Path) -> None:
    """A valid library from an older layout is still completed in place."""

    _make_library(tmp_path)
    assert not (tmp_path / "Thumbnails").exists()

    result = StorageValidationService().validate_full(tmp_path)

    assert result.ok is True
    for name in REQUIRED_SUBDIRECTORIES:
        assert (tmp_path / name).is_dir(), f"{name} must exist after acceptance"


def test_the_write_probe_never_survives_validation(tmp_path: Path) -> None:
    folder = tmp_path / "not-a-library"
    folder.mkdir()

    StorageValidationService().validate_full(folder)

    assert not any(p.name.startswith(".joyread-write-test") for p in folder.iterdir())


def _seed_book_row(database: Path) -> None:
    """One healthy book whose managed path is stored relative, as imports write it."""

    connection = open_sqlite_connection(database)
    connection.execute(
        """
        INSERT INTO book_files
          (file_id, original_path, original_file_name, storage_path, file_format,
           hash_algorithm, source_hash, stored_hash, storage_kind, state, created_at, updated_at)
        VALUES ('f1','/orig/x.cbz','x.cbz','Books/ab/x.cbz','cbz','sha256','h','h','verbatim','healthy',
                '2026-01-01','2026-01-01')
        """
    )
    connection.execute(
        """
        INSERT INTO books (book_id, file_id, title, author, book_type, created_at, updated_at)
        VALUES ('b1','f1','T','A','manga','2026-01-01','2026-01-01')
        """
    )
    connection.commit()
    connection.close()


def test_the_smoke_query_resolves_paths_instead_of_warning_about_them(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Managed paths are stored relative to the storage root. Running the smoke
    query without a resolver made the repository hand them back raw and warn,
    once per row, that the caller would resolve them against the process CWD --
    an unsafe fallback that validation has no reason to reach, since it knows
    the root.
    """

    database = _make_library(tmp_path)
    _seed_book_row(database)

    with caplog.at_level(logging.WARNING):
        result = StorageValidationService().validate_full(tmp_path)

    assert result.ok is True
    assert not [record for record in caplog.records if "storage resolver" in record.getMessage()]


def test_a_rejected_layout_conflict_leaves_the_database_untouched(tmp_path: Path) -> None:
    """Opening the database applies migrations, so a check that runs after it
    has already rewritten the library the user was only asking about.

    Reproduced with an empty-but-migratable database and a regular file named
    `Thumbnails`: validation rejected the root, but the 0-byte database had
    been upgraded through every migration first.
    """

    database = tmp_path / "Database" / "joyread.sqlite3"
    database.parent.mkdir(parents=True)
    database.touch()
    (tmp_path / "Thumbnails").write_text("a regular file, not a directory")

    result = StorageValidationService().validate_full(tmp_path)

    assert result.ok is False
    assert result.code is StorageValidationCode.NOT_WRITABLE
    assert database.stat().st_size == 0, "a rejected candidate must not be migrated"


def test_a_layout_conflict_is_reported_rather_than_crashing(tmp_path: Path) -> None:
    _make_library(tmp_path)
    (tmp_path / "Books").write_text("occupied by a file")

    result = StorageValidationService().validate_full(tmp_path)

    assert result.ok is False
    assert "Books" in result.message
