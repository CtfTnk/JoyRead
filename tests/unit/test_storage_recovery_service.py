from __future__ import annotations

from pathlib import Path

from joyread.core.services.storage_migration_service import StorageMigrationService
from joyread.core.services.storage_recovery_service import StorageRecoveryService
from joyread.core.services.storage_validation_service import StorageValidationService
from joyread.infrastructure.config.settings_store import AppSettings, SettingsStore
from joyread.infrastructure.database import LATEST_SCHEMA_VERSION, apply_migrations
from joyread.infrastructure.database.sqlite_connection import open_sqlite_connection


def _store(tmp_path: Path, default_root: Path) -> SettingsStore:
    return SettingsStore(support_root=tmp_path / "support", default_storage_root=default_root)


def _recovery(store: SettingsStore) -> StorageRecoveryService:
    validation = StorageValidationService()
    migration = StorageMigrationService(store, validation)
    return StorageRecoveryService(store, validation, migration)


def _make_library(root: Path) -> Path:
    database = root / "Database" / "joyread.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = open_sqlite_connection(database)
    apply_migrations(connection)
    connection.close()
    return database


# -- first run --------------------------------------------------------------


def test_first_run_with_no_library_creates_settings_pointing_at_default(tmp_path: Path) -> None:
    default_root = tmp_path / "default"
    store = _store(tmp_path, default_root)
    assert not store.settings_path.exists()

    result = _recovery(store).prepare()

    assert result.notice is None
    assert result.settings.storage_location == str(default_root)
    assert result.settings.last_good_storage_location == str(default_root)
    assert store.settings_path.exists()


def test_first_run_reuses_compatible_default_library(tmp_path: Path) -> None:
    default_root = tmp_path / "default"
    _make_library(default_root)
    store = _store(tmp_path, default_root)

    result = _recovery(store).prepare()

    assert result.notice is None
    assert result.settings.storage_location == str(default_root)


def test_first_run_overwrites_incompatible_default_library(tmp_path: Path) -> None:
    default_root = tmp_path / "default"
    database = _make_library(default_root)
    # Mark the library as written by a newer build so it is incompatible.
    connection = open_sqlite_connection(database)
    connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (LATEST_SCHEMA_VERSION + 1,))
    connection.close()
    # Leave a marker book file to prove the root was wiped.
    marker = default_root / "Books" / "marker.cbz"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"x")
    store = _store(tmp_path, default_root)

    result = _recovery(store).prepare()

    assert result.notice is not None
    assert "reset" in result.notice.lower()
    assert not marker.exists()  # incompatible library was wiped


# -- daily startup ----------------------------------------------------------


def test_daily_startup_records_last_good_when_healthy(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    _make_library(root)
    store = _store(tmp_path, root)
    store.save(AppSettings(storage_location=str(root)))

    result = _recovery(store).prepare()

    assert result.notice is None
    assert result.settings.storage_location == str(root)
    assert result.settings.last_good_storage_location == str(root)


def test_daily_startup_falls_back_to_last_good(tmp_path: Path) -> None:
    good = tmp_path / "good"
    _make_library(good)
    missing = tmp_path / "gone"  # never created
    store = _store(tmp_path, tmp_path / "default")
    store.save(
        AppSettings(
            storage_location=str(missing),
            last_good_storage_location=str(good),
        )
    )

    result = _recovery(store).prepare()

    assert result.notice is not None
    assert result.settings.storage_location == str(good)
    assert result.settings.last_good_storage_location == str(good)
    # Persisted, so the next launch starts clean on the good root.
    assert store.load().storage_location == str(good)


def test_daily_startup_falls_back_to_default_when_nothing_good(tmp_path: Path) -> None:
    default_root = tmp_path / "default"
    _make_library(default_root)
    missing = tmp_path / "gone"
    store = _store(tmp_path, default_root)
    store.save(AppSettings(storage_location=str(missing)))

    result = _recovery(store).prepare()

    assert result.notice is not None
    assert result.settings.storage_location == str(default_root)


def test_daily_startup_uses_empty_default_when_all_unavailable(tmp_path: Path) -> None:
    default_root = tmp_path / "default"  # does not exist yet
    missing = tmp_path / "gone"
    store = _store(tmp_path, default_root)
    store.save(AppSettings(storage_location=str(missing)))

    result = _recovery(store).prepare()

    assert result.notice is not None
    assert result.settings.storage_location == str(default_root)
