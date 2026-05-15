"""SQLite-backed JoyRead library repository."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from uuid import uuid4

from joyread.core.models.book import Book
from joyread.core.models.bookmark import Bookmark
from joyread.core.models.collection import Collection
from joyread.core.models.export import BookExportRecord
from joyread.core.models.language import Language
from joyread.core.repositories.book_repository import BookRepository
from joyread.core.reader.models import (
    ReaderDirection,
    ReaderFitMode,
    ReaderProgress,
    ReaderSettings,
    ReaderTransitionMode,
)
from joyread.infrastructure.database.database_interpreter import DatabaseInterpreter, DatabasePriority


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _BookDeletionCleanup:
    storage_path: Path | None
    cover_path: Path | None
    generated_cover_pattern: str | None


class SqliteBookRepository(BookRepository):
    _SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

    def __init__(
        self,
        database: DatabaseInterpreter,
        *,
        managed_books_root: Path | None = None,
        thumbnails_root: Path | None = None,
    ) -> None:
        self._database = database
        self._managed_books_root = managed_books_root.resolve() if managed_books_root is not None else None
        self._thumbnails_root = thumbnails_root.resolve() if thumbnails_root is not None else None

    def list_books(self) -> list[Book]:
        return self._database.execute(_list_books, DatabasePriority.HIGH)

    def list_collections(self) -> list[Collection]:
        return self._database.execute(_list_collections, DatabasePriority.HIGH)

    def list_languages(self) -> list[Language]:
        return self._database.execute(_list_languages, DatabasePriority.HIGH)

    def get_export_records(self, book_ids: tuple[str, ...]) -> list[BookExportRecord]:
        target_ids = tuple(dict.fromkeys(book_ids))
        if not target_ids:
            return []

        def read(connection: sqlite3.Connection) -> list[BookExportRecord]:
            placeholders = ", ".join("?" for _book_id in target_ids)
            rows = connection.execute(
                f"""
                SELECT
                    books.book_id,
                    books.title,
                    book_files.storage_path,
                    book_files.original_path,
                    book_files.original_file_name,
                    book_files.hash_algorithm,
                    book_files.content_hash,
                    book_files.state
                FROM books
                JOIN book_files ON book_files.file_id = books.file_id
                WHERE books.book_id IN ({placeholders})
                """,
                target_ids,
            ).fetchall()
            records_by_id = {row["book_id"]: _export_record_from_row(row) for row in rows}
            return [records_by_id[book_id] for book_id in target_ids if book_id in records_by_id]

        return self._database.execute(read, DatabasePriority.HIGH)

    def get_book(self, book_id: str) -> Book | None:
        books = self._database.execute(lambda connection: _list_books(connection, book_id), DatabasePriority.HIGH)
        return books[0] if books else None

    def update_book_metadata(
        self,
        book_id: str,
        *,
        title: str | None = None,
        author: str | None = None,
        language_tag: str | None = None,
    ) -> None:
        updates: list[str] = []
        values: list[object] = []
        if title is not None:
            updates.append("title = ?")
            values.append(title)
        if author is not None:
            updates.append("author = ?")
            values.append(author)
        if language_tag is not None:
            updates.append("language_tag = ?")
            values.append(language_tag)
        if not updates:
            return
        logger.debug(
            "update_book_metadata book=%s fields=%s",
            book_id,
            [field.split(" ", 1)[0] for field in updates],
        )
        updates.append("updated_at = ?")
        values.append(_now())
        values.append(book_id)

        def write(connection: sqlite3.Connection) -> None:
            if language_tag is not None and not _language_exists(connection, language_tag):
                raise ValueError(f"Unknown language code: {language_tag}")
            connection.execute(
                f"UPDATE books SET {', '.join(updates)} WHERE book_id = ?",
                tuple(values),
            )

        self._database.execute(
            write,
            DatabasePriority.NORMAL,
        )

    def set_favourite(self, book_id: str, is_favourite: bool) -> None:
        logger.debug("set_favourite book=%s value=%s", book_id, is_favourite)
        now = _now()
        self._database.execute(
            lambda connection: connection.execute(
                "UPDATE books SET is_favourite = ?, updated_at = ? WHERE book_id = ?",
                (1 if is_favourite else 0, now, book_id),
            ),
            DatabasePriority.NORMAL,
        )

    def delete_book(self, book_id: str) -> None:
        logger.info("delete_book %s", book_id)
        cleanup = self._database.execute(
            lambda connection: self._delete_book_records(connection, book_id),
            DatabasePriority.NORMAL,
        )
        self._delete_managed_artifacts(cleanup)

    def mark_recent(self, book_id: str) -> None:
        now = _now()

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO recent_books(book_id, last_read_at)
                SELECT book_id, ? FROM books WHERE book_id = ?
                ON CONFLICT(book_id) DO UPDATE SET
                    last_read_at = excluded.last_read_at
                """,
                (now, book_id),
            )
            _trim_recent_books(connection)

        self._database.execute(write, DatabasePriority.NORMAL)

    def remove_book_from_recent(self, book_id: str) -> None:
        self._database.execute(
            lambda connection: connection.execute(
                "DELETE FROM recent_books WHERE book_id = ?",
                (book_id,),
            ),
            DatabasePriority.NORMAL,
        )

    def _delete_book_records(self, connection: sqlite3.Connection, book_id: str) -> _BookDeletionCleanup:
        row = connection.execute(
            """
            SELECT
                books.file_id,
                books.cover_path,
                book_files.storage_path
            FROM books
            JOIN book_files ON book_files.file_id = books.file_id
            WHERE books.book_id = ?
            """,
            (book_id,),
        ).fetchone()
        if row is None:
            return _BookDeletionCleanup(storage_path=None, cover_path=None, generated_cover_pattern=None)

        file_id = row["file_id"]
        storage_path = Path(row["storage_path"])
        cover_path = Path(row["cover_path"]) if row["cover_path"] else None
        should_delete_storage_file = False

        connection.execute("BEGIN")
        try:
            connection.execute(
                "DELETE FROM bookmarks WHERE book_scope = 'public' AND book_id = ?",
                (book_id,),
            )
            connection.execute(
                "DELETE FROM progress WHERE book_scope = 'public' AND book_id = ?",
                (book_id,),
            )
            connection.execute("DELETE FROM recent_books WHERE book_id = ?", (book_id,))
            connection.execute("DELETE FROM collection_books WHERE book_id = ?", (book_id,))
            connection.execute(
                "DELETE FROM import_items WHERE book_id = ? OR file_id = ?",
                (book_id, file_id),
            )
            connection.execute("DELETE FROM books WHERE book_id = ?", (book_id,))

            remaining_refs = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM books WHERE file_id = ?) +
                    (SELECT COUNT(*) FROM private_books WHERE file_id = ?) AS count
                """,
                (file_id, file_id),
            ).fetchone()["count"]
            should_delete_storage_file = int(remaining_refs) == 0
            if should_delete_storage_file:
                connection.execute("DELETE FROM book_files WHERE file_id = ?", (file_id,))
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

        return _BookDeletionCleanup(
            storage_path=storage_path if should_delete_storage_file else None,
            cover_path=cover_path,
            generated_cover_pattern=f"{self._safe_book_uuid(book_id)}-*.png",
        )

    def _delete_managed_artifacts(self, cleanup: _BookDeletionCleanup) -> None:
        errors: list[str] = []
        if cleanup.storage_path is not None:
            error = self._unlink_if_managed(cleanup.storage_path, self._managed_books_root)
            if error is not None:
                errors.append(error)
        if cleanup.cover_path is not None:
            error = self._unlink_if_managed(cleanup.cover_path, self._thumbnails_root)
            if error is not None:
                errors.append(error)
        if self._thumbnails_root is not None and cleanup.generated_cover_pattern is not None:
            covers_dir = self._thumbnails_root / "covers"
            if covers_dir.exists():
                for cover_path in covers_dir.glob(cleanup.generated_cover_pattern):
                    error = self._unlink_if_managed(cover_path, self._thumbnails_root)
                    if error is not None:
                        errors.append(error)

        if errors:
            raise OSError("JoyRead deleted the database record, but cleanup failed: " + "; ".join(errors))

    def _unlink_if_managed(self, path: Path, managed_root: Path | None) -> str | None:
        if managed_root is None:
            return None
        try:
            resolved = path.expanduser().resolve()
        except OSError as exc:
            return f"{path}: {exc}"
        if not resolved.is_relative_to(managed_root):
            return None
        try:
            resolved.unlink(missing_ok=True)
        except OSError as exc:
            return f"{resolved}: {exc}"
        return None

    def _safe_book_uuid(self, book_uuid: str) -> str:
        return self._SAFE_NAME_RE.sub("_", book_uuid).strip("_") or "book"

    def create_collection(self, name: str) -> Collection:
        collection_id = str(uuid4())
        logger.info("create_collection name=%r id=%s", name, collection_id)
        now = _now()

        def write(connection: sqlite3.Connection) -> Collection:
            connection.execute(
                """
                INSERT INTO collections(collection_id, name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (collection_id, name, now, now),
            )
            return Collection(
                uuid=collection_id,
                name=name,
                is_private=False,
                created_at=datetime.fromisoformat(now),
                updated_at=datetime.fromisoformat(now),
            )

        return self._database.execute(write, DatabasePriority.NORMAL)

    def delete_collection(self, collection_id: str) -> None:
        logger.info("delete_collection %s", collection_id)
        self._database.execute(
            lambda connection: connection.execute(
                "DELETE FROM collections WHERE collection_id = ?",
                (collection_id,),
            ),
            DatabasePriority.NORMAL,
        )

    def rename_collection(self, collection_id: str, name: str) -> None:
        logger.debug("rename_collection %s -> %r", collection_id, name)
        now = _now()
        self._database.execute(
            lambda connection: connection.execute(
                "UPDATE collections SET name = ?, updated_at = ? WHERE collection_id = ?",
                (name, now, collection_id),
            ),
            DatabasePriority.NORMAL,
        )

    def add_book_to_collection(self, book_id: str, collection_id: str) -> None:
        logger.debug("add_book_to_collection book=%s collection=%s", book_id, collection_id)
        now = _now()
        self._database.execute(
            lambda connection: connection.execute(
                """
                INSERT OR IGNORE INTO collection_books(collection_id, book_id, created_at)
                VALUES (?, ?, ?)
                """,
                (collection_id, book_id, now),
            ),
            DatabasePriority.NORMAL,
        )

    def remove_book_from_collection(self, book_id: str, collection_id: str) -> None:
        logger.debug("remove_book_from_collection book=%s collection=%s", book_id, collection_id)
        self._database.execute(
            lambda connection: connection.execute(
                """
                DELETE FROM collection_books
                WHERE collection_id = ? AND book_id = ?
                """,
                (collection_id, book_id),
            ),
            DatabasePriority.NORMAL,
        )

    def set_progress(self, book_id: str, page_index: int, progress_percent: float) -> None:
        logger.debug(
            "set_progress book=%s page=%d percent=%.2f", book_id, page_index, progress_percent
        )
        now = _now()

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO progress(book_scope, book_id, page_index, progress_percent, updated_at)
                VALUES ('public', ?, ?, ?, ?)
                ON CONFLICT(book_scope, book_id) DO UPDATE SET
                    page_index = excluded.page_index,
                    progress_percent = excluded.progress_percent,
                    updated_at = excluded.updated_at
                """,
                (book_id, page_index, progress_percent, now),
            )
            connection.execute(
                """
                INSERT INTO recent_books(book_id, last_read_at)
                SELECT book_id, ? FROM books WHERE book_id = ?
                ON CONFLICT(book_id) DO UPDATE SET
                    last_read_at = excluded.last_read_at
                """,
                (now, book_id),
            )
            _trim_recent_books(connection)

        self._database.execute(write, DatabasePriority.NORMAL)

    def get_progress(self, book_id: str, book_scope: str = "public") -> ReaderProgress | None:
        def read(connection: sqlite3.Connection) -> ReaderProgress | None:
            row = connection.execute(
                """
                SELECT page_index, progress_percent
                FROM progress
                WHERE book_scope = ? AND book_id = ?
                """,
                (book_scope, book_id),
            ).fetchone()
            if row is None:
                return None
            return ReaderProgress(
                page_index=int(row["page_index"]),
                progress_percent=float(row["progress_percent"]),
            )

        return self._database.execute(read, DatabasePriority.HIGH)

    def get_reader_settings(self, book_id: str, book_scope: str = "public") -> ReaderSettings | None:
        def read(connection: sqlite3.Connection) -> ReaderSettings | None:
            row = connection.execute(
                """
                SELECT
                    direction,
                    vertical_enabled,
                    vertical_fit_width,
                    page_spacing,
                    vertical_zoom_percent,
                    custom_enabled,
                    always_one_page,
                    fit_mode,
                    transition_mode,
                    spread_offset
                FROM reader_settings
                WHERE book_scope = ? AND book_id = ?
                """,
                (book_scope, book_id),
            ).fetchone()
            if row is None:
                return None
            return _reader_settings_from_row(row)

        return self._database.execute(read, DatabasePriority.HIGH)

    def save_reader_settings(
        self,
        book_id: str,
        settings: ReaderSettings,
        book_scope: str = "public",
    ) -> None:
        logger.debug(
            "save_reader_settings scope=%s book=%s direction=%s fit=%s vertical_custom=%s",
            book_scope,
            book_id,
            settings.direction.value,
            settings.fit_mode.value,
            settings.vertical_custom_enabled,
        )
        now = _now()

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO reader_settings(
                    book_scope,
                    book_id,
                    direction,
                    vertical_enabled,
                    vertical_fit_width,
                    page_spacing,
                    vertical_zoom_percent,
                    custom_enabled,
                    always_one_page,
                    fit_mode,
                    transition_mode,
                    spread_offset,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(book_scope, book_id) DO UPDATE SET
                    direction = excluded.direction,
                    vertical_enabled = excluded.vertical_enabled,
                    vertical_fit_width = excluded.vertical_fit_width,
                    page_spacing = excluded.page_spacing,
                    vertical_zoom_percent = excluded.vertical_zoom_percent,
                    custom_enabled = excluded.custom_enabled,
                    always_one_page = excluded.always_one_page,
                    fit_mode = excluded.fit_mode,
                    transition_mode = excluded.transition_mode,
                    spread_offset = excluded.spread_offset,
                    updated_at = excluded.updated_at
                """,
                (
                    book_scope,
                    book_id,
                    settings.direction.value,
                    1 if settings.vertical_custom_enabled else 0,
                    1 if settings.vertical_fit_width else 0,
                    int(settings.page_spacing),
                    int(settings.vertical_zoom_percent),
                    1 if settings.custom_enabled else 0,
                    1 if settings.always_one_page else 0,
                    settings.fit_mode.value,
                    settings.transition_mode.value,
                    int(settings.spread_offset),
                    now,
                ),
            )

        self._database.execute(write, DatabasePriority.NORMAL)

    def add_bookmark(self, book_id: str, name: str, page_index: int, book_scope: str = "public") -> Bookmark:
        bookmark_id = str(uuid4())
        logger.debug(
            "add_bookmark scope=%s book=%s page=%d id=%s name=%r",
            book_scope,
            book_id,
            page_index,
            bookmark_id,
            name,
        )
        now = _now()

        def write(connection: sqlite3.Connection) -> Bookmark:
            connection.execute(
                """
                INSERT INTO bookmarks(
                    bookmark_id, book_scope, book_id, name, page_index, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (bookmark_id, book_scope, book_id, name, page_index, now, now),
            )
            return Bookmark(
                uuid=bookmark_id,
                book_scope=book_scope,
                book_uuid=book_id,
                name=name,
                page_index=page_index,
                created_at=datetime.fromisoformat(now),
                updated_at=datetime.fromisoformat(now),
            )

        return self._database.execute(write, DatabasePriority.NORMAL)

    def list_bookmarks(self, book_id: str, book_scope: str = "public") -> list[Bookmark]:
        return self._database.execute(
            lambda connection: [
                Bookmark(
                    uuid=row["bookmark_id"],
                    book_scope=row["book_scope"],
                    book_uuid=row["book_id"],
                    name=row["name"],
                    page_index=row["page_index"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                )
                for row in connection.execute(
                    """
                    SELECT bookmark_id, book_scope, book_id, name, page_index, created_at, updated_at
                    FROM bookmarks
                    WHERE book_scope = ? AND book_id = ?
                    ORDER BY page_index ASC, created_at ASC
                    """,
                    (book_scope, book_id),
                ).fetchall()
            ],
            DatabasePriority.HIGH,
        )

    def rename_bookmark(
        self,
        book_id: str,
        bookmark_id: str,
        name: str,
        book_scope: str = "public",
    ) -> None:
        logger.debug(
            "rename_bookmark scope=%s book=%s id=%s name=%r",
            book_scope,
            book_id,
            bookmark_id,
            name,
        )
        now = _now()

        def write(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE bookmarks
                SET name = ?, updated_at = ?
                WHERE bookmark_id = ? AND book_scope = ? AND book_id = ?
                """,
                (name, now, bookmark_id, book_scope, book_id),
            )
            if cursor.rowcount <= 0:
                raise ValueError(f"Bookmark does not exist: {bookmark_id}")

        self._database.execute(write, DatabasePriority.NORMAL)

    def delete_bookmark(self, book_id: str, bookmark_id: str, book_scope: str = "public") -> None:
        logger.debug(
            "delete_bookmark scope=%s book=%s id=%s", book_scope, book_id, bookmark_id
        )

        def write(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                DELETE FROM bookmarks
                WHERE bookmark_id = ? AND book_scope = ? AND book_id = ?
                """,
                (bookmark_id, book_scope, book_id),
            )
            if cursor.rowcount <= 0:
                raise ValueError(f"Bookmark does not exist: {bookmark_id}")

        self._database.execute(write, DatabasePriority.NORMAL)

    def move_book_to_private(self, book_id: str, private_collection_id: str | None = None) -> str:
        private_book_id = str(uuid4())
        now = _now()

        def write(connection: sqlite3.Connection) -> str:
            row = connection.execute(
                """
                SELECT book_id, file_id, title, author, language_tag, book_type, cover_path, created_at
                FROM books
                WHERE book_id = ?
                """,
                (book_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Book does not exist: {book_id}")

            connection.execute("BEGIN")
            try:
                connection.execute("DELETE FROM collection_books WHERE book_id = ?", (book_id,))
                connection.execute(
                    """
                    INSERT INTO private_books(
                        private_book_id, file_id, title, author, language_tag, book_type,
                        cover_path, encrypted_cover_path, encryption_status,
                        private_collection_id, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'not_encrypted', ?, ?, ?)
                    """,
                    (
                        private_book_id,
                        row["file_id"],
                        row["title"],
                        row["author"],
                        row["language_tag"],
                        row["book_type"],
                        row["cover_path"],
                        private_collection_id,
                        row["created_at"],
                        now,
                    ),
                )
                connection.execute("DELETE FROM books WHERE book_id = ?", (book_id,))
            except Exception:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")
            return private_book_id

        return self._database.execute(write, DatabasePriority.NORMAL)


def _list_books(connection: sqlite3.Connection, book_id: str | None = None) -> list[Book]:
    where = "WHERE books.book_id = ?" if book_id is not None else ""
    parameters = (book_id,) if book_id is not None else ()
    rows = connection.execute(
        f"""
        SELECT
            books.book_id,
            books.title,
            books.author,
            books.language_tag,
            COALESCE(languages.plain_text, 'Unknown') AS language_name,
            books.book_type,
            books.cover_path,
            books.is_favourite,
            books.created_at,
            books.updated_at,
            book_files.storage_path,
            book_files.original_path,
            book_files.original_file_name,
            book_files.file_format,
            book_files.state,
            progress.progress_percent,
            recent_books.last_read_at,
            GROUP_CONCAT(collection_books.collection_id) AS collection_ids
        FROM books
        JOIN book_files ON book_files.file_id = books.file_id
        LEFT JOIN languages ON languages.iso_code = books.language_tag
        LEFT JOIN progress
            ON progress.book_scope = 'public' AND progress.book_id = books.book_id
        LEFT JOIN recent_books ON recent_books.book_id = books.book_id
        LEFT JOIN collection_books ON collection_books.book_id = books.book_id
        {where}
        GROUP BY books.book_id
        ORDER BY books.created_at DESC, books.title COLLATE NOCASE ASC
        """,
        parameters,
    ).fetchall()
    return [_book_from_row(row) for row in rows]


def _list_collections(connection: sqlite3.Connection) -> list[Collection]:
    rows = connection.execute(
        """
        SELECT collection_id, name, created_at, updated_at
        FROM collections
        ORDER BY name COLLATE NOCASE ASC
        """
    ).fetchall()
    return [
        Collection(
            uuid=row["collection_id"],
            name=row["name"],
            is_private=False,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
        for row in rows
    ]


def _list_languages(connection: sqlite3.Connection) -> list[Language]:
    rows = connection.execute(
        """
        SELECT iso_code, plain_text
        FROM languages
        ORDER BY sort_order ASC, plain_text COLLATE NOCASE ASC
        """
    ).fetchall()
    return [
        Language(
            plain_text=row["plain_text"],
            iso_code=row["iso_code"],
        )
        for row in rows
    ]


def _book_from_row(row: sqlite3.Row) -> Book:
    collection_ids = tuple(
        value for value in (row["collection_ids"] or "").split(",") if value
    )
    progress_percent = float(row["progress_percent"] or 0.0)
    return Book(
        uuid=row["book_id"],
        title=row["title"],
        author=row["author"],
        language_tag=row["language_tag"],
        language_name=row["language_name"],
        book_type=row["book_type"],
        file_format=row["file_format"],
        file_path=row["storage_path"],
        progress=max(0.0, min(100.0, progress_percent)) / 100.0,
        cover_thumbnail_path=row["cover_path"],
        added_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        last_read_at=(
            datetime.fromisoformat(row["last_read_at"])
            if row["last_read_at"] is not None
            else None
        ),
        is_favourite=bool(row["is_favourite"]),
        is_missing=row["state"] == "missing",
        collection_ids=collection_ids,
        page_count=0,
        original_file_name=_original_file_name_from_row(row),
    )


def _export_record_from_row(row: sqlite3.Row) -> BookExportRecord:
    return BookExportRecord(
        book_uuid=row["book_id"],
        title=row["title"],
        storage_path=row["storage_path"],
        original_file_name=_original_file_name_from_row(row),
        hash_algorithm=row["hash_algorithm"],
        content_hash=row["content_hash"],
        is_missing=row["state"] == "missing",
    )


def _original_file_name_from_row(row: sqlite3.Row) -> str:
    return (
        str(row["original_file_name"] or "").strip()
        or _basename(row["original_path"])
        or _basename(row["storage_path"])
        or "book"
    )


def _basename(value: object) -> str:
    normalized = str(value or "").replace("\\", "/").rstrip("/")
    if not normalized:
        return ""
    return normalized.rsplit("/", 1)[-1]


def _language_exists(connection: sqlite3.Connection, language_tag: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM languages WHERE iso_code = ?",
        (language_tag,),
    ).fetchone()
    return row is not None


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


def _trim_recent_books(connection: sqlite3.Connection) -> None:
    # Recent is a public-shelf convenience list, not private-space history.
    connection.execute(
        """
        DELETE FROM recent_books
        WHERE book_id NOT IN (
            SELECT book_id
            FROM recent_books
            ORDER BY last_read_at DESC
            LIMIT 10
        )
        """
    )


def _reader_settings_from_row(row: sqlite3.Row) -> ReaderSettings:
    return ReaderSettings(
        direction=_coerce_direction(row["direction"]),
        vertical_custom_enabled=bool(row["vertical_enabled"]),
        vertical_fit_width=bool(row["vertical_fit_width"]),
        page_spacing=max(0, int(row["page_spacing"])),
        vertical_zoom_percent=max(25, min(200, int(row["vertical_zoom_percent"] or 100))),
        custom_enabled=bool(row["custom_enabled"]),
        always_one_page=bool(row["always_one_page"]),
        fit_mode=_coerce_fit_mode(row["fit_mode"]),
        transition_mode=_coerce_transition_mode(row["transition_mode"]),
        spread_offset=1 if int(row["spread_offset"] or 0) else 0,
    )


def _coerce_direction(value: object) -> ReaderDirection:
    try:
        return ReaderDirection(str(value))
    except ValueError:
        return ReaderDirection.RIGHT_TO_LEFT


def _coerce_fit_mode(value: object) -> ReaderFitMode:
    try:
        return ReaderFitMode(str(value))
    except ValueError:
        return ReaderFitMode.AUTO


def _coerce_transition_mode(value: object) -> ReaderTransitionMode:
    try:
        return ReaderTransitionMode(str(value))
    except ValueError:
        return ReaderTransitionMode.NONE
