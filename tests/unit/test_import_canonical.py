"""Importing an archive by repackaging it instead of copying it.

The feature only pays off if three things hold together: the library records the
*source's* identity so the same file is still recognised after its bytes were
replaced, the stored artifact is addressed so no two rows can ever share it, and
the metadata the archive carried ends up on the book rather than a filename.
"""

from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import zipfile

from PIL import Image

from joyread.core.archive import ArchiveImageService
from joyread.core.models.import_policy import CanonicalImportPolicy
from joyread.core.repositories.sqlite_tag_repository import SqliteTagRepository
from joyread.core.services.hash_service import HashService
from joyread.core.services.import_service import ImportService, ImportStage
from joyread.core.services.tag_service import TagService
from joyread.infrastructure.database import DatabaseInterpreter, DatabasePriority, apply_migrations
from joyread.infrastructure.filesystem.path_service import PathService


def _png(color: str = "#336699") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (8, 12), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def _nested(tmp_path: Path, name: str = "nested.cbz", **extra: bytes) -> Path:
    inner = _zip(tmp_path / "src" / "Vol01.cbz", {"001.png": _png(), "002.png": _png()})
    return _zip(tmp_path / name, {"Vol01.cbz": inner.read_bytes(), **extra})


def _services(
    tmp_path: Path,
    policy: CanonicalImportPolicy = CanonicalImportPolicy.EXPENSIVE_AND_NESTED,
) -> tuple[ImportService, DatabaseInterpreter, PathService]:
    paths = PathService(storage_root=tmp_path / "storage", support_root=tmp_path / "support")
    paths.ensure_directories()
    database = DatabaseInterpreter(paths.paths.database / "joyread.sqlite3")
    database.execute(apply_migrations, DatabasePriority.CRITICAL)
    service = ImportService(
        paths,
        database,
        ArchiveImageService(),
        HashService(),
        tag_service=TagService(SqliteTagRepository(database)),
        canonical_import_policy=policy,
    )
    return service, database, paths


def _file_row(database: DatabaseInterpreter):
    return database.execute(
        lambda connection: connection.execute(
            "SELECT * FROM book_files"
        ).fetchone(),
        DatabasePriority.CRITICAL,
    )


def _book_row(database: DatabaseInterpreter):
    return database.execute(
        lambda connection: connection.execute("SELECT * FROM books").fetchone(),
        DatabasePriority.CRITICAL,
    )


# ----------------------------------------------------------------------
# Policy
# ----------------------------------------------------------------------


def test_a_nested_archive_is_converted_even_though_zip_is_cheap(tmp_path: Path) -> None:
    """Nesting is structural, not a matter of degree.

    A nested tree can never be bulk-converted into the cache, so it pays
    sequential warmup on *every* open for the rest of its life. Converting at
    import is the only thing that stops that clock.
    """

    service, database, _paths = _services(tmp_path)
    try:
        result = service.import_files([_nested(tmp_path)])

        assert result.imported_count == 1
        assert result.items[0].message == "Imported and converted."
        assert _file_row(database)["storage_kind"] == "canonical"
    finally:
        database.close()


def test_a_flat_zip_is_left_alone_by_the_default_policy(tmp_path: Path) -> None:
    """Rewriting a flat CBZ costs a full copy and saves nothing measurable."""

    service, database, _paths = _services(tmp_path)
    try:
        source = _zip(tmp_path / "flat.cbz", {"001.png": _png()})

        result = service.import_files([source])

        assert result.items[0].message == "Imported."
        assert _file_row(database)["storage_kind"] == "verbatim"
    finally:
        database.close()


def test_the_never_policy_leaves_a_nested_archive_verbatim(tmp_path: Path) -> None:
    service, database, _paths = _services(tmp_path, CanonicalImportPolicy.NEVER)
    try:
        service.import_files([_nested(tmp_path)])

        assert _file_row(database)["storage_kind"] == "verbatim"
    finally:
        database.close()


def test_the_always_policy_converts_a_flat_zip(tmp_path: Path) -> None:
    service, database, _paths = _services(tmp_path, CanonicalImportPolicy.ALWAYS)
    try:
        service.import_files([_zip(tmp_path / "flat.cbz", {"001.png": _png()})])

        assert _file_row(database)["storage_kind"] == "canonical"
    finally:
        database.close()


# ----------------------------------------------------------------------
# The two hashes, once they actually differ
# ----------------------------------------------------------------------


def test_a_converted_book_records_the_source_hash_and_the_artifact_hash(
    tmp_path: Path,
) -> None:
    """The case the schema split exists for: the two values finally diverge."""

    service, database, paths = _services(tmp_path)
    try:
        source = _nested(tmp_path)
        source_hash = HashService().compute(source, "sha256")

        service.import_files([source])
        row = _file_row(database)

        stored = paths.resolver.to_storage_absolute(row["storage_path"])
        assert row["source_hash"] == source_hash
        assert row["stored_hash"] != source_hash
        assert row["stored_hash"] == HashService().compute(stored, "sha256")
    finally:
        database.close()


def test_reimporting_the_same_source_is_a_duplicate_after_conversion(
    tmp_path: Path,
) -> None:
    """The proof that dedupe keys on the source.

    The stored artifact no longer hashes like anything the user has on disk, so
    a library matching on ``stored_hash`` would happily import this book again
    and again.
    """

    service, database, _paths = _services(tmp_path)
    try:
        source = _nested(tmp_path)
        first = service.import_files([source])

        second = service.import_files([source])

        assert first.imported_count == 1
        assert second.duplicate_count == 1
        assert second.items[0].book_id == first.items[0].book_id
    finally:
        database.close()


def test_a_canonical_artifact_is_addressed_by_file_id_not_by_its_bytes(
    tmp_path: Path,
) -> None:
    """Two different sources can repackage to identical bytes.

    Content addressing would hand both rows one file, and deleting either book
    would then delete the other's pages.
    """

    service, database, _paths = _services(tmp_path)
    try:
        service.import_files([_nested(tmp_path)])
        row = _file_row(database)

        assert Path(row["storage_path"]).stem == row["file_id"]
        assert Path(row["storage_path"]).suffix == ".cbz"
    finally:
        database.close()


def test_two_sources_that_repackage_identically_keep_separate_files(
    tmp_path: Path,
) -> None:
    """The collision the layout is designed around, built end to end."""

    service, database, paths = _services(tmp_path)
    try:
        # Same pages, same order, different outer containers -- so the canonical
        # output is byte-identical while the sources are not.
        inner = _zip(tmp_path / "src" / "Vol01.cbz", {"001.png": _png(), "002.png": _png()})
        first = _zip(tmp_path / "a.cbz", {"Vol01.cbz": inner.read_bytes()})
        second = _zip(tmp_path / "b.cbz", {"Vol01.cbz": inner.read_bytes(), "extra.txt": b"x"})

        service.import_files([first, second])

        rows = database.execute(
            lambda connection: connection.execute(
                "SELECT file_id, storage_path, source_hash, stored_hash FROM book_files"
            ).fetchall(),
            DatabasePriority.CRITICAL,
        )
        assert len(rows) == 2
        assert rows[0]["stored_hash"] == rows[1]["stored_hash"]
        assert rows[0]["source_hash"] != rows[1]["source_hash"]
        assert rows[0]["storage_path"] != rows[1]["storage_path"]
        for row in rows:
            assert paths.resolver.to_storage_absolute(row["storage_path"]).exists()
    finally:
        database.close()


# ----------------------------------------------------------------------
# Metadata
# ----------------------------------------------------------------------


def test_the_archives_metadata_becomes_the_book_rather_than_the_filename(
    tmp_path: Path,
) -> None:
    service, database, _paths = _services(tmp_path)
    try:
        meta = json.dumps(
            {
                "title": {"english": "A Real Title"},
                "language": "japanese",
                "tags": [
                    {"type": "tag", "name": "comedy"},
                    {"type": "artist", "name": "Some Artist"},
                ],
            }
        ).encode()
        source = _zip(tmp_path / "ugly-filename-01.cbz", {"001.png": _png(), "meta.json": meta})

        service.import_files([source])
        book = _book_row(database)

        assert book["title"] == "A Real Title"
        assert book["author"] == "Some Artist"
        assert book["language_tag"] == "ja"
    finally:
        database.close()


def test_metadata_tags_are_linked_to_the_book(tmp_path: Path) -> None:
    service, database, _paths = _services(tmp_path)
    try:
        meta = json.dumps(
            {"title": {"english": "T"}, "tags": [{"type": "tag", "name": "full color"}]}
        ).encode()
        service.import_files(
            [_zip(tmp_path / "book.cbz", {"001.png": _png(), "meta.json": meta})]
        )

        names = database.execute(
            lambda connection: [
                str(row["name"])
                for row in connection.execute(
                    "SELECT tags.name FROM tags"
                    " JOIN book_tags ON book_tags.tag_id = tags.tag_id"
                ).fetchall()
            ],
            DatabasePriority.CRITICAL,
        )

        # ``TagService`` applies the library's own tag normalisation, the same
        # as a hand-typed tag: metadata is a source of names, not a bypass.
        assert names == ["Full color"]
    finally:
        database.close()


def test_an_archive_without_metadata_still_falls_back_to_the_filename(
    tmp_path: Path,
) -> None:
    service, database, _paths = _services(tmp_path)
    try:
        service.import_files([_zip(tmp_path / "Fallback Name.cbz", {"001.png": _png()})])
        book = _book_row(database)

        assert book["title"] == "Fallback Name"
        assert book["author"] == "Unknown"
        assert book["language_tag"] == "und"
    finally:
        database.close()


def test_metadata_survives_conversion_into_the_artifact(tmp_path: Path) -> None:
    """A converted book must stay self-describing: exporting it and importing
    it somewhere else should not lose the title."""

    service, database, paths = _services(tmp_path)
    try:
        meta = json.dumps({"title": {"english": "Carried Over"}}).encode()
        service.import_files([_nested(tmp_path, **{"meta.json": meta})])
        row = _file_row(database)

        stored = paths.resolver.to_storage_absolute(row["storage_path"])
        with zipfile.ZipFile(stored) as archive:
            assert "meta.json" in archive.namelist()
            assert b"Carried Over" in archive.read("meta.json")
    finally:
        database.close()


# ----------------------------------------------------------------------
# Progress
# ----------------------------------------------------------------------


def test_progress_reports_stages_and_a_real_page_count_while_converting(
    tmp_path: Path,
) -> None:
    """Only conversion has an honest denominator, so only it reports one."""

    service, database, _paths = _services(tmp_path)
    try:
        events: list[tuple[str, int, int]] = []
        service.import_items(
            [{"source_path": str(_nested(tmp_path))}],
            manifest_path=None,
            progress=lambda event: events.append(
                (event.stage.value, event.unit_done, event.unit_total)
            ),
        )

        stages = [stage for stage, _done, _total in events]
        assert stages[0] == ImportStage.STAGING.value
        assert ImportStage.INSPECTING.value in stages
        assert stages[-1] == ImportStage.RECORDING.value
        converting = [
            (done, total)
            for stage, done, total in events
            if stage == ImportStage.CONVERTING.value
        ]
        # The leading 0-of-N matters: bulk extraction pulls the whole container
        # out before the writer is handed page one, so without it the dialog
        # sits on "Checking contents..." through nearly all the work on a large
        # solid archive and only says "Converting" once it is basically done.
        # The denominator comes from the inspection and must agree with the
        # writer's own count.
        assert converting == [(0, 2), (1, 2), (2, 2)]
    finally:
        database.close()


def test_import_without_a_progress_callback_is_unaffected(tmp_path: Path) -> None:
    """Every existing caller passes nothing, and must keep working."""

    service, database, _paths = _services(tmp_path)
    try:
        assert service.import_files([_nested(tmp_path)]).imported_count == 1
    finally:
        database.close()


def test_a_cancelled_bulk_extraction_skips_the_item_instead_of_importing_it(
    tmp_path: Path,
) -> None:
    """``ArchiveCancelled`` subclasses ``ArchiveError``.

    Without an explicit branch it lands in the recover-by-storing-verbatim path,
    so a user who cancelled gets the book imported anyway and reported as a
    success — the opposite of what they asked for.
    """

    from joyread.core.archive.errors import ArchiveCancelled

    service, database, _paths = _services(tmp_path)
    try:
        def cancel_during_conversion(*_args, **_kwargs):
            raise ArchiveCancelled("Archive conversion cancelled.")

        service._archive_service.convert_to_canonical = cancel_during_conversion

        result = service.import_files([_nested(tmp_path), _nested(tmp_path, "second.cbz")])

        assert result.imported_count == 0
        assert result.skipped_count == 1
        assert result.items[0].status == "skipped"
        assert result.items[0].message == "Import cancelled."
        # And the batch stopped rather than carrying on into the second item.
        assert len(result.items) == 1
        assert database.execute(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM book_files"
            ).fetchone()[0]
        ) == 0
    finally:
        database.close()


def test_rejected_tags_from_the_archive_and_the_manifest_are_both_counted(
    tmp_path: Path,
) -> None:
    """A manifest import of an archive that carries its own tags has two
    sources of rejection, and reporting only the last one under-counts what the
    user lost."""

    import json as _json

    service, database, _paths = _services(tmp_path)
    try:
        meta = _json.dumps(
            {"title": {"english": "T"}, "tags": [{"type": "tag", "name": "from-archive"}]}
        ).encode()
        source = _zip(tmp_path / "book.cbz", {"001.png": _png(), "meta.json": meta})

        # Every tag is refused, whichever list it came from.
        service._tag_service.find_or_create = lambda _name: None

        result = service.import_items(
            [{"source_path": str(source), "tags": ["from-manifest-a", "from-manifest-b"]}],
            manifest_path=None,
        )

        assert result.items[0].status == "imported"
        assert result.items[0].tags_rejected == 3  # 1 from the sidecar + 2 from the manifest
    finally:
        database.close()
