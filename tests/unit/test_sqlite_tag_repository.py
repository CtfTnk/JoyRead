"""Tests for the SQLite-backed tag repository and migration v10."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from joyread.core.repositories.sqlite_tag_repository import SqliteTagRepository
from joyread.core.repositories.tag_repository import TagNameConflictError, TagNotFoundError
from joyread.infrastructure.database import DatabaseInterpreter, DatabasePriority, apply_migrations


def _database(tmp_path: Path) -> DatabaseInterpreter:
    database = DatabaseInterpreter(tmp_path / "joyread.sqlite3")
    database.execute(apply_migrations, DatabasePriority.CRITICAL)
    return database


def _insert_book(database: DatabaseInterpreter, book_id: str, *, file_id: str | None = None) -> str:
    file_id = file_id or f"file-{book_id}"
    now = datetime.now().isoformat(timespec="seconds")

    def write(connection) -> None:
        connection.execute(
            """
            INSERT INTO book_files(
                file_id, original_path, original_file_name, storage_path, file_format,
                file_size, mtime_ns, hash_algorithm, content_hash, state,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'CBZ', 0, 0, 'sha256', ?, 'healthy', ?, ?)
            """,
            (file_id, f"/tmp/{book_id}.cbz", f"{book_id}.cbz", f"/storage/{book_id}.cbz", book_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO books(
                book_id, file_id, title, author, language_tag, book_type, cover_path,
                is_favourite, created_at, updated_at
            )
            VALUES (?, ?, ?, 'Unknown', 'und', 'manga', NULL, 0, ?, ?)
            """,
            (book_id, file_id, book_id, now, now),
        )

    database.execute(write, DatabasePriority.NORMAL)
    return book_id


def test_migration_v10_creates_tags_and_book_tags_tables(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        rows = database.execute(
            lambda connection: [
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('tags', 'book_tags')"
                ).fetchall()
            ],
            DatabasePriority.HIGH,
        )
    finally:
        database.close()

    assert set(rows) == {"tags", "book_tags"}


def test_create_and_list_tags_sorts_alphabetically_case_insensitive(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        repo = SqliteTagRepository(database)
        repo.create("comedy")
        repo.create("Action")
        repo.create("drama")
        tags = repo.list_tags()
    finally:
        database.close()

    assert [tag.name for tag in tags] == ["Action", "Comedy", "Drama"]


def test_create_rejects_case_insensitive_duplicate(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        repo = SqliteTagRepository(database)
        repo.create("Action")
        with pytest.raises(TagNameConflictError):
            repo.create("ACTION")
    finally:
        database.close()


def test_find_or_create_is_idempotent(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        repo = SqliteTagRepository(database)
        first = repo.find_or_create("Comedy")
        again = repo.find_or_create("comedy")
        all_tags = repo.list_tags()
    finally:
        database.close()

    assert first.tag_id == again.tag_id
    assert len(all_tags) == 1


def test_rename_updates_display_and_normalized(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        repo = SqliteTagRepository(database)
        tag = repo.create("Action")
        renamed = repo.rename(tag.tag_id, "drama")
        listed = repo.list_tags()
    finally:
        database.close()

    assert renamed.name == "Drama"
    assert [tag.name for tag in listed] == ["Drama"]


def test_rename_rejects_collision(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        repo = SqliteTagRepository(database)
        repo.create("Comedy")
        action = repo.create("Action")
        with pytest.raises(TagNameConflictError):
            repo.rename(action.tag_id, "comedy")
    finally:
        database.close()


def test_rename_unknown_id_raises(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        repo = SqliteTagRepository(database)
        with pytest.raises(TagNotFoundError):
            repo.rename("does-not-exist", "Comedy")
    finally:
        database.close()


def test_delete_cascades_unlink_books(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        repo = SqliteTagRepository(database)
        tag = repo.create("Comedy")
        _insert_book(database, "book-1")
        _insert_book(database, "book-2")
        repo.link_book(tag.tag_id, "book-1")
        repo.link_book(tag.tag_id, "book-2")

        unlinked = repo.delete(tag.tag_id)

        remaining_book_tags = database.execute(
            lambda connection: connection.execute("SELECT COUNT(*) AS count FROM book_tags").fetchone()["count"],
            DatabasePriority.HIGH,
        )
    finally:
        database.close()

    assert unlinked == 2
    assert remaining_book_tags == 0
    # Books themselves are not deleted.
    database = _database(tmp_path)
    try:
        books = database.execute(
            lambda connection: connection.execute("SELECT COUNT(*) AS count FROM books").fetchone()["count"],
            DatabasePriority.HIGH,
        )
    finally:
        database.close()
    assert books == 2


def test_delete_absent_tag_is_noop_returns_zero(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        repo = SqliteTagRepository(database)
        result = repo.delete("missing-tag-id")
    finally:
        database.close()

    assert result == 0


def test_link_and_list_tag_ids_for_book(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        repo = SqliteTagRepository(database)
        action = repo.create("Action")
        comedy = repo.create("Comedy")
        drama = repo.create("Drama")
        _insert_book(database, "book-1")
        repo.link_book(action.tag_id, "book-1")
        repo.link_book(comedy.tag_id, "book-1")
        repo.link_book(action.tag_id, "book-1")  # idempotent
        # Drama is intentionally not linked.
        _ = drama
        ids = repo.list_tag_ids_for_book("book-1")
    finally:
        database.close()

    # Ordered by normalized name.
    assert ids == [action.tag_id, comedy.tag_id]


def test_unlink_removes_only_one_join(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        repo = SqliteTagRepository(database)
        tag = repo.create("Action")
        _insert_book(database, "book-1")
        _insert_book(database, "book-2")
        repo.link_book(tag.tag_id, "book-1")
        repo.link_book(tag.tag_id, "book-2")
        repo.unlink_book(tag.tag_id, "book-1")
        rows = database.execute(
            lambda connection: [
                row["book_id"]
                for row in connection.execute("SELECT book_id FROM book_tags").fetchall()
            ],
            DatabasePriority.HIGH,
        )
    finally:
        database.close()

    assert rows == ["book-2"]


def test_book_delete_cascades_book_tags_rows(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        repo = SqliteTagRepository(database)
        tag = repo.create("Comedy")
        _insert_book(database, "book-1")
        repo.link_book(tag.tag_id, "book-1")

        database.execute(
            lambda connection: connection.execute("DELETE FROM books WHERE book_id = ?", ("book-1",)),
            DatabasePriority.NORMAL,
        )
        rows = database.execute(
            lambda connection: connection.execute("SELECT COUNT(*) AS count FROM book_tags").fetchone()["count"],
            DatabasePriority.HIGH,
        )
    finally:
        database.close()

    assert rows == 0
