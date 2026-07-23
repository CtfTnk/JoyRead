from __future__ import annotations

from io import BytesIO
import logging
from pathlib import Path
import sqlite3
from zipfile import ZIP_DEFLATED, ZipFile

import pyzipper
import pytest
from PIL import Image
from PySide6.QtGui import QPainter, QPdfWriter

from joyread.app.app_context import create_app_context
from joyread.core.archive import ArchiveImageService
from joyread.core.repositories.sqlite_book_repository import SqliteBookRepository
from joyread.core.reader import ReaderDirection, ReaderFitMode, ReaderSettings, ReaderTransitionMode
from joyread.core.services.archive_extraction_pool import HiddenImageExtractionPool
from joyread.core.services.hash_service import HashService
from joyread.core.services.import_service import ImportService
from joyread.core.services.storage_migration_service import (
    StorageMigrationError,
    StorageMigrationService,
)
from joyread.core.services.storage_validation_service import StorageValidationService
from joyread.infrastructure.config.settings_store import AppSettings, SettingsStore
from joyread.infrastructure.config.storage_names import LIBRARY_DIRECTORY_NAME
from joyread.infrastructure.database import DatabaseInterpreter, DatabasePriority, apply_migrations
from joyread.infrastructure.database.migrations import MIGRATIONS
from joyread.infrastructure.database.sqlite_connection import open_sqlite_connection
from joyread.infrastructure.filesystem.path_service import PathService


def _database(tmp_path: Path) -> DatabaseInterpreter:
    database = DatabaseInterpreter(tmp_path / "joyread.sqlite3")
    database.execute(apply_migrations, DatabasePriority.CRITICAL)
    return database


def _png_bytes(path: Path, color: str = "#336699") -> None:
    Image.new("RGB", (10, 20), color).save(path, format="PNG")


def _png_payload(color: str = "#336699") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (10, 20), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _write_cbz(path: Path, color: str = "#336699") -> None:
    image = path.with_suffix(".png")
    _png_bytes(image, color)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")


def _write_encrypted_cbz(path: Path, password: str = "secret") -> None:
    with pyzipper.AESZipFile(
        path,
        "w",
        compression=ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as archive:
        archive.setpassword(password.encode("utf-8"))
        archive.writestr("001.png", _png_payload())


def _write_cbz_with_nested_encrypted_archive(path: Path) -> None:
    nested = BytesIO()
    with pyzipper.AESZipFile(
        nested,
        "w",
        compression=ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as archive:
        archive.setpassword(b"secret")
        archive.writestr("001.png", _png_payload("#cc4422"))
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("001.png", _png_payload())
        archive.writestr("nested.cbz", nested.getvalue())


def _write_pdf(path: Path) -> None:
    writer = QPdfWriter(str(path))
    painter = QPainter(writer)
    painter.drawText(40, 80, "JoyRead PDF")
    painter.end()


def _import_service(tmp_path: Path) -> tuple[ImportService, DatabaseInterpreter, PathService]:
    paths = PathService(storage_root=tmp_path / "storage", support_root=tmp_path / "support")
    paths.ensure_directories()
    database = _database(paths.paths.database)
    service = ImportService(paths, database, ArchiveImageService(), HashService())
    return service, database, paths


class _RecordingHashService(HashService):
    def __init__(self) -> None:
        self.compute_paths: list[Path] = []
        self.copy_paths: list[tuple[Path, Path]] = []

    def compute(self, path: Path, algorithm: str = "sha256") -> str:
        self.compute_paths.append(Path(path))
        return super().compute(path, algorithm)

    def copy_with_hash(self, source: Path, destination: Path, algorithm: str = "sha256") -> str:
        self.copy_paths.append((Path(source), Path(destination)))
        return super().copy_with_hash(source, destination, algorithm)


def test_migrations_create_expected_tables_and_are_idempotent(tmp_path: Path) -> None:
    database = _database(tmp_path)

    database.execute(apply_migrations, DatabasePriority.CRITICAL)
    tables = set(
        database.execute(
            lambda connection: [
                row["name"]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            ]
        )
    )

    assert {
        "schema_migrations",
        "book_files",
        "books",
        "progress",
        "bookmarks",
        "collections",
        "collection_books",
        "private_collections",
        "private_books",
        "recent_books",
        "reader_settings",
        "languages",
        "import_batches",
        "import_items",
    } <= tables
    language_rows = database.execute(
        lambda connection: [
            (row["iso_code"], row["plain_text"])
            for row in connection.execute(
                "SELECT iso_code, plain_text FROM languages ORDER BY sort_order"
            ).fetchall()
        ]
    )
    assert language_rows == [
        ("en", "English"),
        ("zh", "Chinese"),
        ("ja", "Japanese"),
        ("und", "Unknown"),
    ]
    book_file_columns = database.execute(
        lambda connection: {
            row["name"]
            for row in connection.execute("PRAGMA table_info(book_files)").fetchall()
        }
    )
    reader_settings_columns = database.execute(
        lambda connection: {
            row["name"]
            for row in connection.execute("PRAGMA table_info(reader_settings)").fetchall()
        }
    )
    assert "original_file_name" in book_file_columns
    assert "vertical_zoom_percent" in reader_settings_columns
    assert "vertical_fit_width" in reader_settings_columns
    database.close()


def test_language_migration_normalizes_legacy_language_values(tmp_path: Path) -> None:
    database = DatabaseInterpreter(tmp_path / "legacy-language.sqlite3")

    def seed_legacy(connection) -> None:  # noqa: ANN001 - sqlite connection type is evident from caller.
        connection.executescript(MIGRATIONS[0][1])
        connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        connection.executescript(MIGRATIONS[1][1])
        connection.execute("INSERT INTO schema_migrations(version) VALUES (2)")
        connection.execute(
            """
            INSERT INTO book_files(
                file_id, original_path, storage_path, file_format, file_size,
                mtime_ns, hash_algorithm, content_hash, state, created_at, updated_at
            )
            VALUES (
                'file-1', '/source/book.cbz', '/managed/book.cbz', 'CBZ', 1,
                1, 'sha256', 'legacy-hash', 'healthy',
                '2026-01-01T00:00:00', '2026-01-01T00:00:00'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO books(
                book_id, file_id, title, author, language_tag, book_type,
                cover_path, is_favourite, created_at, updated_at
            )
            VALUES (
                'book-1', 'file-1', 'Legacy', 'Unknown', 'English', 'manga',
                NULL, 0, '2026-01-01T00:00:00', '2026-01-01T00:00:00'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO private_books(
                private_book_id, file_id, title, author, language_tag, book_type,
                cover_path, encrypted_cover_path, encryption_status,
                private_collection_id, created_at, updated_at
            )
            VALUES (
                'private-1', 'file-1', 'Private Legacy', 'Unknown', 'Other', 'manga',
                NULL, NULL, 'not_encrypted', NULL,
                '2026-01-01T00:00:00', '2026-01-01T00:00:00'
            )
            """
        )

    database.execute(seed_legacy, DatabasePriority.CRITICAL)
    database.execute(apply_migrations, DatabasePriority.CRITICAL)

    rows = database.execute(
        lambda connection: {
            "public": connection.execute(
                "SELECT language_tag FROM books WHERE book_id = 'book-1'"
            ).fetchone()["language_tag"],
            "private": connection.execute(
                "SELECT language_tag FROM private_books WHERE private_book_id = 'private-1'"
            ).fetchone()["language_tag"],
            "original_file_name": connection.execute(
                "SELECT original_file_name FROM book_files WHERE file_id = 'file-1'"
            ).fetchone()["original_file_name"],
        }
    )

    assert rows == {"public": "en", "private": "und", "original_file_name": "book.cbz"}
    database.close()


def test_database_interpreter_respects_priority_before_start(tmp_path: Path) -> None:
    database = DatabaseInterpreter(tmp_path / "priority.sqlite3", autostart=False)
    seen: list[str] = []

    low = database.submit(lambda _connection: seen.append("low"), DatabasePriority.BACKGROUND)
    high = database.submit(lambda _connection: seen.append("high"), DatabasePriority.CRITICAL)
    database.start()
    low.result(timeout=2)
    high.result(timeout=2)

    assert seen == ["high", "low"]
    database.close()


def test_database_interpreter_debug_logs_callback_name(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = DatabaseInterpreter(tmp_path / "callback-trace.sqlite3", autostart=False)

    def trace_callback(connection) -> int:  # noqa: ANN001 - sqlite connection type is evident from caller.
        return connection.execute("SELECT 1").fetchone()[0]

    try:
        with caplog.at_level(logging.DEBUG, logger="joyread.infrastructure.database.database_interpreter"):
            future = database.submit(trace_callback, DatabasePriority.NORMAL)
            database.start()
            assert future.result(timeout=2) == 1
    finally:
        database.close()

    messages = [record.getMessage() for record in caplog.records]
    assert any("DB request queued" in message and "trace_callback" in message for message in messages)
    assert any("DB request starting" in message and "trace_callback" in message for message in messages)
    assert any("DB request completed" in message and "trace_callback" in message for message in messages)


def test_settings_config_is_outside_storage_root(tmp_path: Path) -> None:
    store = SettingsStore(
        support_root=tmp_path / "support",
        default_storage_root=tmp_path / "storage",
    )
    settings = store.load()
    paths = PathService(storage_root=Path(settings.storage_location), support_root=store.support_root)

    assert store.settings_path.is_relative_to(tmp_path / "support")
    assert not store.settings_path.is_relative_to(paths.paths.books.parent)
    assert paths.paths.config.is_relative_to(tmp_path / "support")
    assert paths.paths.cache == tmp_path / "storage" / "Cache"
    assert paths.paths.thumbnails == tmp_path / "storage" / "Thumbnails"


def test_manifest_import_copies_book_and_repository_lists_it(tmp_path: Path) -> None:
    source = tmp_path / "source" / "Example Manga.cbz"
    source.parent.mkdir()
    _write_cbz(source)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        '{"version": 1, "items": [{"source_path": "source/Example Manga.cbz"}]}',
        encoding="utf-8",
    )
    service, database, paths = _import_service(tmp_path)

    result = service.import_manifest(manifest)
    books = SqliteBookRepository(database).list_books()
    stored_page_count_columns = database.execute(
        lambda connection: [
            row["name"]
            for row in connection.execute("PRAGMA table_info(books)").fetchall()
            if row["name"] == "page_count"
        ]
    )
    original_file_name = database.execute(
        lambda connection: connection.execute(
            "SELECT original_file_name FROM book_files"
        ).fetchone()["original_file_name"]
    )

    assert result.imported_count == 1
    assert result.failed_count == 0
    assert books[0].title == "Example Manga"
    assert books[0].author == "Unknown"
    assert books[0].language_tag == "und"
    assert books[0].language_name == "Unknown"
    assert books[0].original_file_name == "Example Manga.cbz"
    assert books[0].book_type == "manga"
    assert Path(books[0].file_path).is_relative_to(paths.paths.books)
    assert Path(books[0].file_path).exists()
    assert original_file_name == "Example Manga.cbz"
    assert stored_page_count_columns == []
    database.close()


def test_duplicate_manifest_import_reuses_existing_book(tmp_path: Path) -> None:
    source = tmp_path / "duplicate.cbz"
    _write_cbz(source)
    service, database, _paths = _import_service(tmp_path)

    first = service.import_files([source])
    second = service.import_files([source])
    books = SqliteBookRepository(database).list_books()

    assert first.imported_count == 1
    assert second.duplicate_count == 1
    assert len(books) == 1
    database.close()


@pytest.mark.parametrize("verify_integrity", [True, False])
def test_import_hashes_staging_copy_in_both_integrity_modes(
    tmp_path: Path,
    verify_integrity: bool,
) -> None:
    source = tmp_path / "hash-mode.cbz"
    _write_cbz(source)
    paths = PathService(storage_root=tmp_path / "storage", support_root=tmp_path / "support")
    paths.ensure_directories()
    database = _database(paths.paths.database)
    hash_service = _RecordingHashService()
    service = ImportService(
        paths,
        database,
        ArchiveImageService(),
        hash_service,
        verify_imported_file_integrity=verify_integrity,
    )

    result = service.import_files([source])
    book = SqliteBookRepository(database).list_books()[0]

    assert result.imported_count == 1
    assert book.file_id == result.items[0].file_id
    assert hash_service.copy_paths and hash_service.copy_paths[0][0] == source
    assert hash_service.copy_paths[0][1].parent == paths.paths.books / ".staging"
    assert hash_service.compute_paths == ([source] if verify_integrity else [])
    assert list((paths.paths.books / ".staging").iterdir()) == []
    database.close()


def test_sqlite_repository_persists_book_cover_path(tmp_path: Path) -> None:
    source = tmp_path / "cover-path.cbz"
    _write_cbz(source)
    service, database, paths = _import_service(tmp_path)
    service.import_files([source])
    repository = SqliteBookRepository(database)
    book = repository.list_books()[0]
    cover_path = paths.paths.thumbnails / "covers" / "custom-cover.png"

    repository.set_book_cover_path(book.uuid, str(cover_path))

    refreshed = repository.list_books()[0]
    assert refreshed.cover_thumbnail_path == str(cover_path)
    database.close()


def test_import_persists_relative_storage_path(tmp_path: Path) -> None:
    source = tmp_path / "relative-storage.cbz"
    _write_cbz(source)
    service, database, paths = _import_service(tmp_path)
    service.import_files([source])

    stored = database.execute(
        lambda connection: connection.execute("SELECT storage_path FROM book_files").fetchone()[
            "storage_path"
        ]
    )
    book = SqliteBookRepository(database).list_books()[0]

    # Persisted relative to the storage root, surfaced absolute to callers.
    assert not Path(stored).is_absolute()
    assert stored.startswith("Books/")
    assert Path(book.file_path).is_absolute()
    assert Path(book.file_path).exists()
    assert paths.resolver.to_storage_relative(book.file_path) == stored
    database.close()


def test_set_book_cover_path_persists_relative(tmp_path: Path) -> None:
    source = tmp_path / "cover-relative.cbz"
    _write_cbz(source)
    service, database, paths = _import_service(tmp_path)
    service.import_files([source])
    repository = SqliteBookRepository(database)
    book = repository.list_books()[0]
    cover_path = paths.paths.thumbnails / "covers" / "custom-cover.png"

    repository.set_book_cover_path(book.uuid, str(cover_path))

    stored = database.execute(
        lambda connection: connection.execute("SELECT cover_path FROM books").fetchone()["cover_path"]
    )
    refreshed = repository.list_books()[0]
    assert stored == "Thumbnails/covers/custom-cover.png"
    assert Path(refreshed.cover_thumbnail_path) == cover_path
    database.close()


def test_migration_normalizes_absolute_managed_paths(tmp_path: Path) -> None:
    paths = PathService(storage_root=tmp_path / "storage", support_root=tmp_path / "support")
    paths.ensure_directories()
    database = _database(paths.paths.database)

    root = paths.storage_root
    inside = root / "Books" / "ab" / "inside.cbz"
    outside = tmp_path / "elsewhere" / "outside.cbz"
    cover_inside = root / "Thumbnails" / "covers" / "c.png"

    def seed(connection: sqlite3.Connection) -> None:
        for file_id, content_hash, storage_path in (
            ("f1", "h1", str(inside)),
            ("f2", "h2", str(outside)),
        ):
            connection.execute(
                """
                INSERT INTO book_files(
                    file_id, original_path, storage_path, file_format, file_size,
                    mtime_ns, hash_algorithm, content_hash, state, created_at, updated_at
                ) VALUES(?, 'orig', ?, 'CBZ', 1, 1, 'sha256', ?, 'healthy', 't', 't')
                """,
                (file_id, storage_path, content_hash),
            )
        connection.execute(
            """
            INSERT INTO books(book_id, file_id, title, author, book_type, cover_path, created_at, updated_at)
            VALUES('b1', 'f1', 'T', 'A', 'manga', ?, 't', 't')
            """,
            (str(cover_inside),),
        )
        # Simulate a pre-normalization database so the migration re-runs.
        connection.execute("DELETE FROM schema_migrations WHERE version = 11")

    database.execute(seed)
    database.execute(apply_migrations, DatabasePriority.CRITICAL)

    stored = database.execute(
        lambda connection: {
            row["file_id"]: row["storage_path"]
            for row in connection.execute("SELECT file_id, storage_path FROM book_files").fetchall()
        }
    )
    cover = database.execute(
        lambda connection: connection.execute(
            "SELECT cover_path FROM books WHERE book_id = 'b1'"
        ).fetchone()["cover_path"]
    )

    assert stored["f1"] == "Books/ab/inside.cbz"
    assert stored["f2"] == str(outside)  # outside the storage root: left as-is (surfaces as missing)
    assert cover == "Thumbnails/covers/c.png"
    database.close()


def test_import_files_accepts_readable_pdf(tmp_path: Path, qtbot) -> None:  # noqa: ARG001
    source = tmp_path / "Readable PDF.pdf"
    _write_pdf(source)
    service, database, _paths = _import_service(tmp_path)

    result = service.import_files([source])
    books = SqliteBookRepository(database).list_books()

    assert result.imported_count == 1
    assert result.failed_count == 0
    assert books[0].file_format == "PDF"
    assert books[0].book_type == "manga"
    database.close()


def test_list_books_preserves_persisted_state_without_refresh(tmp_path: Path) -> None:
    # State refresh runs only on the user-action ``get_book`` path. The
    # shelf load (``list_books``) returns whatever was last persisted —
    # the file disappearing between load and click does not flip the
    # row until a user action fetches it. This test pins that contract
    # so a future regression cannot reintroduce the per-shelf-load
    # ``Path.exists()`` stat.
    source = tmp_path / "source" / "Persisted File.cbz"
    source.parent.mkdir()
    _write_cbz(source)
    service, database, _paths = _import_service(tmp_path)
    service.import_files([source])
    repository = SqliteBookRepository(database)
    book = repository.list_books()[0]
    Path(book.file_path).unlink()

    refreshed = repository.list_books()[0]
    state = database.execute(
        lambda connection: connection.execute("SELECT state FROM book_files").fetchone()["state"]
    )

    assert refreshed.is_missing is False
    assert state == "healthy"
    database.close()


def test_get_book_marks_missing_when_file_deleted(tmp_path: Path) -> None:
    source = tmp_path / "source" / "Missing Get.cbz"
    source.parent.mkdir()
    _write_cbz(source)
    service, database, _paths = _import_service(tmp_path)
    service.import_files([source])
    repository = SqliteBookRepository(database)
    book = repository.list_books()[0]
    Path(book.file_path).unlink()

    refreshed = repository.get_book(book.uuid)
    state = database.execute(
        lambda connection: connection.execute("SELECT state FROM book_files").fetchone()["state"]
    )

    assert refreshed is not None
    assert refreshed.is_missing is True
    assert state == "missing"
    database.close()


def test_get_export_records_marks_missing_when_file_deleted(tmp_path: Path) -> None:
    # Export is a manual user action; if a targeted file has been moved
    # or deleted, the row should flip to ``missing`` so the shelf and
    # the export failure surface agree without waiting for the user to
    # click the book separately.
    source = tmp_path / "source" / "Missing Export.cbz"
    source.parent.mkdir()
    _write_cbz(source)
    service, database, _paths = _import_service(tmp_path)
    service.import_files([source])
    repository = SqliteBookRepository(database)
    book = repository.list_books()[0]
    Path(book.file_path).unlink()

    records = repository.get_export_records((book.uuid,))
    state = database.execute(
        lambda connection: connection.execute("SELECT state FROM book_files").fetchone()["state"]
    )

    assert len(records) == 1
    assert records[0].is_missing is True
    assert state == "missing"
    database.close()


def test_import_folder_respects_filesystem_depth(tmp_path: Path) -> None:
    root_file = tmp_path / "folder" / "root.cbz"
    child_file = tmp_path / "folder" / "child" / "nested.cbz"
    child_file.parent.mkdir(parents=True)
    _write_cbz(root_file)
    _write_cbz(child_file, color="#cc4422")
    service, database, _paths = _import_service(tmp_path)

    depth_one = service.import_folder(root_file.parent, max_depth=1)
    books = SqliteBookRepository(database).list_books()

    assert depth_one.imported_count == 1
    assert [book.title for book in books] == ["root"]

    depth_two = service.import_folder(root_file.parent, max_depth=2)
    books = sorted(SqliteBookRepository(database).list_books(), key=lambda book: book.title)

    assert depth_two.imported_count == 1
    assert depth_two.duplicate_count == 1
    assert [book.title for book in books] == ["nested", "root"]
    database.close()


def test_import_skips_encrypted_archives_without_adding_books(tmp_path: Path) -> None:
    source = tmp_path / "encrypted.cbz"
    _write_encrypted_cbz(source)
    service, database, _paths = _import_service(tmp_path)

    result = service.import_files([source])
    item_rows = database.execute(
        lambda connection: connection.execute("SELECT status, message FROM import_items").fetchall()
    )

    assert result.imported_count == 0
    assert result.skipped_count == 1
    assert result.failed_count == 0
    assert result.items[0].status == "skipped"
    assert "Skipped encrypted archive" in (result.items[0].message or "")
    assert SqliteBookRepository(database).list_books() == []
    assert [(row["status"], "Skipped encrypted archive" in row["message"]) for row in item_rows] == [
        ("skipped", True)
    ]
    database.close()


def test_import_probes_only_top_level_and_allows_nested_encrypted_archives(tmp_path: Path) -> None:
    source = tmp_path / "outer.cbz"
    _write_cbz_with_nested_encrypted_archive(source)
    service, database, _paths = _import_service(tmp_path)

    result = service.import_files([source])

    assert result.imported_count == 1
    assert result.skipped_count == 0
    assert result.failed_count == 0
    assert result.items[0].status == "imported"
    assert [book.title for book in SqliteBookRepository(database).list_books()] == ["outer"]
    database.close()


def test_recent_books_are_public_only_and_capped_at_ten(tmp_path: Path) -> None:
    service, database, _paths = _import_service(tmp_path)
    sources: list[Path] = []
    for index in range(11):
        source = tmp_path / f"recent-{index:02d}.cbz"
        _write_cbz(source, color=f"#{index:02x}3366")
        sources.append(source)
    service.import_files(sources)
    repository = SqliteBookRepository(database)
    books = sorted(repository.list_books(), key=lambda book: book.title)

    for book in books:
        repository.set_progress(book.uuid, page_index=1, progress_percent=10.0)

    recent_rows = database.execute(
        lambda connection: connection.execute(
            "SELECT book_id FROM recent_books ORDER BY last_read_at DESC"
        ).fetchall()
    )
    recent_books = [book for book in repository.list_books() if book.last_read_at is not None]

    assert len(recent_rows) == 10
    assert len(recent_books) == 10

    target = recent_books[0]
    repository.remove_book_from_recent(target.uuid)
    updated_target = next(book for book in repository.list_books() if book.uuid == target.uuid)
    assert updated_target.last_read_at is None
    assert updated_target.progress == target.progress
    assert repository.get_progress(target.uuid).progress_percent == 10.0
    assert target.uuid not in {
        row["book_id"]
        for row in database.execute(
            lambda connection: connection.execute("SELECT book_id FROM recent_books").fetchall()
        )
    }

    database.execute(
        lambda connection: connection.execute(
            """
            INSERT INTO progress(book_scope, book_id, page_index, progress_percent, updated_at)
            VALUES ('private', 'private-book', 1, 10.0, '2026-01-01T00:00:00')
            """
        )
    )
    private_recent_count = database.execute(
        lambda connection: connection.execute(
            "SELECT COUNT(*) AS count FROM recent_books WHERE book_id = 'private-book'"
        ).fetchone()["count"]
    )
    assert private_recent_count == 0
    database.close()


def test_reader_settings_persist_per_public_book(tmp_path: Path) -> None:
    source = tmp_path / "reader-settings.cbz"
    _write_cbz(source)
    service, database, _paths = _import_service(tmp_path)
    service.import_files([source])
    repository = SqliteBookRepository(database)
    book = repository.list_books()[0]
    settings = ReaderSettings(
        direction=ReaderDirection.LEFT_TO_RIGHT,
        vertical_custom_enabled=True,
        vertical_fit_width=True,
        page_spacing=24,
        vertical_zoom_percent=135,
        custom_enabled=True,
        always_one_page=True,
        fit_mode=ReaderFitMode.FIT_HEIGHT,
        transition_mode=ReaderTransitionMode.SLIDE,
        spread_offset=1,
    )

    repository.save_reader_settings(book.uuid, settings)
    loaded = repository.get_reader_settings(book.uuid)

    assert loaded == settings
    database.close()


def test_reader_progress_round_trips_without_page_count(tmp_path: Path) -> None:
    source = tmp_path / "reader-progress.cbz"
    _write_cbz(source)
    service, database, _paths = _import_service(tmp_path)
    service.import_files([source])
    repository = SqliteBookRepository(database)
    book = repository.list_books()[0]

    repository.set_progress(book.uuid, page_index=12, progress_percent=42.5)
    progress = repository.get_progress(book.uuid)

    assert progress is not None
    assert progress.page_index == 12
    assert progress.progress_percent == 42.5
    database.close()


def test_import_failures_are_recorded_without_books(tmp_path: Path) -> None:
    unsupported = tmp_path / "sample.txt"
    unsupported.write_text("not a book", encoding="utf-8")
    shelved_epub = tmp_path / "shelved.epub"
    shelved_epub.write_bytes(b"epub access disabled")
    corrupt = tmp_path / "corrupt.cbz"
    corrupt.write_bytes(b"not a zip")
    service, database, _paths = _import_service(tmp_path)

    result = service.import_files([tmp_path / "missing.cbz", unsupported, shelved_epub, corrupt])
    item_rows = database.execute(lambda connection: connection.execute("SELECT status FROM import_items").fetchall())

    assert result.failed_count == 4
    assert SqliteBookRepository(database).list_books() == []
    assert [row["status"] for row in item_rows] == ["failed", "failed", "failed", "failed"]
    database.close()


def test_collection_delete_and_private_move_keep_public_books_consistent(tmp_path: Path) -> None:
    source = tmp_path / "book.cbz"
    _write_cbz(source)
    service, database, _paths = _import_service(tmp_path)
    service.import_files([source])
    repository = SqliteBookRepository(database)
    book = repository.list_books()[0]
    collection = repository.create_collection("Reading")
    repository.add_book_to_collection(book.uuid, collection.uuid)
    repository.add_book_to_collection(book.uuid, collection.uuid)
    repository.rename_collection(collection.uuid, "Reading Queue")

    assert repository.list_collections()[0].name == "Reading Queue"
    assert repository.list_books()[0].collection_ids == (collection.uuid,)

    repository.remove_book_from_collection(book.uuid, collection.uuid)

    assert repository.list_books()[0].collection_ids == ()
    assert repository.list_books()[0].uuid == book.uuid

    repository.add_book_to_collection(book.uuid, collection.uuid)

    repository.delete_collection(collection.uuid)

    assert repository.list_books()[0].collection_ids == ()

    private_id = repository.move_book_to_private(book.uuid)
    assert private_id
    assert repository.list_books() == []
    private_count = database.execute(
        lambda connection: connection.execute("SELECT COUNT(*) AS count FROM private_books").fetchone()["count"]
    )
    assert private_count == 1
    database.close()


def test_list_collections_orders_newest_first(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = SqliteBookRepository(database)

    def _insert(connection, collection_id: str, name: str, when: str) -> None:
        connection.execute(
            "INSERT INTO collections(collection_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (collection_id, name, when, when),
        )

    database.execute(lambda c: _insert(c, "old", "Old Collection", "2026-01-01T00:00:00.000000"))
    database.execute(lambda c: _insert(c, "mid", "Mid Collection", "2026-02-01T00:00:00.000000"))
    database.execute(lambda c: _insert(c, "new", "New Collection", "2026-03-01T00:00:00.000000"))

    ordered = [collection.uuid for collection in repository.list_collections()]
    assert ordered == ["new", "mid", "old"]
    database.close()


def test_internal_book_operations_and_bookmarks_are_app_only_repository_methods(tmp_path: Path) -> None:
    source = tmp_path / "ops.cbz"
    _write_cbz(source)
    service, database, _paths = _import_service(tmp_path)
    service.import_files([source])
    repository = SqliteBookRepository(database)
    book = repository.list_books()[0]

    assert [(language.iso_code, language.plain_text) for language in repository.list_languages()] == [
        ("en", "English"),
        ("zh", "Chinese"),
        ("ja", "Japanese"),
        ("und", "Unknown"),
    ]

    repository.update_book_metadata(book.uuid, title="Edited Title", author="Edited Author", language_tag="ja")
    repository.set_favourite(book.uuid, True)
    bookmark = repository.add_bookmark(book.uuid, "Good page", 7)
    repository.rename_bookmark(book.uuid, bookmark.uuid, "Renamed page")
    updated = repository.get_book(book.uuid)

    assert updated is not None
    assert updated.title == "Edited Title"
    assert updated.author == "Edited Author"
    assert updated.language_tag == "ja"
    assert updated.language_name == "Japanese"
    assert updated.is_favourite is True
    renamed = repository.list_bookmarks(book.uuid)
    assert len(renamed) == 1
    assert renamed[0].uuid == bookmark.uuid
    assert renamed[0].name == "Renamed page"
    repository.delete_bookmark(book.uuid, bookmark.uuid)
    assert repository.list_bookmarks(book.uuid) == []

    with pytest.raises(ValueError, match="Unknown language code"):
        repository.update_book_metadata(book.uuid, language_tag="bad")

    with pytest.raises(ValueError, match="Bookmark does not exist"):
        repository.rename_bookmark(book.uuid, bookmark.uuid, "Missing")

    with pytest.raises(ValueError, match="Bookmark does not exist"):
        repository.delete_bookmark(book.uuid, bookmark.uuid)

    repository.add_bookmark(book.uuid, "Good page", 7)
    repository.delete_book(book.uuid)
    assert repository.get_book(book.uuid) is None
    assert repository.list_bookmarks(book.uuid) == []
    database.close()


def test_delete_book_removes_related_rows_and_managed_files(tmp_path: Path) -> None:
    source = tmp_path / "delete-me.cbz"
    _write_cbz(source)
    service, database, paths = _import_service(tmp_path)
    service.import_files([source])
    repository = SqliteBookRepository(
        database,
        managed_books_root=paths.paths.books,
        thumbnails_root=paths.paths.thumbnails,
    )
    book = repository.list_books()[0]
    stored_file = Path(book.file_path)
    collection = repository.create_collection("Delete Test")
    repository.add_book_to_collection(book.uuid, collection.uuid)
    repository.set_progress(book.uuid, page_index=3, progress_percent=33.0)
    repository.add_bookmark(book.uuid, "A page", 3)
    covers_dir = paths.paths.thumbnails / "covers"
    covers_dir.mkdir(parents=True)
    generated_cover = covers_dir / f"{book.uuid}-signature-200x284.png"
    generated_cover.write_bytes(b"cover")

    repository.delete_book(book.uuid)

    counts = database.execute(
        lambda connection: {
            table: connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            for table in (
                "books",
                "book_files",
                "bookmarks",
                "progress",
                "recent_books",
                "collection_books",
                "import_items",
            )
        }
    )
    assert counts == {
        "books": 0,
        "book_files": 0,
        "bookmarks": 0,
        "progress": 0,
        "recent_books": 0,
        "collection_books": 0,
        "import_items": 0,
    }
    assert stored_file.exists() is False
    assert source.exists() is True
    assert generated_cover.exists() is False
    database.close()


def test_delete_book_preserves_managed_file_when_private_book_references_it(tmp_path: Path) -> None:
    source = tmp_path / "shared.cbz"
    _write_cbz(source)
    service, database, paths = _import_service(tmp_path)
    service.import_files([source])
    repository = SqliteBookRepository(database, managed_books_root=paths.paths.books)
    book = repository.list_books()[0]
    stored_file = Path(book.file_path)
    file_id = database.execute(
        lambda connection: connection.execute(
            "SELECT file_id FROM books WHERE book_id = ?",
            (book.uuid,),
        ).fetchone()["file_id"]
    )
    database.execute(
        lambda connection: connection.execute(
            """
            INSERT INTO private_books(
                private_book_id, file_id, title, author, language_tag, book_type,
                cover_path, encrypted_cover_path, encryption_status,
                private_collection_id, created_at, updated_at
            )
            VALUES ('private-copy', ?, 'Private Copy', 'Unknown', NULL, 'manga',
                NULL, NULL, 'not_encrypted', NULL, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
            """,
            (file_id,),
        )
    )

    repository.delete_book(book.uuid)

    assert stored_file.exists() is True
    assert database.execute(lambda connection: connection.execute("SELECT COUNT(*) AS count FROM book_files").fetchone()["count"]) == 1
    assert database.execute(lambda connection: connection.execute("SELECT COUNT(*) AS count FROM private_books").fetchone()["count"]) == 1
    database.close()


def test_progress_stores_page_index_and_percent_without_clamping(tmp_path: Path) -> None:
    source = tmp_path / "progress.cbz"
    _write_cbz(source)
    service, database, _paths = _import_service(tmp_path)
    service.import_files([source])
    repository = SqliteBookRepository(database)
    book = repository.list_books()[0]

    repository.set_progress(book.uuid, page_index=9999, progress_percent=125.0)
    row = database.execute(lambda connection: connection.execute("SELECT * FROM progress").fetchone())

    assert row["page_index"] == 9999
    assert row["progress_percent"] == 125.0
    database.close()


def _seed_library(root: Path) -> None:
    """Create a valid migrated JoyRead library with one book file at ``root``."""

    database = root / "Database" / "joyread.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = open_sqlite_connection(database)
    apply_migrations(connection)
    connection.close()
    book = root / "Books" / "ab" / "book.cbz"
    book.parent.mkdir(parents=True, exist_ok=True)
    book.write_bytes(b"book")


def _migration_service(store: SettingsStore) -> StorageMigrationService:
    return StorageMigrationService(store, StorageValidationService())


def test_move_to_parent_copies_library_updates_settings_and_removes_old(tmp_path: Path) -> None:
    old = tmp_path / "old"
    _seed_library(old)
    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=old)
    store.save(AppSettings(storage_location=str(old)))
    parent = tmp_path / "elsewhere"

    result = _migration_service(store).move_to_parent(old, parent)

    target = parent / LIBRARY_DIRECTORY_NAME
    assert result.target_root == target.resolve()
    assert (target / "Books" / "ab" / "book.cbz").exists()
    assert (target / "Database" / "joyread.sqlite3").exists()
    assert store.load().storage_location == str(target.resolve())
    assert not old.exists()  # old root removed after a successful move


def test_move_to_parent_fails_when_target_exists_and_keeps_settings(tmp_path: Path) -> None:
    old = tmp_path / "old"
    _seed_library(old)
    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=old)
    store.save(AppSettings(storage_location=str(old)))
    parent = tmp_path / "elsewhere"
    (parent / LIBRARY_DIRECTORY_NAME).mkdir(parents=True)

    with pytest.raises(StorageMigrationError):
        _migration_service(store).move_to_parent(old, parent)

    assert store.load().storage_location == str(old)
    assert old.exists()


def test_move_to_parent_rolls_back_when_validation_fails(tmp_path: Path) -> None:
    # A library with no database fails staging validation; nothing is adopted.
    old = tmp_path / "old"
    (old / "Books").mkdir(parents=True)
    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=old)
    store.save(AppSettings(storage_location=str(old)))
    parent = tmp_path / "elsewhere"

    with pytest.raises(StorageMigrationError):
        _migration_service(store).move_to_parent(old, parent)

    assert store.load().storage_location == str(old)
    assert old.exists()
    assert not (parent / LIBRARY_DIRECTORY_NAME).exists()
    # Staging copies are cleaned up.
    assert not any(
        child.name.startswith(f".{LIBRARY_DIRECTORY_NAME}.staging-")
        for child in parent.iterdir()
    )


def test_reset_library_clears_root(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    _seed_library(root)
    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=root)

    _migration_service(store).reset_library(root)

    assert root.exists()
    assert list(root.iterdir()) == []


def test_app_context_uses_sqlite_repository_by_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path / "runtime"))

    context = create_app_context()

    assert isinstance(context.book_repository, SqliteBookRepository)
    assert (context.paths.paths.database / "joyread.sqlite3").exists()
    assert Path(context.settings.storage_location).is_relative_to(
        tmp_path / "runtime" / LIBRARY_DIRECTORY_NAME
    )
    assert context.settings_store.settings_path.is_relative_to(tmp_path / "runtime" / ".joyread_support")
    context.database_interpreter.close()


def test_settings_store_source_checkout_default_uses_library_directory_name(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "src" / "joyread").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    store = SettingsStore(support_root=tmp_path / "support")

    assert store.default_storage_root == (tmp_path / LIBRARY_DIRECTORY_NAME).resolve()


def test_app_context_move_storage_preserves_books_under_new_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path / "runtime"))
    context = create_app_context()
    source = tmp_path / "moved.cbz"
    _write_cbz(source)
    context.import_service.import_files([source])
    assert len(context.book_repository.list_books()) == 1
    old_root = Path(context.settings.storage_location)
    parent = tmp_path / "newhome"

    context.move_storage_to_parent(parent)

    new_root = (parent / LIBRARY_DIRECTORY_NAME).resolve()
    assert Path(context.settings.storage_location) == new_root
    assert (new_root / "Database" / "joyread.sqlite3").exists()
    books = context.book_repository.list_books()
    assert len(books) == 1
    # Relative storage paths resolve under the new root and the file exists.
    assert Path(books[0].file_path).is_relative_to(new_root)
    assert Path(books[0].file_path).exists()
    assert not old_root.exists()
    context.close()


def test_app_context_reset_storage_empties_library(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path / "runtime"))
    context = create_app_context()
    source = tmp_path / "reset.cbz"
    _write_cbz(source)
    context.import_service.import_files([source])
    assert len(context.book_repository.list_books()) == 1

    context.reset_storage()

    assert context.book_repository.list_books() == []
    assert (Path(context.settings.storage_location) / "Database" / "joyread.sqlite3").exists()
    context.close()


def test_app_context_select_existing_library_switches_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path / "runtime"))
    context = create_app_context()
    other = tmp_path / "other" / "Arbitrary-Library-Name"
    _seed_library(other)

    result = context.select_storage(other)

    assert result.ok
    assert Path(context.settings.storage_location) == other.resolve()
    assert other.exists()  # Select never deletes the chosen library.
    context.close()


def test_app_context_select_existing_library_rejects_invalid(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path / "runtime"))
    context = create_app_context()
    original_root = context.settings.storage_location
    invalid = tmp_path / "not-a-library"
    invalid.mkdir()

    result = context.select_storage(invalid)

    assert not result.ok
    # Settings unchanged; the current library keeps running.
    assert context.settings.storage_location == original_root
    assert context.book_repository.list_books() == []
    context.close()


def test_app_context_switches_archive_cache_strategy_and_clears_old_pool(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path / "runtime"))
    context = create_app_context()
    source = tmp_path / "book.cb7"
    source.write_bytes(b"fake")
    context.archive_extraction_pool.put(source, "001.png", b"page")
    old_directory = context.archive_extraction_pool.directory
    assert old_directory is not None
    assert any(path.is_file() for path in old_directory.rglob("*"))

    context.settings_store.update(archive_cache_strategy="hidden_image_files")
    context.apply_cache_settings()

    assert isinstance(context.archive_extraction_pool, HiddenImageExtractionPool)
    assert context.archive_extraction_pool.directory is not None
    assert context.archive_extraction_pool.directory.name == ".archive_image_pages"
    assert not any(path.is_file() for path in old_directory.rglob("*"))
    context.close()


def test_migration_9_adds_is_hidden_and_is_hidable_columns(tmp_path: Path) -> None:
    database = _database(tmp_path)
    book_columns = database.execute(
        lambda connection: {
            row["name"] for row in connection.execute("PRAGMA table_info(books)").fetchall()
        }
    )
    collection_columns = database.execute(
        lambda connection: {
            row["name"] for row in connection.execute("PRAGMA table_info(collections)").fetchall()
        }
    )
    indexes = database.execute(
        lambda connection: {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
    )

    assert "is_hidden" in book_columns
    assert "is_hidable" in collection_columns
    assert "idx_books_is_hidden" in indexes
    assert "idx_collections_is_hidable" in indexes
    database.close()


def test_set_book_hidden_clears_favourite_recent_and_normal_collection(tmp_path: Path) -> None:
    source = tmp_path / "to-hide.cbz"
    _write_cbz(source)
    service, database, paths = _import_service(tmp_path)
    service.import_files([source])
    repository = SqliteBookRepository(database, managed_books_root=paths.paths.books)
    book = repository.list_books()[0]
    repository.set_favourite(book.uuid, True)
    repository.set_progress(book.uuid, page_index=5, progress_percent=50.0)
    normal = repository.create_collection("Reading Queue")
    hidable = repository.create_collection("Hidden Stash")
    repository.add_book_to_collection(book.uuid, normal.uuid)
    repository.add_book_to_collection(book.uuid, hidable.uuid)
    repository.set_collection_hidable(hidable.uuid, True)

    repository.set_book_hidden(book.uuid, True)
    refreshed = repository.get_book(book.uuid)

    assert refreshed is not None
    assert refreshed.is_hidden is True
    assert refreshed.is_favourite is False
    assert refreshed.last_read_at is None
    # Hidden books leave normal collections but stay in hidable ones.
    assert normal.uuid not in refreshed.collection_ids
    assert hidable.uuid in refreshed.collection_ids
    database.close()


def test_set_collection_hidable_demotion_drops_hidden_members(tmp_path: Path) -> None:
    source = tmp_path / "demote.cbz"
    _write_cbz(source)
    service, database, paths = _import_service(tmp_path)
    service.import_files([source])
    repository = SqliteBookRepository(database, managed_books_root=paths.paths.books)
    book = repository.list_books()[0]
    hidable = repository.create_collection("Hidable")
    repository.set_collection_hidable(hidable.uuid, True)
    repository.add_book_to_collection(book.uuid, hidable.uuid)
    repository.set_book_hidden(book.uuid, True)

    repository.set_collection_hidable(hidable.uuid, False)
    refreshed = repository.get_book(book.uuid)

    assert refreshed is not None
    # Book stays hidden but is detached from the now-normal collection.
    assert refreshed.is_hidden is True
    assert hidable.uuid not in refreshed.collection_ids
    database.close()


def test_revert_hidden_state_clears_flags_without_deleting_rows(tmp_path: Path) -> None:
    source = tmp_path / "revert.cbz"
    _write_cbz(source)
    service, database, paths = _import_service(tmp_path)
    service.import_files([source])
    repository = SqliteBookRepository(database, managed_books_root=paths.paths.books)
    book = repository.list_books()[0]
    hidable = repository.create_collection("Hidable")
    repository.set_collection_hidable(hidable.uuid, True)
    repository.set_book_hidden(book.uuid, True)

    repository.revert_hidden_state()

    assert repository.list_hidden_book_ids() == []
    assert repository.list_hidable_collection_ids() == []
    refreshed = repository.get_book(book.uuid)
    assert refreshed is not None
    assert refreshed.is_hidden is False
    # Row counts unchanged: revert is a flag clear, not a delete.
    counts = database.execute(
        lambda connection: {
            "books": connection.execute("SELECT COUNT(*) AS c FROM books").fetchone()["c"],
            "collections": connection.execute("SELECT COUNT(*) AS c FROM collections").fetchone()["c"],
        }
    )
    assert counts == {"books": 1, "collections": 1}
    database.close()


def test_app_settings_round_trips_hidden_space_fields(tmp_path: Path) -> None:
    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=tmp_path / "storage")

    store.update(
        hidden_space_password_hash="deadbeef",
        hidden_space_password_salt="c2FsdA==",
        hidden_space_password_hint="dog name",
        show_hidden_collection=True,
    )
    loaded = store.load()

    assert loaded.hidden_space_password_hash == "deadbeef"
    assert loaded.hidden_space_password_salt == "c2FsdA=="
    assert loaded.hidden_space_password_hint == "dog name"
    assert loaded.show_hidden_collection is True
