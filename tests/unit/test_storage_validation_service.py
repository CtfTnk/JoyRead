from __future__ import annotations

import os
from pathlib import Path

import pytest

from joyread.core.services.storage_validation_service import (
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
