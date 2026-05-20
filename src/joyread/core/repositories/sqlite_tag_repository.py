"""SQLite-backed JoyRead tag repository."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from uuid import uuid4

from joyread.core.models.tag import Tag, normalize_tag_name
from joyread.core.repositories.tag_repository import (
    TagNameConflictError,
    TagNotFoundError,
    TagRepository,
)
from joyread.infrastructure.database.database_interpreter import DatabaseInterpreter, DatabasePriority


logger = logging.getLogger(__name__)


class SqliteTagRepository(TagRepository):
    def __init__(self, database: DatabaseInterpreter) -> None:
        self._database = database

    def list_tags(self) -> list[Tag]:
        return self._database.execute(_list_tags, DatabasePriority.HIGH)

    def get_tag(self, tag_id: str) -> Tag | None:
        return self._database.execute(
            lambda connection: _get_tag(connection, tag_id),
            DatabasePriority.HIGH,
        )

    def get_tag_by_normalized(self, normalized: str) -> Tag | None:
        key = normalized.strip().casefold()
        return self._database.execute(
            lambda connection: _get_tag_by_normalized(connection, key),
            DatabasePriority.HIGH,
        )

    def create(self, display_name: str) -> Tag:
        normalized_display = normalize_tag_name(display_name)
        normalized_key = normalized_display.casefold()

        def write(connection: sqlite3.Connection) -> Tag:
            existing = _get_tag_by_normalized(connection, normalized_key)
            if existing is not None:
                raise TagNameConflictError(
                    f"A tag named '{existing.name}' already exists."
                )
            tag_id = str(uuid4())
            now = _now()
            connection.execute(
                """
                INSERT INTO tags(tag_id, name, name_normalized, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (tag_id, normalized_display, normalized_key, now, now),
            )
            logger.info("Tag created id=%s name=%r", tag_id, normalized_display)
            return Tag(tag_id=tag_id, name=normalized_display)

        return self._database.execute(write, DatabasePriority.NORMAL)

    def find_or_create(self, display_name: str) -> Tag:
        normalized_display = normalize_tag_name(display_name)
        normalized_key = normalized_display.casefold()

        def write(connection: sqlite3.Connection) -> Tag:
            existing = _get_tag_by_normalized(connection, normalized_key)
            if existing is not None:
                return existing
            tag_id = str(uuid4())
            now = _now()
            connection.execute(
                """
                INSERT INTO tags(tag_id, name, name_normalized, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (tag_id, normalized_display, normalized_key, now, now),
            )
            logger.info("Tag find_or_create created id=%s name=%r", tag_id, normalized_display)
            return Tag(tag_id=tag_id, name=normalized_display)

        return self._database.execute(write, DatabasePriority.NORMAL)

    def rename(self, tag_id: str, new_display_name: str) -> Tag:
        normalized_display = normalize_tag_name(new_display_name)
        normalized_key = normalized_display.casefold()

        def write(connection: sqlite3.Connection) -> Tag:
            current = _get_tag(connection, tag_id)
            if current is None:
                raise TagNotFoundError(f"Tag {tag_id} does not exist.")
            if current.name_normalized == normalized_key:
                if current.name != normalized_display:
                    # Same normalized key but different display form: update
                    # the display in place so the user sees their chosen
                    # casing immediately.
                    now = _now()
                    connection.execute(
                        "UPDATE tags SET name = ?, updated_at = ? WHERE tag_id = ?",
                        (normalized_display, now, tag_id),
                    )
                    return Tag(tag_id=tag_id, name=normalized_display)
                return current
            collision = _get_tag_by_normalized(connection, normalized_key)
            if collision is not None:
                raise TagNameConflictError(
                    f"A tag named '{collision.name}' already exists."
                )
            now = _now()
            connection.execute(
                """
                UPDATE tags
                SET name = ?, name_normalized = ?, updated_at = ?
                WHERE tag_id = ?
                """,
                (normalized_display, normalized_key, now, tag_id),
            )
            logger.info(
                "Tag renamed id=%s old=%r new=%r",
                tag_id,
                current.name,
                normalized_display,
            )
            return Tag(tag_id=tag_id, name=normalized_display)

        return self._database.execute(write, DatabasePriority.NORMAL)

    def delete(self, tag_id: str) -> int:
        def write(connection: sqlite3.Connection) -> int:
            tag = _get_tag(connection, tag_id)
            if tag is None:
                logger.warning("Tag delete: id=%s not found, no-op", tag_id)
                return 0
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM book_tags WHERE tag_id = ?",
                (tag_id,),
            ).fetchone()
            linked_count = int(row["count"]) if row is not None else 0
            connection.execute("DELETE FROM tags WHERE tag_id = ?", (tag_id,))
            logger.info(
                "Tag deleted id=%s name=%r unlinked_books=%d",
                tag_id,
                tag.name,
                linked_count,
            )
            return linked_count

        return self._database.execute(write, DatabasePriority.NORMAL)

    def link_book(self, tag_id: str, book_id: str) -> None:
        def write(connection: sqlite3.Connection) -> None:
            now = _now()
            connection.execute(
                """
                INSERT INTO book_tags(tag_id, book_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(tag_id, book_id) DO NOTHING
                """,
                (tag_id, book_id, now),
            )

        self._database.execute(write, DatabasePriority.NORMAL)

    def unlink_book(self, tag_id: str, book_id: str) -> None:
        self._database.execute(
            lambda connection: connection.execute(
                "DELETE FROM book_tags WHERE tag_id = ? AND book_id = ?",
                (tag_id, book_id),
            ),
            DatabasePriority.NORMAL,
        )

    def set_book_tag_ids(self, book_id: str, tag_ids: tuple[str, ...]) -> None:
        normalized_ids = tuple(dict.fromkeys(tag_id for tag_id in tag_ids if tag_id))

        def write(connection: sqlite3.Connection) -> None:
            now = _now()
            connection.execute("BEGIN")
            try:
                connection.execute("DELETE FROM book_tags WHERE book_id = ?", (book_id,))
                connection.executemany(
                    """
                    INSERT INTO book_tags(tag_id, book_id, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(tag_id, book_id) DO NOTHING
                    """,
                    ((tag_id, book_id, now) for tag_id in normalized_ids),
                )
            except Exception:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

        self._database.execute(write, DatabasePriority.NORMAL)

    def list_tag_ids_for_book(self, book_id: str) -> list[str]:
        return self._database.execute(
            lambda connection: [
                row["tag_id"]
                for row in connection.execute(
                    """
                    SELECT book_tags.tag_id
                    FROM book_tags
                    JOIN tags ON tags.tag_id = book_tags.tag_id
                    WHERE book_tags.book_id = ?
                    ORDER BY tags.name_normalized COLLATE NOCASE ASC
                    """,
                    (book_id,),
                ).fetchall()
            ],
            DatabasePriority.HIGH,
        )

    def list_tag_ids_for_books(self, book_ids: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
        normalized_ids = tuple(dict.fromkeys(book_id for book_id in book_ids if book_id))
        if not normalized_ids:
            return {}

        def read(connection: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
            placeholders = ", ".join("?" for _book_id in normalized_ids)
            result: dict[str, list[str]] = {book_id: [] for book_id in normalized_ids}
            rows = connection.execute(
                f"""
                SELECT book_tags.book_id, book_tags.tag_id
                FROM book_tags
                JOIN tags ON tags.tag_id = book_tags.tag_id
                WHERE book_tags.book_id IN ({placeholders})
                ORDER BY book_tags.book_id ASC, tags.name_normalized COLLATE NOCASE ASC
                """,
                normalized_ids,
            ).fetchall()
            for row in rows:
                result.setdefault(row["book_id"], []).append(row["tag_id"])
            return {book_id: tuple(tag_ids) for book_id, tag_ids in result.items()}

        return self._database.execute(read, DatabasePriority.HIGH)


def _list_tags(connection: sqlite3.Connection) -> list[Tag]:
    rows = connection.execute(
        """
        SELECT tag_id, name
        FROM tags
        ORDER BY name_normalized COLLATE NOCASE ASC
        """
    ).fetchall()
    return [Tag(tag_id=row["tag_id"], name=row["name"]) for row in rows]


def _get_tag(connection: sqlite3.Connection, tag_id: str) -> Tag | None:
    row = connection.execute(
        "SELECT tag_id, name FROM tags WHERE tag_id = ?",
        (tag_id,),
    ).fetchone()
    if row is None:
        return None
    return Tag(tag_id=row["tag_id"], name=row["name"])


def _get_tag_by_normalized(connection: sqlite3.Connection, normalized: str) -> Tag | None:
    row = connection.execute(
        "SELECT tag_id, name FROM tags WHERE name_normalized = ?",
        (normalized,),
    ).fetchone()
    if row is None:
        return None
    return Tag(tag_id=row["tag_id"], name=row["name"])


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
