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
from joyread.infrastructure.filesystem.path_service import StoragePathResolver


logger = logging.getLogger(__name__)


def _resolve_managed(resolver: StoragePathResolver | None, value: object) -> str | None:
    """Turn a stored (relative) managed path into an absolute one for callers.

    Legacy rows that still hold an absolute path — or any path outside the
    current storage root — are passed through unchanged so a moved library
    surfaces them as ``missing`` rather than silently rewriting them.
    """

    if value is None:
        return None
    text = str(value)
    if resolver is None:
        if text and not Path(text).is_absolute():
            logger.warning(
                "No storage resolver available; returning raw relative path %r — "
                "callers will resolve it against CWD",
                text,
            )
        return text
    try:
        return str(resolver.to_storage_absolute(text))
    except ValueError:
        return text


def _resolver_from_database(database: DatabaseInterpreter) -> StoragePathResolver | None:
    """Derive a resolver from ``<storage_root>/Database/joyread.sqlite3``.

    Only returns a resolver when the database actually lives inside a
    ``Database/`` subdirectory of the storage root.  A flat or non-standard path
    (e.g. a test database placed directly in ``tmp_path``) returns ``None`` so
    callers fall back to passing through raw stored values rather than resolving
    against a structurally wrong root.
    """

    database_path = getattr(database, "database_path", None)
    if database_path is None:
        return None
    path = Path(database_path)
    if not path.is_absolute():
        return None
    if path.parent.name != "Database":
        return None
    return StoragePathResolver(path.parent.parent)


def _relativize_managed(resolver: StoragePathResolver | None, value: object) -> str | None:
    """Turn an absolute managed path into the storage-relative form to persist."""

    if value is None:
        return None
    text = str(value)
    if resolver is None:
        return text
    try:
        return resolver.to_storage_relative(text)
    except ValueError:
        return text


@dataclass(frozen=True)
class _BookDeletionCleanup:
    storage_path: Path | None
    cover_path: Path | None
    generated_cover_pattern: str | None


class SqliteBookRepository(BookRepository):
    # Sanitises user-supplied book UUIDs before they are embedded in
    # generated thumbnail/cover filenames. Anything outside the allow-list
    # is collapsed to ``_`` so a malicious or weird UUID cannot escape the
    # thumbnails directory via path separators.
    _SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

    def __init__(
        self,
        database: DatabaseInterpreter,
        *,
        resolver: StoragePathResolver | None = None,
        managed_books_root: Path | None = None,
        thumbnails_root: Path | None = None,
    ) -> None:
        self._database = database
        # Fall back to deriving the storage root from the database location
        # (``<storage_root>/Database/joyread.sqlite3``) so the repository can
        # always resolve managed relative paths even when a resolver is not
        # injected explicitly.
        self._resolver = resolver or _resolver_from_database(database)
        self._managed_books_root = managed_books_root.resolve() if managed_books_root is not None else None
        self._thumbnails_root = thumbnails_root.resolve() if thumbnails_root is not None else None

    def list_books(self) -> list[Book]:
        # State refresh runs only on the user-action ``get_book`` path
        # so a shelf load doesn't pay a per-book ``Path.exists()`` stat.
        books = self._database.execute(
            lambda connection: _list_books(connection, resolver=self._resolver),
            DatabasePriority.HIGH,
        )
        logger.debug("list_books returned count=%d", len(books))
        return books

    def list_collections(self) -> list[Collection]:
        collections = self._database.execute(_list_collections, DatabasePriority.HIGH)
        logger.debug("list_collections returned count=%d", len(collections))
        return collections

    def list_languages(self) -> list[Language]:
        return self._database.execute(_list_languages, DatabasePriority.HIGH)

    def get_export_records(self, book_ids: tuple[str, ...]) -> list[BookExportRecord]:
        # ``dict.fromkeys`` is an order-preserving dedup: exports must follow
        # the order the user selected (shelf order, multi-select sequence),
        # not the row order SQLite happens to return.
        target_ids = tuple(dict.fromkeys(book_ids))
        if not target_ids:
            return []

        def read(connection: sqlite3.Connection) -> list[BookExportRecord]:
            # Export is a manual per-book user action — same category
            # as open/detail — so refresh state for the targeted books
            # first. Without this, an EXPORT against a moved/deleted
            # file would report a per-book failure but leave the row's
            # ``state`` stale, so the shelf still shows the book as
            # healthy until the user later clicks it.
            _refresh_book_file_states(connection, target_ids, resolver=self._resolver)
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
            records_by_id = {
                row["book_id"]: _export_record_from_row(row, resolver=self._resolver) for row in rows
            }
            return [records_by_id[book_id] for book_id in target_ids if book_id in records_by_id]

        return self._database.execute(read, DatabasePriority.HIGH)

    def get_book(self, book_id: str) -> Book | None:
        def read(connection: sqlite3.Connection) -> list[Book]:
            _refresh_book_file_states(connection, (book_id,), resolver=self._resolver)
            return _list_books(connection, book_id, resolver=self._resolver)

        books = self._database.execute(read, DatabasePriority.HIGH)
        if not books:
            logger.debug("get_book book=%s miss", book_id)
            return None
        logger.debug("get_book book=%s hit", book_id)
        return books[0]

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

    def set_book_cover_path(self, book_id: str, cover_path: str) -> None:
        # Covers live under the managed Thumbnails/ tree, so persist them
        # relative to the storage root just like book files.
        stored_path = _relativize_managed(self._resolver, cover_path)
        logger.debug("set_book_cover_path book=%s path=%s stored=%s", book_id, cover_path, stored_path)
        self._database.execute(
            lambda connection: connection.execute(
                "UPDATE books SET cover_path = ?, updated_at = ? WHERE book_id = ?",
                (stored_path, _now(), book_id),
            ),
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

    def set_book_hidden(self, book_id: str, hidden: bool) -> None:
        logger.debug("set_book_hidden book=%s value=%s", book_id, hidden)
        now = _now()

        def write(connection: sqlite3.Connection) -> None:
            if hidden:
                # Hiding a book is a "move to Hidden" operation: clear the
                # favourite flag, drop it from Recent, and detach from every
                # non-hidable collection. Hidable collections are the one
                # place where hidden + non-hidden books coexist, so we leave
                # those memberships alone.
                connection.execute(
                    "UPDATE books SET is_hidden = 1, is_favourite = 0, updated_at = ? WHERE book_id = ?",
                    (now, book_id),
                )
                connection.execute("DELETE FROM recent_books WHERE book_id = ?", (book_id,))
                connection.execute(
                    """
                    DELETE FROM collection_books
                    WHERE book_id = ?
                      AND collection_id IN (
                          SELECT collection_id FROM collections WHERE is_hidable = 0
                      )
                    """,
                    (book_id,),
                )
            else:
                connection.execute(
                    "UPDATE books SET is_hidden = 0, updated_at = ? WHERE book_id = ?",
                    (now, book_id),
                )

        self._database.execute(write, DatabasePriority.NORMAL)

    def set_collection_hidable(self, collection_id: str, hidable: bool) -> None:
        logger.debug("set_collection_hidable collection=%s value=%s", collection_id, hidable)
        now = _now()

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE collections SET is_hidable = ?, updated_at = ? WHERE collection_id = ?",
                (1 if hidable else 0, now, collection_id),
            )
            if not hidable:
                # Demoting a hidable collection back to normal: a normal
                # collection must never contain hidden books, so detach any
                # hidden members. The books themselves stay hidden (still
                # visible only under the Hidden shelf).
                connection.execute(
                    """
                    DELETE FROM collection_books
                    WHERE collection_id = ?
                      AND book_id IN (SELECT book_id FROM books WHERE is_hidden = 1)
                    """,
                    (collection_id,),
                )

        self._database.execute(write, DatabasePriority.NORMAL)

    def revert_hidden_state(self) -> None:
        logger.info("revert_hidden_state: clearing is_hidden + is_hidable across the library")
        now = _now()

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE books SET is_hidden = 0, updated_at = ? WHERE is_hidden = 1",
                (now,),
            )
            connection.execute(
                "UPDATE collections SET is_hidable = 0, updated_at = ? WHERE is_hidable = 1",
                (now,),
            )

        self._database.execute(write, DatabasePriority.NORMAL)

    def list_hidden_book_ids(self) -> list[str]:
        return self._database.execute(
            lambda connection: [
                row["book_id"]
                for row in connection.execute(
                    "SELECT book_id FROM books WHERE is_hidden = 1"
                ).fetchall()
            ],
            DatabasePriority.HIGH,
        )

    def list_hidable_collection_ids(self) -> list[str]:
        return self._database.execute(
            lambda connection: [
                row["collection_id"]
                for row in connection.execute(
                    "SELECT collection_id FROM collections WHERE is_hidable = 1"
                ).fetchall()
            ],
            DatabasePriority.HIGH,
        )

    def delete_book(self, book_id: str) -> None:
        logger.info("delete_book %s", book_id)
        cleanup = self._database.execute(
            lambda connection: self._delete_book_records(connection, book_id),
            DatabasePriority.NORMAL,
        )
        self._delete_managed_artifacts(cleanup)

    def mark_recent(self, book_id: str) -> None:
        logger.debug("mark_recent book=%s", book_id)
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
        logger.debug("remove_book_from_recent book=%s", book_id)
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
        # Stored paths are storage-relative; resolve to absolute before any
        # filesystem check so the managed-root guard in ``_unlink_if_managed``
        # compares like-for-like.
        resolved_storage = _resolve_managed(self._resolver, row["storage_path"])
        resolved_cover = _resolve_managed(self._resolver, row["cover_path"])
        storage_path = Path(resolved_storage) if resolved_storage else None
        cover_path = Path(resolved_cover) if resolved_cover else None
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
        logger.info(
            "move_book_to_private book=%s private_id=%s collection=%s",
            book_id,
            private_book_id,
            private_collection_id,
        )
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


def _list_books(
    connection: sqlite3.Connection,
    book_id: str | None = None,
    *,
    resolver: StoragePathResolver | None = None,
) -> list[Book]:
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
            books.is_hidden,
            books.created_at,
            books.updated_at,
            book_files.file_id,
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
    return [_book_from_row(row, resolver=resolver) for row in rows]


def _refresh_book_file_states(
    connection: sqlite3.Connection,
    book_ids: tuple[str, ...] | None = None,
    *,
    resolver: StoragePathResolver | None = None,
) -> None:
    where = ""
    parameters: tuple[object, ...] = ()
    if book_ids:
        placeholders = ", ".join("?" for _book_id in book_ids)
        where = f"WHERE books.book_id IN ({placeholders})"
        parameters = tuple(book_ids)
    rows = connection.execute(
        f"""
        SELECT
            books.book_id,
            book_files.file_id,
            book_files.storage_path,
            book_files.state
        FROM books
        JOIN book_files ON book_files.file_id = books.file_id
        {where}
        """,
        parameters,
    ).fetchall()
    if not rows:
        return

    now = _now()
    updates: list[tuple[str, str, str]] = []
    missing_count = 0
    healthy_count = 0
    for row in rows:
        resolved = _resolve_managed(resolver, row["storage_path"])
        storage_path = Path(resolved).expanduser() if resolved else None
        desired_state = "healthy" if storage_path is not None and storage_path.exists() else "missing"
        if row["state"] == desired_state:
            continue
        updates.append((desired_state, now, row["file_id"]))
        if desired_state == "missing":
            missing_count += 1
        else:
            healthy_count += 1
    if updates:
        connection.executemany(
            "UPDATE book_files SET state = ?, updated_at = ? WHERE file_id = ?",
            updates,
        )
    if missing_count or healthy_count:
        logger.info(
            "Refreshed book file state: missing=%d healthy=%d",
            missing_count,
            healthy_count,
        )


def _list_collections(connection: sqlite3.Connection) -> list[Collection]:
    rows = connection.execute(
        """
        SELECT collection_id, name, created_at, updated_at, is_hidable
        FROM collections
        ORDER BY created_at DESC, name COLLATE NOCASE ASC
        """
    ).fetchall()
    return [
        Collection(
            uuid=row["collection_id"],
            name=row["name"],
            is_private=False,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            is_hidable=bool(row["is_hidable"]),
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


def _book_from_row(row: sqlite3.Row, *, resolver: StoragePathResolver | None = None) -> Book:
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
        file_path=_resolve_managed(resolver, row["storage_path"]),
        progress=max(0.0, min(100.0, progress_percent)) / 100.0,
        cover_thumbnail_path=_resolve_managed(resolver, row["cover_path"]),
        added_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        last_read_at=(
            datetime.fromisoformat(row["last_read_at"])
            if row["last_read_at"] is not None
            else None
        ),
        is_favourite=bool(row["is_favourite"]),
        is_missing=row["state"] == "missing",
        is_hidden=bool(row["is_hidden"]),
        collection_ids=collection_ids,
        page_count=0,
        original_file_name=_original_file_name_from_row(row),
        file_id=row["file_id"],
    )


def _export_record_from_row(
    row: sqlite3.Row, *, resolver: StoragePathResolver | None = None
) -> BookExportRecord:
    return BookExportRecord(
        book_uuid=row["book_id"],
        title=row["title"],
        storage_path=_resolve_managed(resolver, row["storage_path"]),
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
