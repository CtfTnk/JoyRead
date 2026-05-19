"""Integration tests for ImportService + TagService."""

from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image

from joyread.core.archive import ArchiveImageService
from joyread.core.repositories.sqlite_tag_repository import SqliteTagRepository
from joyread.core.services.hash_service import HashService
from joyread.core.services.import_service import ImportService
from joyread.core.services.tag_service import TagService
from joyread.infrastructure.database import DatabaseInterpreter, DatabasePriority, apply_migrations
from joyread.infrastructure.filesystem.path_service import PathService


def _database(path: Path) -> DatabaseInterpreter:
    database = DatabaseInterpreter(path / "joyread.sqlite3")
    database.execute(apply_migrations, DatabasePriority.CRITICAL)
    return database


def _write_cbz(path: Path, *, color: str = "#336699") -> None:
    image = path.with_suffix(".png")
    Image.new("RGB", (10, 20), color).save(image, format="PNG")
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")


def _make_services(tmp_path: Path) -> tuple[ImportService, DatabaseInterpreter, PathService, TagService]:
    paths = PathService(storage_root=tmp_path / "storage", support_root=tmp_path / "support")
    paths.ensure_directories()
    database = _database(paths.paths.database)
    tag_service = TagService(SqliteTagRepository(database))
    service = ImportService(
        paths,
        database,
        ArchiveImageService(),
        HashService(),
        tag_service=tag_service,
    )
    return service, database, paths, tag_service


def test_import_manifest_creates_and_links_tags(tmp_path: Path) -> None:
    service, database, paths, tag_service = _make_services(tmp_path)
    try:
        source = tmp_path / "first.cbz"
        _write_cbz(source)
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": [{"source_path": str(source), "tags": ["Comedy", "Action"]}],
                }
            ),
            encoding="utf-8",
        )

        result = service.import_manifest(manifest_path)
        assert result.imported_count == 1
        book_id = result.items[0].book_id
        assert book_id is not None

        tags = tag_service.list_tags()
        linked = tag_service.repository.list_tag_ids_for_book(book_id)

        assert sorted(tag.name for tag in tags) == ["Action", "Comedy"]
        assert sorted(linked) == sorted(tag.tag_id for tag in tags)
    finally:
        database.close()


def test_import_manifest_reuses_existing_tags_on_second_run(tmp_path: Path) -> None:
    service, database, paths, tag_service = _make_services(tmp_path)
    try:
        source_a = tmp_path / "a.cbz"
        source_b = tmp_path / "b.cbz"
        _write_cbz(source_a, color="#336699")
        _write_cbz(source_b, color="#9933AA")
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": [
                        {"source_path": str(source_a), "tags": ["Comedy"]},
                        {"source_path": str(source_b), "tags": ["comedy"]},
                    ],
                }
            ),
            encoding="utf-8",
        )

        service.import_manifest(manifest_path)
        tags = tag_service.list_tags()

        assert len(tags) == 1
        assert tags[0].name == "Comedy"
    finally:
        database.close()


def test_import_manifest_skips_invalid_tag_entries_without_aborting(tmp_path: Path) -> None:
    service, database, paths, tag_service = _make_services(tmp_path)
    try:
        source = tmp_path / "first.cbz"
        _write_cbz(source)
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": [
                        {
                            "source_path": str(source),
                            "tags": [
                                "Comedy",
                                "   ",  # rejected
                                "x" * 64,  # rejected as overlong
                                42,  # rejected as non-string
                                "Action",
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = service.import_manifest(manifest_path)
        assert result.imported_count == 1
        tag_names = sorted(tag.name for tag in tag_service.list_tags())
        linked = tag_service.repository.list_tag_ids_for_book(result.items[0].book_id)
        assert tag_names == ["Action", "Comedy"]
        assert len(linked) == 2
    finally:
        database.close()


def test_import_manifest_without_tags_field_works(tmp_path: Path) -> None:
    service, database, paths, tag_service = _make_services(tmp_path)
    try:
        source = tmp_path / "first.cbz"
        _write_cbz(source)
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps({"version": 1, "items": [{"source_path": str(source)}]}),
            encoding="utf-8",
        )
        result = service.import_manifest(manifest_path)
        assert result.imported_count == 1
        assert tag_service.list_tags() == []
    finally:
        database.close()


def test_duplicate_import_still_links_tags_to_existing_book(tmp_path: Path) -> None:
    service, database, paths, tag_service = _make_services(tmp_path)
    try:
        source = tmp_path / "dup.cbz"
        _write_cbz(source)
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps({"version": 1, "items": [{"source_path": str(source)}]}),
            encoding="utf-8",
        )
        first = service.import_manifest(manifest_path)
        assert first.imported_count == 1
        book_id = first.items[0].book_id
        assert book_id is not None

        manifest_path.write_text(
            json.dumps(
                {"version": 1, "items": [{"source_path": str(source), "tags": ["Comedy"]}]}
            ),
            encoding="utf-8",
        )
        second = service.import_manifest(manifest_path)
        assert second.duplicate_count == 1
        assert second.items[0].book_id == book_id

        linked = tag_service.repository.list_tag_ids_for_book(book_id)
        assert len(linked) == 1
    finally:
        database.close()
