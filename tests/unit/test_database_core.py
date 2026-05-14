from __future__ import annotations

from io import BytesIO
from pathlib import Path
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
from joyread.core.services.storage_migration_service import StorageMigrationService
from joyread.infrastructure.config.settings_store import AppSettings, SettingsStore
from joyread.infrastructure.database import DatabaseInterpreter, DatabasePriority, apply_migrations
from joyread.infrastructure.database.migrations import MIGRATIONS
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


def test_import_skips_unencrypted_archives_containing_encrypted_archives(tmp_path: Path) -> None:
    source = tmp_path / "outer.cbz"
    _write_cbz_with_nested_encrypted_archive(source)
    service, database, _paths = _import_service(tmp_path)

    result = service.import_files([source])

    assert result.imported_count == 0
    assert result.skipped_count == 1
    assert result.failed_count == 0
    assert "outer.cbz::nested.cbz" in (result.items[0].message or "")
    assert SqliteBookRepository(database).list_books() == []
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
    corrupt = tmp_path / "corrupt.cbz"
    corrupt.write_bytes(b"not a zip")
    service, database, _paths = _import_service(tmp_path)

    result = service.import_files([tmp_path / "missing.cbz", unsupported, corrupt])
    item_rows = database.execute(lambda connection: connection.execute("SELECT status FROM import_items").fetchall())

    assert result.failed_count == 3
    assert SqliteBookRepository(database).list_books() == []
    assert [row["status"] for row in item_rows] == ["failed", "failed", "failed"]
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


def test_storage_migration_moves_storage_and_updates_config(tmp_path: Path) -> None:
    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=tmp_path / "old")
    store.save(AppSettings(storage_location=str(tmp_path / "old")))
    old = tmp_path / "old"
    old.mkdir()
    (old / "Books").mkdir()
    (old / "Books" / "book.cbz").write_bytes(b"book")

    result = StorageMigrationService(store).move_storage_location(old, tmp_path / "new")

    assert (tmp_path / "new" / "Books" / "book.cbz").exists()
    assert result.old_backup_root is not None
    assert result.old_backup_root.exists()
    assert store.load().storage_location == str((tmp_path / "new").resolve())


def test_app_context_uses_sqlite_repository_by_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path / "runtime"))

    context = create_app_context()

    assert isinstance(context.book_repository, SqliteBookRepository)
    assert (context.paths.paths.database / "joyread.sqlite3").exists()
    assert context.settings_store.settings_path.is_relative_to(tmp_path / "runtime" / ".joyread_support")
    context.database_interpreter.close()


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
