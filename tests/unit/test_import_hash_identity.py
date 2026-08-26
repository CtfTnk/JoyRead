"""What an import records as the source's identity versus the stored artifact.

Migration 13 split one ``content_hash`` column into ``source_hash`` (what the
user handed over) and ``stored_hash`` (what is on disk). Today every import is
byte-verbatim, so the two values are equal and no ordinary import can tell them
apart -- which is exactly why these tests exist: they pin the semantics *now*,
while the code that will make the two diverge is still ahead of us.
"""

from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image

from joyread.core.archive import ArchiveImageService
from joyread.core.services.hash_service import HashService
from joyread.core.services.import_service import ImportService
from joyread.infrastructure.database import DatabaseInterpreter, DatabasePriority, apply_migrations
from joyread.infrastructure.filesystem.path_service import PathService


def _write_cbz(path: Path, *, color: str = "#336699") -> None:
    image = path.with_suffix(".png")
    Image.new("RGB", (10, 20), color).save(image, format="PNG")
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")


def _make_service(tmp_path: Path) -> tuple[ImportService, DatabaseInterpreter]:
    paths = PathService(storage_root=tmp_path / "storage", support_root=tmp_path / "support")
    paths.ensure_directories()
    database = DatabaseInterpreter(paths.paths.database / "joyread.sqlite3")
    database.execute(apply_migrations, DatabasePriority.CRITICAL)
    service = ImportService(paths, database, ArchiveImageService(), HashService())
    return service, database


def test_a_verbatim_import_records_both_hashes_and_names_the_writer(tmp_path: Path) -> None:
    """A plain copy is the source's identity *and* the artifact's baseline.

    ``storage_kind`` has to say so explicitly: a later reader must not have to
    infer from the suffix whether the bytes on disk are the user's own file.
    """

    service, database = _make_service(tmp_path)
    try:
        source = tmp_path / "book.cbz"
        _write_cbz(source)

        result = service.import_files([source])
        assert result.failed_count == 0

        row = database.execute(
            lambda connection: connection.execute(
                "SELECT source_hash, stored_hash, storage_kind FROM book_files"
            ).fetchone(),
            DatabasePriority.CRITICAL,
        )

        assert row["storage_kind"] == "verbatim"
        assert row["source_hash"] == row["stored_hash"]
        assert row["source_hash"] == HashService().compute(source, "sha256")
    finally:
        database.close()


def test_duplicate_detection_matches_the_source_not_the_stored_artifact(tmp_path: Path) -> None:
    """The distinguishing case, built by hand because no writer produces it yet.

    A row whose two hashes differ is what canonical import will write. Importing
    the file that row came *from* must be recognised as a duplicate -- matching
    on the stored artifact instead would re-import it, since a repackaged CBZ
    does not hash like the archive it was built from.
    """

    service, database = _make_service(tmp_path)
    try:
        source = tmp_path / "already-imported.cbz"
        _write_cbz(source)
        source_hash = HashService().compute(source, "sha256")

        def seed(connection) -> None:  # noqa: ANN001 - sqlite connection, evident from caller.
            connection.execute(
                """
                INSERT INTO book_files(
                    file_id, original_path, original_file_name, storage_path, file_format,
                    hash_algorithm, source_hash, stored_hash, storage_kind, state,
                    created_at, updated_at
                ) VALUES ('file-1', ?, 'already-imported.cbz', 'Books/aa/repacked.cbz', 'CBZ',
                          'sha256', ?, 'a-hash-no-source-predicts', 'canonical', 'healthy',
                          '2026-01-01T00:00:00', '2026-01-01T00:00:00')
                """,
                (str(source), source_hash),
            )
            connection.execute(
                """
                INSERT INTO books(
                    book_id, file_id, title, author, language_tag, book_type,
                    cover_path, is_favourite, created_at, updated_at
                ) VALUES ('book-1', 'file-1', 'Already Imported', 'Unknown', 'und', 'manga',
                          NULL, 0, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
                """
            )

        database.execute(seed, DatabasePriority.CRITICAL)

        result = service.import_files([source])

        assert [item.status for item in result.items] == ["duplicate"]
        assert result.items[0].book_id == "book-1"
        file_count = database.execute(
            lambda connection: connection.execute(
                "SELECT COUNT(*) AS n FROM book_files"
            ).fetchone()["n"],
            DatabasePriority.CRITICAL,
        )
        assert file_count == 1
    finally:
        database.close()
