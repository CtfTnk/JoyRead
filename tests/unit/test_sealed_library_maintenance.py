"""Library maintenance, export and delete around Hidden Space books."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4
import zipfile

import pytest

from joyread.core.services.export_service import ExportService
from joyread.core.services.library_maintenance_service import LibraryAuditAction
from joyread.core.vault import ContentType, seal_bytes
from tests.support.vault_library import (
    VaultLibrary,
    build_vault_library,
    png_bytes,
    sample_pages,
    write_cbz,
)


class _Crash(BaseException):
    pass


@pytest.fixture
def library(tmp_path: Path):
    lib = build_vault_library(tmp_path)
    yield lib
    lib.close()


def _sealed_book(library: VaultLibrary, tmp_path: Path, *, with_cover: bool = True) -> tuple[str, list[bytes]]:
    book = library.import_file(write_cbz(tmp_path / "incoming" / "Night Notes.cbz", sample_pages(2)))
    pages = library.read_pages(book)
    if with_cover:
        cover = library.paths.paths.thumbnails / "covers" / f"{book}-generated-200x284.png"
        cover.parent.mkdir(parents=True, exist_ok=True)
        cover.write_bytes(png_bytes())
    if not library.vault.has_vault:
        library.create_vault()
    library.sealing.hide_books([book])
    library.sealing.run_pending()
    assert library.file_row(book)["storage_kind"] == "sealed"
    return book, pages


def test_the_audit_checks_a_sealed_book_while_locked_and_finds_nothing_to_clean(
    library: VaultLibrary, tmp_path: Path
) -> None:
    book, _pages = _sealed_book(library, tmp_path)
    library.vault.lock()

    plan = library.maintenance.scan()

    item = next(item for item in plan.items if book in item.book_ids)
    assert item.action is LibraryAuditAction.HEALTHY
    assert item.storage_kind == "sealed"
    assert plan.orphan_files == () and plan.orphan_cache_files == ()


def test_a_sealed_file_for_another_vault_is_unavailable(library: VaultLibrary, tmp_path: Path) -> None:
    book, _pages = _sealed_book(library, tmp_path)
    stored = library.stored_path(book)
    library.vault_repository.delete_vault(library.vault.vault.vault_id)  # The key pair is gone.

    plan = library.maintenance.scan()

    item = next(item for item in plan.items if book in item.book_ids)
    assert (item.action, item.probe_code) == (LibraryAuditAction.UNAVAILABLE, "sealed_key_unknown")
    assert stored.is_file()


def test_files_a_job_still_owns_are_never_orphans(
    library: VaultLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = library.import_file(write_cbz(tmp_path / "incoming" / "Book.cbz", sample_pages(2)))
    cover = library.paths.paths.thumbnails / "covers" / f"{book}-generated-200x284.png"
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(png_bytes())
    library.create_vault()
    library.sealing.hide_books([book])

    def crash(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise _Crash()

    monkeypatch.setattr(library.sealing_repository, "commit_seal", crash)
    with pytest.raises(_Crash):
        library.sealing.run_pending()
    monkeypatch.undo()

    plan = library.maintenance.scan()
    report = library.maintenance.apply(plan)

    assert plan.orphan_files == () and plan.orphan_cache_files == ()
    assert report.cleaned_file_count == 0
    library.sealing.run_pending()
    assert library.file_row(book)["storage_kind"] == "sealed"
    assert library.cover_store.path_for(book).is_file()


def test_sealed_covers_nothing_uses_are_cleaned_up(library: VaultLibrary, tmp_path: Path) -> None:
    book, _pages = _sealed_book(library, tmp_path)
    covers = library.paths.paths.thumbnails / "covers"
    stray = covers / f"{uuid4()}.jrl"
    stray.write_bytes(seal_bytes(png_bytes(), library.vault.public_key, ContentType.PNG))
    torn = covers / f"{book}.jrl.partial"
    torn.write_bytes(b"half a cover")

    plan = library.maintenance.scan()

    assert {orphan.path for orphan in plan.orphan_cache_files} == {stray, torn}
    library.maintenance.apply(plan)
    assert not stray.exists() and not torn.exists()
    assert library.cover_store.path_for(book).is_file()


def test_export_decrypts_a_sealed_book_only_while_unlocked(library: VaultLibrary, tmp_path: Path) -> None:
    book, pages = _sealed_book(library, tmp_path)
    exporter = ExportService(library.books, library.hash_service, vault_keyring=library.keyring)
    destination = tmp_path / "exported"
    destination.mkdir()

    library.vault.lock()
    locked = exporter.export_books((book,), destination)
    assert locked.items[0].status == "failed"
    assert locked.items[0].message == "Hidden Space is locked."
    assert list(destination.iterdir()) == []

    library.vault.unlock("1234")
    result = exporter.export_books((book,), destination)

    exported = Path(result.items[0].destination_path)
    assert result.items[0].status == "exported"
    assert exported.name == "Night Notes.cbz"
    with zipfile.ZipFile(exported) as archive:
        assert [archive.read(name) for name in archive.namelist() if name.endswith(".png")] == pages
    assert sorted(path.name for path in destination.iterdir()) == ["Night Notes.cbz"]


def test_deleting_a_sealed_book_removes_its_file_and_cover(library: VaultLibrary, tmp_path: Path) -> None:
    book, _pages = _sealed_book(library, tmp_path)
    sealed = library.stored_path(book)
    sealed_cover = library.cover_store.path_for(book)

    library.books.delete_book(book)

    assert not sealed.exists() and not sealed_cover.exists()
    assert library.sealing.jobs_by_book() == {}
