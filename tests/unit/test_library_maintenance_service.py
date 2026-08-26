from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
from threading import Event, Thread
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from PIL import Image

from joyread.core.archive import ArchiveImageService, ArchiveOpenLimits
from joyread.core.repositories.sqlite_book_repository import SqliteBookRepository
from joyread.core.services.archive_extraction_pool import ArchiveExtractionPool
from joyread.core.models.import_policy import CanonicalImportPolicy
from joyread.core.services.hash_service import HashService
from joyread.core.services.import_service import ImportService
from joyread.core.services.library_maintenance_service import (
    LibraryAuditAction,
    LibraryMaintenanceCoordinator,
    LibraryMaintenanceService,
    is_platform_metadata,
)
from joyread.infrastructure.database import DatabaseInterpreter, DatabasePriority, apply_migrations
from joyread.infrastructure.filesystem.path_service import PathService


def _png_payload(color: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (16, 24), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _write_cbz(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("001.png", _png_payload(color))


@pytest.fixture
def maintenance_stack(tmp_path: Path):
    paths = PathService(storage_root=tmp_path / "library", support_root=tmp_path / "support")
    paths.ensure_directories()
    database = DatabaseInterpreter(paths.paths.database / "joyread.sqlite3")
    database.execute(apply_migrations, DatabasePriority.CRITICAL)
    cache = ArchiveExtractionPool(paths.paths.cache / ".archive_zip_bundles", max_bytes=32 * 1024 * 1024)
    archive_service = ArchiveImageService(extraction_pool=cache)
    coordinator = LibraryMaintenanceCoordinator()
    hash_service = HashService()
    importer = ImportService(
        paths,
        database,
        archive_service,
        hash_service,
        maintenance_coordinator=coordinator,
    )
    invalidated: list[str] = []
    maintenance = LibraryMaintenanceService(
        paths,
        database,
        hash_service,
        archive_service,
        extraction_cache=cache,
        invalidate_file_cache=invalidated.append,
        coordinator=coordinator,
    )
    repository = SqliteBookRepository(
        database,
        resolver=paths.resolver,
        managed_books_root=paths.paths.books,
        thumbnails_root=paths.paths.thumbnails,
    )
    try:
        yield paths, database, cache, importer, maintenance, repository, invalidated
    finally:
        database.close()


def _import_book(importer: ImportService, repository: SqliteBookRepository, source: Path):
    result = importer.import_files([source])
    assert result.imported_count == 1
    assert result.items[0].book_id is not None
    book = repository.get_book(result.items[0].book_id)
    assert book is not None
    return book


def test_maintenance_lease_can_finish_on_ui_thread_after_worker_acquires_it() -> None:
    """Storage tasks retain the gate until their UI-side service rebuild ends."""

    coordinator = LibraryMaintenanceCoordinator()
    lease_ready = Event()
    import_finished = Event()
    leases = []

    def acquire_storage_lease() -> None:
        leases.append(coordinator.acquire("storage-move"))
        lease_ready.set()

    def wait_for_import() -> None:
        with coordinator.hold("import"):
            import_finished.set()

    storage_worker = Thread(target=acquire_storage_lease)
    storage_worker.start()
    assert lease_ready.wait(timeout=1)
    storage_worker.join(timeout=1)

    import_worker = Thread(target=wait_for_import)
    import_worker.start()
    assert not import_finished.wait(timeout=0.05)

    # The UI thread, not the worker that acquired it, releases this lease once
    # the replacement AppContext services are ready.
    leases[0].release()
    assert import_finished.wait(timeout=1)
    import_worker.join(timeout=1)


def test_audit_changed_file_renames_resets_navigation_and_invalidates_artifacts(maintenance_stack, tmp_path: Path) -> None:
    paths, _database, cache, importer, maintenance, repository, invalidated = maintenance_stack
    source = tmp_path / "source" / "Book.cbz"
    _write_cbz(source, "#224466")
    book = _import_book(importer, repository, source)
    original_path = Path(book.file_path)
    repository.set_progress(book.uuid, 4, 60.0)
    repository.add_bookmark(book.uuid, "Here", 4)
    cover = paths.paths.thumbnails / "covers" / f"{book.uuid}-generated-100x120.png"
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(b"generated")
    cache.put(f"file:{book.file_id}", "00000000", b"cached")

    _write_cbz(original_path, "#cc4422")
    plan = maintenance.scan()
    item = next(item for item in plan.items if item.file_id == book.file_id)
    assert item.action is LibraryAuditAction.CHANGED
    assert item.observed_hash is not None

    report = maintenance.apply(plan)
    refreshed = repository.get_book(book.uuid)
    assert refreshed is not None
    expected = paths.paths.books / item.observed_hash[:2] / f"{item.observed_hash}.cbz"
    assert report.changed_count == 1
    assert refreshed.file_path == str(expected)
    assert expected.exists()
    assert not original_path.exists()
    assert repository.get_progress(book.uuid) is None
    assert repository.list_bookmarks(book.uuid) == []
    assert cache.get(f"file:{book.file_id}", "00000000") is None
    assert not cover.exists()
    assert invalidated == [book.file_id]


def test_audit_scan_keeps_one_archive_limits_snapshot(maintenance_stack, tmp_path: Path, monkeypatch) -> None:
    _paths, _database, _cache, importer, maintenance, repository, _invalidated = maintenance_stack
    first = tmp_path / "source" / "first.cbz"
    second = tmp_path / "source" / "second.cbz"
    _write_cbz(first, "#224466")
    _write_cbz(second, "#cc4422")
    _import_book(importer, repository, first)
    _import_book(importer, repository, second)
    initial_limits = ArchiveOpenLimits(max_source_bytes=5 * 1024 * 1024)
    maintenance.set_archive_open_limits(initial_limits)
    observed_limits: list[ArchiveOpenLimits] = []
    original_probe = maintenance._archive_service.probe_archive

    def probe(path: Path, *, limits: ArchiveOpenLimits):
        observed_limits.append(limits)
        if len(observed_limits) == 1:
            maintenance.set_archive_open_limits(ArchiveOpenLimits(max_source_bytes=1))
        return original_probe(path, limits=limits)

    monkeypatch.setattr(maintenance._archive_service, "probe_archive", probe)

    plan = maintenance.scan()

    assert len(plan.items) == 2
    assert observed_limits == [initial_limits, initial_limits]


def test_audit_missing_file_marks_book_missing(maintenance_stack, tmp_path: Path) -> None:
    _paths, _database, _cache, importer, maintenance, repository, _invalidated = maintenance_stack
    source = tmp_path / "source" / "Missing.cbz"
    _write_cbz(source, "#224466")
    book = _import_book(importer, repository, source)
    Path(book.file_path).unlink()

    plan = maintenance.scan()
    item = next(item for item in plan.items if item.file_id == book.file_id)
    assert item.action is LibraryAuditAction.MISSING
    report = maintenance.apply(plan)

    refreshed = repository.get_book(book.uuid)
    assert report.missing_count == 1
    assert refreshed is not None and refreshed.is_missing


def test_audit_invalid_changed_file_marks_book_unavailable_until_next_audit(maintenance_stack, tmp_path: Path) -> None:
    _paths, _database, _cache, importer, maintenance, repository, _invalidated = maintenance_stack
    source = tmp_path / "source" / "Broken.cbz"
    _write_cbz(source, "#224466")
    book = _import_book(importer, repository, source)
    Path(book.file_path).write_bytes(b"not an archive")

    plan = maintenance.scan()
    item = next(item for item in plan.items if item.file_id == book.file_id)
    assert item.action is LibraryAuditAction.UNAVAILABLE
    report = maintenance.apply(plan)

    refreshed = repository.get_book(book.uuid)
    assert report.unavailable_count == 1
    assert refreshed is not None
    assert refreshed.is_unavailable
    assert not refreshed.is_available


def test_audit_merges_changed_file_when_its_hash_already_exists(maintenance_stack, tmp_path: Path) -> None:
    _paths, database, _cache, importer, maintenance, repository, _invalidated = maintenance_stack
    source_a = tmp_path / "source" / "A.cbz"
    source_b = tmp_path / "source" / "B.cbz"
    _write_cbz(source_a, "#224466")
    _write_cbz(source_b, "#cc4422")
    first = _import_book(importer, repository, source_a)
    second = _import_book(importer, repository, source_b)
    repository.set_progress(first.uuid, 2, 50.0)
    Path(first.file_path).write_bytes(Path(second.file_path).read_bytes())

    plan = maintenance.scan()
    item = next(item for item in plan.items if item.file_id == first.file_id)
    assert item.action is LibraryAuditAction.MERGE
    assert item.duplicate_file_id == second.file_id
    report = maintenance.apply(plan)

    merged = repository.get_book(first.uuid)
    assert report.merged_count == 1
    assert merged is not None and merged.file_id == second.file_id
    assert repository.get_progress(first.uuid) is None
    remaining = database.execute(
        lambda connection: connection.execute(
            "SELECT COUNT(*) FROM book_files WHERE file_id = ?", (first.file_id,)
        ).fetchone()[0]
    )
    assert remaining == 0


def test_audit_cleans_orphan_book_generated_cover_and_extraction_cache(maintenance_stack, tmp_path: Path) -> None:
    paths, _database, cache, importer, maintenance, repository, _invalidated = maintenance_stack
    source = tmp_path / "source" / "Managed.cbz"
    _write_cbz(source, "#224466")
    _import_book(importer, repository, source)
    orphan_book = paths.paths.books / "orphan.cbz"
    _write_cbz(orphan_book, "#cc4422")
    covers = paths.paths.thumbnails / "covers"
    covers.mkdir(parents=True, exist_ok=True)
    generated = covers / "no-longer-a-book-generated-100x120.png"
    generated.write_bytes(b"generated")
    custom = covers / "no-longer-a-book-custom.png"
    custom.write_bytes(b"custom")
    cache.put("file:no-longer-a-file", "00000000", b"cached")

    plan = maintenance.scan()
    assert orphan_book in {orphan.path for orphan in plan.orphan_files}
    cache_paths = {orphan.path for orphan in plan.orphan_cache_files}
    assert generated in cache_paths
    assert any(path.parent == paths.paths.cache / ".archive_zip_bundles" for path in cache_paths)

    report = maintenance.apply(plan)
    assert report.cleaned_file_count == 1
    assert report.cleaned_cache_count >= 2
    assert not orphan_book.exists()
    assert not generated.exists()
    assert custom.exists()


def test_recover_pending_rename_journal_finishes_database_update(maintenance_stack, tmp_path: Path) -> None:
    paths, database, _cache, importer, maintenance, repository, _invalidated = maintenance_stack
    source = tmp_path / "source" / "Journal.cbz"
    _write_cbz(source, "#224466")
    book = _import_book(importer, repository, source)
    target = Path(book.file_path)
    old_path = paths.paths.books / "legacy" / "journal.cbz"
    old_path.parent.mkdir(parents=True, exist_ok=True)
    target.replace(old_path)
    old_relative = paths.resolver.to_storage_relative(old_path)
    target_relative = paths.resolver.to_storage_relative(target)
    journal_id = "rename-journal"

    def seed_journal(connection) -> None:  # noqa: ANN001 - database actor callback.
        connection.execute("BEGIN")
        try:
            connection.execute(
                "UPDATE book_files SET storage_path = ? WHERE file_id = ?",
                (old_relative, book.file_id),
            )
            connection.execute(
                """
                INSERT INTO library_maintenance_journal(
                    journal_id, operation_kind, file_id, from_storage_path,
                    to_storage_path, payload_json, created_at
                ) VALUES (?, 'rename', ?, ?, ?, ?, '2026-01-01T00:00:00')
                """,
                (journal_id, book.file_id, old_relative, target_relative, json.dumps({"content_hash": "ignored"})),
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    database.execute(seed_journal, DatabasePriority.NORMAL)
    old_path.replace(target)

    # The recovery journal carries the intended content hash. Use the actual
    # recorded value so the test mirrors a real interrupted audit rename.
    recorded_hash = database.execute(
        lambda connection: connection.execute(
            "SELECT stored_hash FROM book_files WHERE file_id = ?", (book.file_id,)
        ).fetchone()[0]
    )
    database.execute(
        lambda connection: connection.execute(
            "UPDATE library_maintenance_journal SET payload_json = ? WHERE journal_id = ?",
            (json.dumps({"content_hash": recorded_hash}), journal_id),
        ),
        DatabasePriority.NORMAL,
    )

    recovery = maintenance.recover_pending_journal()
    refreshed = repository.get_book(book.uuid)
    remaining = database.execute(
        lambda connection: connection.execute(
            "SELECT COUNT(*) FROM library_maintenance_journal WHERE journal_id = ?", (journal_id,)
        ).fetchone()[0]
    )
    assert recovery.recovered_count == 1
    assert recovery.conflicts == ()
    assert refreshed is not None and Path(refreshed.file_path) == target
    assert remaining == 0


def test_recover_pending_rename_journal_keeps_conflict_when_target_bytes_changed(
    maintenance_stack,
    tmp_path: Path,
) -> None:
    paths, database, _cache, importer, maintenance, repository, _invalidated = maintenance_stack
    source = tmp_path / "source" / "Journal Conflict.cbz"
    _write_cbz(source, "#224466")
    book = _import_book(importer, repository, source)
    target = Path(book.file_path)
    old_path = paths.paths.books / "legacy" / "journal-conflict.cbz"
    old_path.parent.mkdir(parents=True, exist_ok=True)
    target.replace(old_path)
    old_relative = paths.resolver.to_storage_relative(old_path)
    target_relative = paths.resolver.to_storage_relative(target)
    journal_id = "rename-conflict"
    recorded_hash = database.execute(
        lambda connection: connection.execute(
            "SELECT stored_hash FROM book_files WHERE file_id = ?", (book.file_id,)
        ).fetchone()[0]
    )

    def seed_journal(connection) -> None:  # noqa: ANN001 - database actor callback.
        connection.execute("BEGIN")
        try:
            connection.execute(
                "UPDATE book_files SET storage_path = ? WHERE file_id = ?",
                (old_relative, book.file_id),
            )
            connection.execute(
                """
                INSERT INTO library_maintenance_journal(
                    journal_id, operation_kind, file_id, from_storage_path,
                    to_storage_path, payload_json, created_at
                ) VALUES (?, 'rename', ?, ?, ?, ?, '2026-01-01T00:00:00')
                """,
                (
                    journal_id,
                    book.file_id,
                    old_relative,
                    target_relative,
                    json.dumps({"content_hash": recorded_hash}),
                ),
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    database.execute(seed_journal, DatabasePriority.NORMAL)
    # Simulate a crash after the rename followed by an external replacement of
    # the destination. Recovery must preserve both evidence and journal rather
    # than attaching the stale hash to new bytes.
    old_path.replace(target)
    _write_cbz(target, "#cc4422")

    recovery = maintenance.recover_pending_journal()
    refreshed = repository.get_book(book.uuid)
    remaining = database.execute(
        lambda connection: connection.execute(
            "SELECT COUNT(*) FROM library_maintenance_journal WHERE journal_id = ?", (journal_id,)
        ).fetchone()[0]
    )

    assert recovery.recovered_count == 0
    assert recovery.conflicts == (journal_id,)
    assert refreshed is not None and Path(refreshed.file_path) == old_path
    assert remaining == 1


def test_audit_ignores_file_manager_metadata(maintenance_stack, tmp_path: Path) -> None:
    """Finder recreates .DS_Store the moment the user opens the folder, so
    reporting it produced an orphan that came back after every cleanup."""

    paths, _database, _cache, importer, maintenance, repository, _invalidated = maintenance_stack
    source = tmp_path / "source" / "Managed.cbz"
    _write_cbz(source, "#224466")
    _import_book(importer, repository, source)

    noise = {
        paths.paths.books / ".DS_Store",
        paths.paths.books / "._Managed.cbz",
        paths.paths.books / "Thumbs.db",
        paths.paths.books / "desktop.ini",
    }
    for path in noise:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"metadata")
    real_orphan = paths.paths.books / "orphan.cbz"
    _write_cbz(real_orphan, "#cc4422")

    plan = maintenance.scan()

    reported = {orphan.path for orphan in plan.orphan_files}
    assert reported & noise == set(), "platform metadata must not be reported"
    assert real_orphan in reported, "a genuine orphan must still be found"

    maintenance.apply(plan)
    assert all(path.exists() for path in noise), "metadata must not be deleted either"


def test_platform_metadata_is_matched_case_insensitively() -> None:
    assert is_platform_metadata(".DS_Store") is True
    assert is_platform_metadata("thumbs.db") is True
    assert is_platform_metadata("._Managed.cbz") is True
    assert is_platform_metadata("desktop.ini") is True
    assert is_platform_metadata("Managed.cbz") is False
    assert is_platform_metadata("my.DS_Store.cbz") is False


def test_audit_never_merges_two_canonical_rows_that_hash_the_same(
    maintenance_stack, tmp_path: Path
) -> None:
    """Identical bytes stop implying an identical book once import repackages.

    Deterministic conversion maps two genuinely different sources -- a ``.cbr``
    and a ``.7z`` of the same pages -- onto byte-identical output. Merging those
    deletes one row, and with it the ``source_hash`` that was the only record
    that the second source had ever been imported: the user could re-import it
    and get a book the library already holds.
    """

    _paths, database, _cache, importer, maintenance, repository, _invalidated = maintenance_stack
    source_a = tmp_path / "source" / "A.cbz"
    source_b = tmp_path / "source" / "B.cbz"
    _write_cbz(source_a, "#224466")
    _write_cbz(source_b, "#cc4422")
    first = _import_book(importer, repository, source_a)
    second = _import_book(importer, repository, source_b)

    # Stand in for what canonical import will produce: distinct sources, and
    # bytes on disk that will hash alike once the audit reads them.
    database.execute(
        lambda connection: connection.execute(
            "UPDATE book_files SET storage_kind = 'canonical'"
        )
    )
    Path(first.file_path).write_bytes(Path(second.file_path).read_bytes())

    plan = maintenance.scan()
    item = next(item for item in plan.items if item.file_id == first.file_id)

    assert item.action is not LibraryAuditAction.MERGE
    assert item.duplicate_file_id is None

    maintenance.apply(plan)
    survivors = database.execute(
        lambda connection: connection.execute(
            "SELECT COUNT(*) FROM book_files"
        ).fetchone()[0]
    )
    assert survivors == 2
    assert repository.get_book(second.uuid) is not None


def test_audit_updates_a_changed_canonical_file_without_deleting_it(
    maintenance_stack, tmp_path: Path
) -> None:
    """A canonical artifact is addressed by its row, so changed bytes do not
    move it -- only the recorded hash has to catch up.

    The relocation branch that handles verbatim files ends by unlinking the file
    it moved *from*. For a canonical row source and destination are the same
    path, so taking that branch would delete the book.
    """

    _paths, database, _cache, importer, maintenance, repository, _invalidated = maintenance_stack
    source = tmp_path / "source" / "Canonical.cbz"
    _write_cbz(source, "#224466")
    importer.set_canonical_import_policy(CanonicalImportPolicy.ALWAYS)
    book = _import_book(importer, repository, source)

    stored = Path(book.file_path)
    assert stored.stem == book.file_id  # a real canonical row, addressed by id
    _write_cbz(stored, "#cc4422")
    expected_hash = HashService().compute(stored, "sha256")

    report = maintenance.apply(maintenance.scan())

    assert report.changed_count == 1
    assert stored.exists()
    row = database.execute(
        lambda connection: connection.execute(
            "SELECT storage_path, stored_hash FROM book_files WHERE file_id = ?",
            (book.file_id,),
        ).fetchone()
    )
    assert row["stored_hash"] == expected_hash
    assert Path(row["storage_path"]).name == stored.name


def test_audit_never_merges_a_changed_verbatim_file_into_a_canonical_row(
    maintenance_stack, tmp_path: Path
) -> None:
    """The candidate's kind matters as much as the item's.

    Guarding only the row being audited leaves the other half open: a changed
    *verbatim* row is content-addressed and therefore mergeable, and if the row
    it matches is *canonical* the merge still deletes one of them and takes a
    distinct ``source_hash`` with it. Both the scan-time hash map and the
    apply-time lookup have to exclude non-content-addressed candidates.
    """

    _paths, database, _cache, importer, maintenance, repository, _invalidated = maintenance_stack
    verbatim_source = tmp_path / "source" / "Verbatim.cbz"
    canonical_source = tmp_path / "source" / "Canonical.cbz"
    _write_cbz(verbatim_source, "#224466")
    _write_cbz(canonical_source, "#cc4422")

    verbatim = _import_book(importer, repository, verbatim_source)
    importer.set_canonical_import_policy(CanonicalImportPolicy.ALWAYS)
    canonical = _import_book(importer, repository, canonical_source)

    kinds = database.execute(
        lambda connection: {
            str(row["file_id"]): str(row["storage_kind"])
            for row in connection.execute(
                "SELECT file_id, storage_kind FROM book_files"
            ).fetchall()
        }
    )
    assert kinds[verbatim.file_id] == "verbatim"
    assert kinds[canonical.file_id] == "canonical"

    # The verbatim file is edited to hold the canonical artifact's bytes.
    Path(verbatim.file_path).write_bytes(Path(canonical.file_path).read_bytes())

    plan = maintenance.scan()
    item = next(item for item in plan.items if item.file_id == verbatim.file_id)

    assert item.action is not LibraryAuditAction.MERGE
    assert item.duplicate_file_id is None

    maintenance.apply(plan)
    survivors = database.execute(
        lambda connection: connection.execute("SELECT COUNT(*) FROM book_files").fetchone()[0]
    )
    assert survivors == 2
