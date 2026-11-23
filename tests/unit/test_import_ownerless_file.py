"""Import of a file whose ``book_files`` row no book owns.

Ordinary import and deletion never leave such a row, so these tests seed one by
hand, the way a legacy defect or a hand-edited database would. Before the fix,
import matched the row as a duplicate and reported "Book already exists" with
no book, so the file could never be imported again.
"""

from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image
import pytest

from joyread.core.archive import ArchiveImageService
from joyread.core.services.hash_service import HashService
from joyread.core.services.import_service import ImportService
from joyread.infrastructure.database import DatabaseInterpreter, DatabasePriority, apply_migrations
from joyread.infrastructure.filesystem.path_service import PathService

_STAMP = "2026-01-01T00:00:00"


def _write_cbz(path: Path) -> None:
    image = path.with_suffix(".png")
    Image.new("RGB", (10, 20), "#336699").save(image, format="PNG")
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")


def _make_service(tmp_path: Path) -> tuple[ImportService, DatabaseInterpreter, PathService]:
    paths = PathService(storage_root=tmp_path / "storage", support_root=tmp_path / "support")
    paths.ensure_directories()
    database = DatabaseInterpreter(paths.paths.database / "joyread.sqlite3")
    database.execute(apply_migrations, DatabasePriority.CRITICAL)
    return ImportService(paths, database, ArchiveImageService(), HashService()), database, paths


def _seed_file_row(
    database: DatabaseInterpreter,
    source: Path,
    *,
    file_id: str,
    storage_path: str,
) -> None:
    source_hash = HashService().compute(source, "sha256")
    database.execute(
        lambda connection: connection.execute(
            """
            INSERT INTO book_files(
                file_id, original_path, original_file_name, storage_path, file_format,
                hash_algorithm, source_hash, stored_hash, storage_kind, state,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'CBZ', 'sha256', ?, ?, 'canonical', 'healthy', ?, ?)
            """,
            (file_id, str(source), source.name, storage_path, source_hash, source_hash, _STAMP, _STAMP),
        ),
        DatabasePriority.CRITICAL,
    )


def _query(database: DatabaseInterpreter, sql: str) -> list[tuple]:
    return database.execute(
        lambda connection: [tuple(row) for row in connection.execute(sql).fetchall()],
        DatabasePriority.CRITICAL,
    )


@pytest.mark.parametrize("verify_integrity", [True, False])
def test_an_ownerless_file_row_is_reclaimed_and_the_file_imports_as_new(
    tmp_path: Path,
    verify_integrity: bool,
) -> None:
    """Both integrity modes, because each looks for duplicates at a different step."""

    service, database, paths = _make_service(tmp_path)
    try:
        service.set_verify_imported_file_integrity(verify_integrity)
        source = tmp_path / "lost.cbz"
        _write_cbz(source)
        stale_file = paths.paths.books / "zz" / "orphan.cbz"
        stale_file.parent.mkdir(parents=True)
        stale_file.write_bytes(source.read_bytes())
        _seed_file_row(database, source, file_id="orphan-file", storage_path="Books/zz/orphan.cbz")

        result = service.import_files([source])

        assert [item.status for item in result.items] == ["imported"]
        imported = result.items[0]
        assert imported.book_id is not None
        assert imported.file_id != "orphan-file"
        assert _query(database, "SELECT file_id FROM book_files") == [(imported.file_id,)]
        assert _query(database, "SELECT book_id, file_id FROM books") == [
            (imported.book_id, imported.file_id)
        ]
        # The stale row's file is reclaimed instead of lingering unreferenced.
        assert not stale_file.exists()
    finally:
        database.close()


def test_a_legacy_private_book_still_owns_its_file(tmp_path: Path) -> None:
    """A ``private_books`` row is an owner, so its file stays a duplicate."""

    service, database, paths = _make_service(tmp_path)
    try:
        source = tmp_path / "private.cbz"
        _write_cbz(source)
        stored_file = paths.paths.books / "pp" / "private.cbz"
        stored_file.parent.mkdir(parents=True)
        stored_file.write_bytes(source.read_bytes())
        _seed_file_row(database, source, file_id="private-file", storage_path="Books/pp/private.cbz")
        database.execute(
            lambda connection: connection.execute(
                """
                INSERT INTO private_books(
                    private_book_id, file_id, title, author, book_type, created_at, updated_at
                ) VALUES ('private-1', 'private-file', 'Private', 'Unknown', 'manga', ?, ?)
                """,
                (_STAMP, _STAMP),
            ),
            DatabasePriority.CRITICAL,
        )

        result = service.import_files([source])

        assert [item.status for item in result.items] == ["duplicate"]
        assert _query(database, "SELECT file_id FROM book_files") == [("private-file",)]
        assert _query(database, "SELECT COUNT(*) FROM books") == [(0,)]
        assert stored_file.exists()
    finally:
        database.close()


def test_a_reclaimed_row_never_deletes_a_file_outside_the_library(tmp_path: Path) -> None:
    """A stale row may hold a path from an old layout; that file is the user's."""

    service, database, _paths = _make_service(tmp_path)
    try:
        source = tmp_path / "outside.cbz"
        _write_cbz(source)
        outside = tmp_path / "storage" / "not-books.cbz"
        outside.write_bytes(source.read_bytes())
        _seed_file_row(database, source, file_id="orphan-file", storage_path="not-books.cbz")

        result = service.import_files([source])

        assert [item.status for item in result.items] == ["imported"]
        assert outside.exists()
    finally:
        database.close()
