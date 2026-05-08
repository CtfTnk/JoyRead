from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image

from joyread.core.archive import ArchiveImageService
from joyread.core.repositories.sqlite_book_repository import SqliteBookRepository
from joyread.core.services.export_service import ExportService
from joyread.core.services.hash_service import HashService
from joyread.core.services.import_service import ImportService
from joyread.infrastructure.database import DatabaseInterpreter, DatabasePriority, apply_migrations
from joyread.infrastructure.filesystem.path_service import PathService


def _database(tmp_path: Path) -> DatabaseInterpreter:
    database = DatabaseInterpreter(tmp_path / "joyread.sqlite3")
    database.execute(apply_migrations, DatabasePriority.CRITICAL)
    return database


def _write_cbz(path: Path, color: str = "#336699") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = path.with_suffix(".png")
    Image.new("RGB", (10, 20), color).save(image, format="PNG")
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")


def _import_service(tmp_path: Path) -> tuple[ImportService, DatabaseInterpreter, PathService]:
    paths = PathService(storage_root=tmp_path / "storage", support_root=tmp_path / "support")
    paths.ensure_directories()
    database = _database(paths.paths.database)
    service = ImportService(paths, database, ArchiveImageService(), HashService())
    return service, database, paths


def test_export_service_writes_original_filename_and_extension(tmp_path: Path) -> None:
    source = tmp_path / "source" / "Volume 01.cbz"
    _write_cbz(source)
    import_service, database, _paths = _import_service(tmp_path)
    import_service.import_files([source])
    repository = SqliteBookRepository(database)
    book = repository.list_books()[0]
    export_dir = tmp_path / "export"
    export_dir.mkdir()

    result = ExportService(repository, HashService()).export_books((book.uuid,), export_dir)

    exported_path = export_dir / "Volume 01.cbz"
    assert result.exported_count == 1
    assert result.failed_count == 0
    assert exported_path.read_bytes() == Path(book.file_path).read_bytes()
    assert source.exists()
    database.close()


def test_export_service_auto_renames_duplicate_original_names(tmp_path: Path) -> None:
    first_source = tmp_path / "a" / "Same.cbz"
    second_source = tmp_path / "b" / "Same.cbz"
    _write_cbz(first_source, color="#113355")
    _write_cbz(second_source, color="#553311")
    import_service, database, _paths = _import_service(tmp_path)
    import_result = import_service.import_files([first_source, second_source])
    repository = SqliteBookRepository(database)
    book_ids = tuple(item.book_id for item in import_result.items if item.book_id is not None)
    export_dir = tmp_path / "export"
    export_dir.mkdir()

    result = ExportService(repository, HashService()).export_books(book_ids, export_dir)

    assert result.exported_count == 2
    assert result.failed_count == 0
    assert sorted(path.name for path in export_dir.iterdir()) == ["Same (1).cbz", "Same.cbz"]
    database.close()


def test_export_service_fails_missing_stored_files_without_output(tmp_path: Path) -> None:
    source = tmp_path / "missing.cbz"
    _write_cbz(source)
    import_service, database, _paths = _import_service(tmp_path)
    import_service.import_files([source])
    repository = SqliteBookRepository(database)
    book = repository.list_books()[0]
    Path(book.file_path).unlink()
    export_dir = tmp_path / "export"
    export_dir.mkdir()

    result = ExportService(repository, HashService()).export_books((book.uuid,), export_dir)

    assert result.exported_count == 0
    assert result.failed_count == 1
    assert list(export_dir.iterdir()) == []
    database.close()


def test_export_service_fails_hash_mismatches_without_output(tmp_path: Path) -> None:
    source = tmp_path / "changed.cbz"
    _write_cbz(source)
    import_service, database, _paths = _import_service(tmp_path)
    import_service.import_files([source])
    repository = SqliteBookRepository(database)
    book = repository.list_books()[0]
    Path(book.file_path).write_bytes(b"changed bytes")
    export_dir = tmp_path / "export"
    export_dir.mkdir()

    result = ExportService(repository, HashService()).export_books((book.uuid,), export_dir)

    assert result.exported_count == 0
    assert result.failed_count == 1
    assert list(export_dir.iterdir()) == []
    database.close()


def test_export_service_sanitizes_original_filename_to_basename(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.cbz"
    _write_cbz(source)
    import_service, database, _paths = _import_service(tmp_path)
    import_service.import_files([source])
    repository = SqliteBookRepository(database)
    book = repository.list_books()[0]
    database.execute(
        lambda connection: connection.execute(
            "UPDATE book_files SET original_file_name = ?",
            ("..\\Outside.cbz",),
        )
    )
    export_dir = tmp_path / "export"
    export_dir.mkdir()

    result = ExportService(repository, HashService()).export_books((book.uuid,), export_dir)

    assert result.exported_count == 1
    assert (export_dir / "Outside.cbz").exists()
    assert not (export_dir.parent / "Outside.cbz").exists()
    database.close()
