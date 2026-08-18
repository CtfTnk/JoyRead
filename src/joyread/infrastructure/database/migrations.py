"""SQLite schema migrations for the JoyRead library database."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from joyread.infrastructure.filesystem.path_service import StoragePathResolver


logger = logging.getLogger(__name__)


MigrationStep = str | Callable[[sqlite3.Connection], None]


def _migrate_book_files_v12(connection: sqlite3.Connection) -> None:
    """Replace mutable source-metadata columns with audit state.

    SQLite cannot drop the two legacy columns while preserving the existing
    foreign-key graph. ``apply_migrations`` runs this callable with foreign
    keys temporarily disabled, then checks the rebuilt graph before commit.
    """

    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(book_files)").fetchall()
    }
    if not columns:
        raise sqlite3.OperationalError("book_files is missing before migration 12")
    original_name = (
        "COALESCE(original_file_name, joyread_basename(original_path))"
        if "original_file_name" in columns
        else "joyread_basename(original_path)"
    )
    connection.execute(
        """
        CREATE TABLE book_files_v12 (
            file_id TEXT PRIMARY KEY,
            original_path TEXT NOT NULL,
            original_file_name TEXT NOT NULL,
            storage_path TEXT NOT NULL,
            file_format TEXT NOT NULL,
            hash_algorithm TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('healthy', 'missing', 'unavailable')),
            integrity_error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(hash_algorithm, content_hash)
        )
        """
    )
    connection.execute(
        f"""
        INSERT INTO book_files_v12(
            file_id, original_path, original_file_name, storage_path, file_format,
            hash_algorithm, content_hash, state, integrity_error_code, created_at, updated_at
        )
        SELECT
            file_id,
            original_path,
            {original_name},
            storage_path,
            file_format,
            hash_algorithm,
            content_hash,
            CASE WHEN state IN ('healthy', 'missing') THEN state ELSE 'unavailable' END,
            NULL,
            created_at,
            updated_at
        FROM book_files
        """
    )
    connection.execute("DROP TABLE book_files")
    connection.execute("ALTER TABLE book_files_v12 RENAME TO book_files")
    connection.execute(
        "CREATE INDEX idx_book_files_hash ON book_files(hash_algorithm, content_hash)"
    )
    connection.execute("CREATE INDEX idx_book_files_state ON book_files(state)")
    connection.execute(
        """
        CREATE TABLE library_maintenance_journal (
            journal_id TEXT PRIMARY KEY,
            operation_kind TEXT NOT NULL CHECK (operation_kind IN ('rename')),
            file_id TEXT NOT NULL,
            from_storage_path TEXT NOT NULL,
            to_storage_path TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX idx_library_maintenance_journal_file ON library_maintenance_journal(file_id)"
    )


MIGRATIONS: tuple[tuple[int, MigrationStep], ...] = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS book_files (
            file_id TEXT PRIMARY KEY,
            original_path TEXT NOT NULL,
            storage_path TEXT NOT NULL,
            file_format TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            hash_algorithm TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('healthy', 'missing')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(hash_algorithm, content_hash)
        );

        CREATE TABLE IF NOT EXISTS books (
            book_id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL REFERENCES book_files(file_id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            author TEXT NOT NULL,
            language_tag TEXT,
            book_type TEXT NOT NULL CHECK (book_type IN ('manga', 'book')),
            cover_path TEXT,
            is_favourite INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS progress (
            book_scope TEXT NOT NULL CHECK (book_scope IN ('public', 'private')),
            book_id TEXT NOT NULL,
            page_index INTEGER NOT NULL DEFAULT 0,
            progress_percent REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (book_scope, book_id)
        );

        CREATE TABLE IF NOT EXISTS bookmarks (
            bookmark_id TEXT PRIMARY KEY,
            book_scope TEXT NOT NULL CHECK (book_scope IN ('public', 'private')),
            book_id TEXT NOT NULL,
            name TEXT NOT NULL,
            page_index INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS collections (
            collection_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS collection_books (
            collection_id TEXT NOT NULL REFERENCES collections(collection_id) ON DELETE CASCADE,
            book_id TEXT NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (collection_id, book_id)
        );

        CREATE TABLE IF NOT EXISTS private_collections (
            private_collection_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS private_books (
            private_book_id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL REFERENCES book_files(file_id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            author TEXT NOT NULL,
            language_tag TEXT,
            book_type TEXT NOT NULL CHECK (book_type IN ('manga', 'book')),
            cover_path TEXT,
            encrypted_cover_path TEXT,
            encryption_status TEXT NOT NULL DEFAULT 'not_encrypted',
            private_collection_id TEXT REFERENCES private_collections(private_collection_id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS import_batches (
            batch_id TEXT PRIMARY KEY,
            manifest_path TEXT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS import_items (
            import_item_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES import_batches(batch_id) ON DELETE CASCADE,
            source_path TEXT NOT NULL,
            external_id TEXT,
            status TEXT NOT NULL,
            book_id TEXT,
            file_id TEXT,
            message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_book_files_hash
            ON book_files(hash_algorithm, content_hash);
        CREATE INDEX IF NOT EXISTS idx_book_files_state
            ON book_files(state);
        CREATE INDEX IF NOT EXISTS idx_books_title
            ON books(title COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_books_author
            ON books(author COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_books_file_id
            ON books(file_id);
        CREATE INDEX IF NOT EXISTS idx_progress_book
            ON progress(book_scope, book_id);
        CREATE INDEX IF NOT EXISTS idx_bookmarks_book
            ON bookmarks(book_scope, book_id);
        CREATE INDEX IF NOT EXISTS idx_collection_books_book
            ON collection_books(book_id);
        CREATE INDEX IF NOT EXISTS idx_import_items_batch
            ON import_items(batch_id);
        """,
    ),
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS recent_books (
            book_id TEXT PRIMARY KEY REFERENCES books(book_id) ON DELETE CASCADE,
            last_read_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_recent_books_last_read
            ON recent_books(last_read_at DESC);
        """,
    ),
    (
        3,
        """
        CREATE TABLE IF NOT EXISTS languages (
            iso_code TEXT PRIMARY KEY,
            plain_text TEXT NOT NULL UNIQUE,
            sort_order INTEGER NOT NULL
        );

        INSERT INTO languages(iso_code, plain_text, sort_order)
        VALUES
            ('en', 'English', 10),
            ('zh', 'Chinese', 20),
            ('ja', 'Japanese', 30),
            ('und', 'Unknown', 40)
        ON CONFLICT(iso_code) DO UPDATE SET
            plain_text = excluded.plain_text,
            sort_order = excluded.sort_order;

        UPDATE books
        SET language_tag = CASE lower(trim(coalesce(language_tag, '')))
            WHEN 'en' THEN 'en'
            WHEN 'english' THEN 'en'
            WHEN 'zh' THEN 'zh'
            WHEN 'zh-cn' THEN 'zh'
            WHEN 'zh-tw' THEN 'zh'
            WHEN 'chinese' THEN 'zh'
            WHEN 'ja' THEN 'ja'
            WHEN 'japanese' THEN 'ja'
            ELSE 'und'
        END;

        UPDATE private_books
        SET language_tag = CASE lower(trim(coalesce(language_tag, '')))
            WHEN 'en' THEN 'en'
            WHEN 'english' THEN 'en'
            WHEN 'zh' THEN 'zh'
            WHEN 'zh-cn' THEN 'zh'
            WHEN 'zh-tw' THEN 'zh'
            WHEN 'chinese' THEN 'zh'
            WHEN 'ja' THEN 'ja'
            WHEN 'japanese' THEN 'ja'
            ELSE 'und'
        END;

        CREATE INDEX IF NOT EXISTS idx_books_language_tag
            ON books(language_tag);
        CREATE INDEX IF NOT EXISTS idx_private_books_language_tag
            ON private_books(language_tag);
        """,
    ),
    (
        4,
        """
        ALTER TABLE book_files ADD COLUMN original_file_name TEXT;

        UPDATE book_files
        SET original_file_name = joyread_basename(original_path)
        WHERE original_file_name IS NULL OR trim(original_file_name) = '';
        """,
    ),
    (
        5,
        """
        CREATE TABLE IF NOT EXISTS reader_settings (
            book_scope TEXT NOT NULL CHECK (book_scope IN ('public', 'private')),
            book_id TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'right_to_left',
            vertical_enabled INTEGER NOT NULL DEFAULT 0,
            page_spacing INTEGER NOT NULL DEFAULT 0,
            custom_enabled INTEGER NOT NULL DEFAULT 0,
            always_one_page INTEGER NOT NULL DEFAULT 0,
            fit_mode TEXT NOT NULL DEFAULT 'auto',
            transition_mode TEXT NOT NULL DEFAULT 'none',
            spread_offset INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (book_scope, book_id)
        );

        CREATE INDEX IF NOT EXISTS idx_reader_settings_book
            ON reader_settings(book_scope, book_id);
        """,
    ),
    (
        6,
        """
        ALTER TABLE reader_settings
            ADD COLUMN vertical_zoom_percent INTEGER NOT NULL DEFAULT 100;
        """,
    ),
    # Note: version 7 is intentionally skipped. An earlier in-development
    # branch (EPUB shell) shipped a different migration 7 and recorded its
    # version row in some local databases. Numbering this ALTER as 8 ensures
    # it actually runs against those drifted databases — and fresh installs
    # are unaffected because the migration runner only cares about the set
    # of versions present, not contiguity.
    (
        8,
        """
        ALTER TABLE reader_settings
            ADD COLUMN vertical_fit_width INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (
        9,
        """
        ALTER TABLE books
            ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0;

        ALTER TABLE collections
            ADD COLUMN is_hidable INTEGER NOT NULL DEFAULT 0;

        CREATE INDEX IF NOT EXISTS idx_books_is_hidden
            ON books(is_hidden);
        CREATE INDEX IF NOT EXISTS idx_collections_is_hidable
            ON collections(is_hidable);
        """,
    ),
    (
        10,
        """
        CREATE TABLE IF NOT EXISTS tags (
            tag_id          TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            name_normalized TEXT NOT NULL UNIQUE,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS book_tags (
            tag_id     TEXT NOT NULL REFERENCES tags(tag_id) ON DELETE CASCADE,
            book_id    TEXT NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (tag_id, book_id)
        );

        CREATE INDEX IF NOT EXISTS idx_book_tags_book ON book_tags(book_id);
        """,
    ),
    # Normalize legacy absolute managed paths to storage-relative form so the
    # library folder can be moved/re-pointed without rewriting rows. The
    # ``joyread_storage_relative`` function (registered in ``apply_migrations``)
    # is bound to the current storage root: absolute paths under it become
    # relative, while paths outside it (or already relative) pass through.
    (
        11,
        """
        UPDATE book_files
            SET storage_path = joyread_storage_relative(storage_path);

        UPDATE books
            SET cover_path = joyread_storage_relative(cover_path)
            WHERE cover_path IS NOT NULL;

        UPDATE private_books
            SET cover_path = joyread_storage_relative(cover_path)
            WHERE cover_path IS NOT NULL;
        """,
    ),
    (12, _migrate_book_files_v12),
)


LATEST_SCHEMA_VERSION: int = max(version for version, _sql in MIGRATIONS)


def apply_migrations(connection: sqlite3.Connection) -> None:
    connection.create_function("joyread_basename", 1, _sqlite_basename)
    connection.create_function(
        "joyread_storage_relative",
        1,
        _make_storage_relativizer(_storage_root_from_connection(connection)),
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied = {
        int(row["version"])
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
    pending = [version for version, _sql in MIGRATIONS if version not in applied]
    logger.info(
        "Migrations: %d already applied, %d pending=%s",
        len(applied),
        len(pending),
        pending,
    )
    for version, migration in MIGRATIONS:
        if version in applied:
            continue
        logger.info("Applying migration %d", version)
        start = time.perf_counter()
        try:
            if isinstance(migration, str):
                connection.executescript(
                    f"""
                    BEGIN;
                    {migration}
                    INSERT INTO schema_migrations(version) VALUES ({int(version)});
                    COMMIT;
                    """
                )
            else:
                _apply_callable_migration(connection, version, migration)
        except Exception as exc:
            logger.error(
                "Migration %d failed: %s\nStep:\n%s",
                version,
                exc,
                migration if isinstance(migration, str) else migration.__name__,
                exc_info=True,
            )
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError as rollback_exc:
                # Silent before; surface so a stuck transaction or closed
                # connection is at least visible in the log.
                logger.warning(
                    "ROLLBACK after migration %d failure also failed: %s",
                    version,
                    rollback_exc,
                )
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        logger.info("Migration %d applied in %.0f ms", version, elapsed_ms)


def _apply_callable_migration(
    connection: sqlite3.Connection,
    version: int,
    migration: Callable[[sqlite3.Connection], None],
) -> None:
    """Run a table-rebuild migration without weakening later connections."""

    foreign_keys_enabled = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN")
        migration(connection)
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            first = violations[0]
            raise sqlite3.IntegrityError(
                f"foreign_key_check failed after migration {version}: {tuple(first)}"
            )
        connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        connection.execute("COMMIT")
    except Exception:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        if foreign_keys_enabled:
            connection.execute("PRAGMA foreign_keys = ON")


def _sqlite_basename(value: object) -> str:
    normalized = str(value or "").replace("\\", "/").rstrip("/")
    if not normalized:
        return "book"
    return normalized.rsplit("/", 1)[-1] or "book"


def _storage_root_from_connection(connection: sqlite3.Connection) -> Path | None:
    """Derive the storage root from the open database file location.

    The library database lives at ``<storage_root>/Database/joyread.sqlite3``,
    so its grandparent is the storage root. In-memory or path-less connections
    (some tests) yield no file, in which case normalization is skipped.
    """

    try:
        rows = connection.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        # PRAGMA database_list columns: (seq, name, file).
        if row[1] == "main" and row[2]:
            return Path(row[2]).resolve().parent.parent
    return None


def _make_storage_relativizer(storage_root: Path | None) -> Callable[[object], object]:
    resolver = StoragePathResolver(storage_root) if storage_root is not None else None

    def relativize(value: object) -> object:
        if value is None or resolver is None:
            return value
        try:
            return resolver.to_storage_relative(str(value))
        except (ValueError, OSError):
            # Path is outside the current storage root (legacy/foreign), or an OS
            # error occurred resolving it (e.g. broken symlink). Leave it unchanged
            # so it surfaces as missing rather than being rewritten to a bad value.
            return value

    return relativize
