"""SQLite schema migrations for the JoyRead library database."""

from __future__ import annotations

import logging
import sqlite3
import time


logger = logging.getLogger(__name__)


MIGRATIONS: tuple[tuple[int, str], ...] = (
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
)


def apply_migrations(connection: sqlite3.Connection) -> None:
    connection.create_function("joyread_basename", 1, _sqlite_basename)
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
    for version, sql in MIGRATIONS:
        if version in applied:
            continue
        logger.info("Applying migration %d", version)
        start = time.perf_counter()
        try:
            connection.executescript(
                f"""
                BEGIN;
                {sql}
                INSERT INTO schema_migrations(version) VALUES ({int(version)});
                COMMIT;
                """
            )
        except Exception as exc:
            logger.error(
                "Migration %d failed: %s\nSQL:\n%s",
                version,
                exc,
                sql,
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


def _sqlite_basename(value: object) -> str:
    normalized = str(value or "").replace("\\", "/").rstrip("/")
    if not normalized:
        return "book"
    return normalized.rsplit("/", 1)[-1] or "book"
