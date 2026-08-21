"""``ImportService.import_paths`` — one gesture, one batch.

Drag-and-drop hands over whatever was selected in Finder, which may mix files
and folders. The point of this method is that such a drop stays *one* import:
one batch id and one result, so the user gets a single summary rather than one
dialog per thing they happened to have selected.
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


def _database(tmp_path: Path) -> DatabaseInterpreter:
    database = DatabaseInterpreter(tmp_path / "joyread.sqlite3")
    database.execute(apply_migrations, DatabasePriority.CRITICAL)
    return database


def _write_cbz(path: Path, color: str = "#336699") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = path.with_suffix(".png")
    Image.new("RGB", (10, 20), color).save(image, format="PNG")
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")
    image.unlink()
    return path


def _service(tmp_path: Path) -> ImportService:
    paths = PathService(storage_root=tmp_path / "storage", support_root=tmp_path / "support")
    paths.ensure_directories()
    return ImportService(paths, _database(tmp_path), ArchiveImageService(), HashService())


def test_files_import_as_one_batch(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = _write_cbz(tmp_path / "drop" / "a.cbz", "#111111")
    second = _write_cbz(tmp_path / "drop" / "b.cbz", "#222222")

    result = service.import_paths([first, second])

    assert result.imported_count == 2
    assert len(result.items) == 2


def test_a_folder_is_expanded_to_the_books_inside_it(tmp_path: Path) -> None:
    service = _service(tmp_path)
    folder = tmp_path / "Series"
    _write_cbz(folder / "01.cbz", "#111111")
    _write_cbz(folder / "02.cbz", "#222222")

    result = service.import_paths([folder])

    assert result.imported_count == 2


def test_a_mixed_drop_yields_exactly_one_result(tmp_path: Path) -> None:
    """The whole reason this method exists: importing the folder and the loose
    file separately would report two outcomes for one drop."""

    service = _service(tmp_path)
    folder = tmp_path / "Series"
    _write_cbz(folder / "01.cbz", "#111111")
    loose = _write_cbz(tmp_path / "drop" / "loose.cbz", "#222222")

    result = service.import_paths([folder, loose])

    assert result.imported_count == 2
    assert len({item.source_path for item in result.items}) == 2
    # One batch id is what makes it one entry in the audit trail.
    assert result.batch_id


def test_a_file_also_reachable_through_a_dropped_folder_imports_once(tmp_path: Path) -> None:
    """Finder lets you select a folder and something inside it. Importing that
    file twice would report a spurious duplicate against the user's own drop."""

    service = _service(tmp_path)
    folder = tmp_path / "Series"
    inner = _write_cbz(folder / "01.cbz", "#111111")

    result = service.import_paths([folder, inner])

    assert result.imported_count == 1
    assert result.duplicate_count == 0


def test_folder_depth_is_honoured(tmp_path: Path) -> None:
    service = _service(tmp_path)
    folder = tmp_path / "Series"
    _write_cbz(folder / "01.cbz", "#111111")
    _write_cbz(folder / "Extras" / "02.cbz", "#222222")

    shallow = service.import_paths([folder], max_depth=1)

    assert shallow.imported_count == 1


def test_an_empty_drop_is_an_empty_batch(tmp_path: Path) -> None:
    service = _service(tmp_path)

    result = service.import_paths([])

    assert result.imported_count == 0
    assert result.items == ()


def test_a_symlinked_folder_and_the_real_file_import_once(tmp_path: Path) -> None:
    """Dedupe has to resolve, not just normcase.

    Finder will hand over a symlinked folder alongside a file reached by its
    real path. Comparing the raw strings imports that file twice and reports a
    duplicate against the user's own single drop.
    """

    service = _service(tmp_path)
    real = tmp_path / "real"
    inner = _write_cbz(real / "01.cbz", "#111111")
    link = tmp_path / "link"
    link.symlink_to(real)

    result = service.import_paths([link, inner])

    assert result.imported_count == 1
    assert result.duplicate_count == 0


def test_unsupported_files_are_skipped_not_failed(tmp_path: Path) -> None:
    """`import_folder` filters non-books silently via the folder walk.

    Without the same rule on directly-passed paths, the two entry points on
    this service disagree: a stray .txt would be reported as a failed import
    here and skipped there, for the same file in the same batch.
    """

    service = _service(tmp_path)
    book = _write_cbz(tmp_path / "drop" / "a.cbz")
    notes = tmp_path / "drop" / "notes.txt"
    notes.write_text("not a book")

    result = service.import_paths([book, notes])

    assert result.imported_count == 1
    assert result.failed_count == 0
    assert len(result.items) == 1
