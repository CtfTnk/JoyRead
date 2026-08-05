"""Tests for ArchiveExtractionPool."""

from __future__ import annotations

import json
import time
from pathlib import Path
from zipfile import ZipFile

from joyread.core.services.archive_cache_lease import ArchiveCacheLease, ArchiveCacheScope
from joyread.core.services.archive_extraction_pool import (
    ArchiveExtractionPool,
    HiddenImageExtractionPool,
    archive_cache_storage_key,
)


def _write_source(tmp_path: Path, name: str = "book.7z", body: bytes = b"fake-archive") -> Path:
    source = tmp_path / name
    source.write_bytes(body)
    return source


def test_put_and_get_round_trip_via_source_path_and_entry_name(tmp_path: Path) -> None:
    pool = ArchiveExtractionPool(tmp_path / "cache", max_bytes=4096)
    source = _write_source(tmp_path)

    pool.put(source, "001.png", b"PNG-PAYLOAD")

    assert pool.get(source, "001.png") == b"PNG-PAYLOAD"
    # A miss for an unknown entry in the same bundle does not nuke the bundle.
    assert pool.get(source, "missing.png") is None
    assert pool.get(source, "001.png") == b"PNG-PAYLOAD"


def test_each_source_archive_gets_a_single_zip_bundle(tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    pool = ArchiveExtractionPool(directory, max_bytes=4096)
    source = _write_source(tmp_path)

    pool.put(source, "001.png", b"page-one")
    pool.put(source, "002.png", b"page-two")
    pool.put(source, "003.png", b"page-three")

    bundles = list(directory.glob("*.zip"))
    # Multiple pages from the same source collapse into one bundle on disk.
    assert len(bundles) == 1
    with ZipFile(bundles[0]) as archive:
        assert sorted(archive.namelist()) == ["001.png", "002.png", "003.png"]


def test_per_book_lru_evicts_oldest_bundle_when_over_budget(tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    # Each bundle is ~zip-overhead + payload. Pick a budget that fits two
    # bundles but not three so eviction is unambiguous.
    pool = ArchiveExtractionPool(directory, max_bytes=4_000)
    source_a = _write_source(tmp_path, "a.7z")
    source_b = _write_source(tmp_path, "b.7z")
    source_c = _write_source(tmp_path, "c.7z")

    pool.put(source_a, "001.png", b"A" * 1_500)
    time.sleep(0.01)
    pool.put(source_b, "001.png", b"B" * 1_500)
    time.sleep(0.01)
    pool.put(source_c, "001.png", b"C" * 1_500)

    assert pool.current_bytes <= 4_000
    # The oldest book (A) lost its bundle entirely, not just one of its pages.
    assert pool.get(source_a, "001.png") is None
    assert pool.get(source_b, "001.png") == b"B" * 1_500
    assert pool.get(source_c, "001.png") == b"C" * 1_500


def test_get_refreshes_lru_position_for_the_whole_book(tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    pool = ArchiveExtractionPool(directory, max_bytes=4_000)
    source_a = _write_source(tmp_path, "a.7z")
    source_b = _write_source(tmp_path, "b.7z")
    source_c = _write_source(tmp_path, "c.7z")

    pool.put(source_a, "001.png", b"A" * 1_500)
    time.sleep(0.01)
    pool.put(source_b, "001.png", b"B" * 1_500)
    time.sleep(0.01)
    # Touch A so B becomes the LRU candidate.
    assert pool.get(source_a, "001.png") is not None
    time.sleep(0.01)
    pool.put(source_c, "001.png", b"C" * 1_500)

    assert pool.get(source_b, "001.png") is None
    assert pool.get(source_a, "001.png") == b"A" * 1_500


def test_resize_evicts_bundles_when_budget_shrinks(tmp_path: Path) -> None:
    pool = ArchiveExtractionPool(tmp_path / "cache", max_bytes=16_000)
    sources = [_write_source(tmp_path, f"book-{i}.7z") for i in range(4)]
    for source in sources:
        pool.put(source, "001.png", b"X" * 1_500)
        time.sleep(0.005)

    pool.resize(4_000)

    assert pool.current_bytes <= 4_000
    # Oldest bundles disappear; the most-recently-written ones are preserved.
    assert pool.get(sources[0], "001.png") is None
    assert pool.get(sources[-1], "001.png") == b"X" * 1_500


def test_clear_drops_indexed_and_orphan_files(tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    pool = ArchiveExtractionPool(directory, max_bytes=4096)
    source = _write_source(tmp_path)
    pool.put(source, "001.png", b"a" * 50)

    # Drop an orphan file unrelated to the index to confirm the sweep.
    directory.mkdir(parents=True, exist_ok=True)
    orphan = directory / "orphan.bin"
    orphan.write_bytes(b"xx")

    pool.clear()

    assert pool.current_bytes == 0
    assert pool.get(source, "001.png") is None
    assert not orphan.exists()


def test_index_reconciliation_picks_up_bundles_from_previous_launch(tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    source = _write_source(tmp_path)

    seed = ArchiveExtractionPool(directory, max_bytes=4096)
    seed.put(source, "001.png", b"P" * 100)
    seed.put(source, "002.png", b"Q" * 100)

    reopened = ArchiveExtractionPool(directory, max_bytes=4096)
    assert reopened.get(source, "001.png") == b"P" * 100
    assert reopened.get(source, "002.png") == b"Q" * 100


def test_index_reconciliation_trims_to_budget_on_startup(tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    seed = ArchiveExtractionPool(directory, max_bytes=16_000)
    sources = [_write_source(tmp_path, f"book-{i}.7z") for i in range(4)]
    for source in sources:
        seed.put(source, "001.png", b"Z" * 1_500)
        time.sleep(0.005)

    tight = ArchiveExtractionPool(directory, max_bytes=4_000)
    # Triggers reconciliation + eviction down to the new budget.
    _ = tight.current_bytes

    assert tight.current_bytes <= 4_000
    assert tight.get(sources[0], "001.png") is None
    assert tight.get(sources[-1], "001.png") == b"Z" * 1_500


def test_atomic_write_does_not_leak_tmp_files_into_index(tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    pool = ArchiveExtractionPool(directory, max_bytes=4096)
    source = _write_source(tmp_path)

    pool.put(source, "001.png", b"P" * 50)

    tmp_files = list(directory.glob("*.tmp*"))
    assert tmp_files == []


def test_orphan_tmp_files_are_swept_on_startup(tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    directory.mkdir(parents=True, exist_ok=True)
    stray = directory / "deadbeef.zip.tmp"
    stray.write_bytes(b"truncated write")

    pool = ArchiveExtractionPool(directory, max_bytes=4096)
    _ = pool.current_bytes  # trigger reconciliation

    assert not stray.exists()


def test_legacy_loose_files_are_swept_on_startup(tmp_path: Path) -> None:
    # Older builds wrote ``<sha256>.<ext>`` files directly. After upgrading,
    # those files no longer fit the per-book bundle layout, so the
    # reconciliation pass cleans them up rather than leaving them stranded.
    directory = tmp_path / "cache"
    directory.mkdir(parents=True, exist_ok=True)
    legacy = directory / "deadbeef.jpg"
    legacy.write_bytes(b"old format payload")

    pool = ArchiveExtractionPool(directory, max_bytes=4096)
    _ = pool.current_bytes

    assert not legacy.exists()


def test_corrupted_bundle_is_dropped_and_returns_none_for_reads(tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    pool = ArchiveExtractionPool(directory, max_bytes=4096)
    source = _write_source(tmp_path)
    pool.put(source, "001.png", b"valid")
    bundles = list(directory.glob("*.zip"))
    assert bundles, "expected a bundle on disk"

    # Simulate a crash mid-write: corrupt the existing bundle by truncating
    # its contents.
    bundles[0].write_bytes(b"not a zip")

    assert pool.get(source, "001.png") is None
    assert not bundles[0].exists()


def test_document_cache_key_is_independent_of_source_file_metadata(tmp_path: Path) -> None:
    pool = ArchiveExtractionPool(tmp_path / "cache", max_bytes=4096)
    source = _write_source(tmp_path)
    document_cache_key = "file:stable-id"

    pool.put(document_cache_key, "001.png", b"old")

    source.write_bytes(b"fake-archive-modified")

    # Source changes are discovered by the manual library audit. Cache identity
    # is the immutable managed file id, not mutable source metadata.
    assert pool.get(document_cache_key, "001.png") == b"old"


def test_zip_pool_put_many_writes_one_bundle_with_multiple_entries(tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    pool = ArchiveExtractionPool(directory, max_bytes=4096)
    source = _write_source(tmp_path)

    pool.put_many(source, {"001.png": b"one", "002.png": b"two"})

    bundles = list(directory.glob("*.zip"))
    assert len(bundles) == 1
    with ZipFile(bundles[0]) as archive:
        assert sorted(archive.namelist()) == ["001.png", "002.png"]


def test_zip_pool_partial_build_publishes_ready_manifest_atomically(tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    source = _write_source(tmp_path)
    pool = ArchiveExtractionPool(directory, max_bytes=4096)

    pool.put_many(source, {"pages/00000000": b"one", "pages/00000001": b"two"})

    partials = list(directory.glob("*.partial.zip"))
    assert len(partials) == 1
    assert not pool.is_complete(source, 2, "depth=2")

    pool.mark_complete(source, 2, "depth=2")

    assert list(directory.glob("*.partial.zip")) == []
    published = [path for path in directory.glob("*.zip") if not path.name.endswith(".partial.zip")]
    assert len(published) == 1
    assert pool.is_complete(source, 2, "depth=2")
    assert not pool.is_complete(source, 3, "depth=2")
    assert not pool.is_complete(source, 2, "depth=3")

    reopened = ArchiveExtractionPool(directory, max_bytes=4096)
    assert reopened.is_complete(source, 2, "depth=2")
    assert reopened.get(source, "pages/00000001") == b"two"


def test_zip_pool_reconciliation_publishes_completed_crash_partial(tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    source = "file:managed-content"
    pool = ArchiveExtractionPool(directory, max_bytes=4096)
    pool.put(source, "pages/00000000", b"page")
    pool.put(
        source,
        "__joyread_ready_manifest__.json",
        json.dumps(
            {
                "schema": 3,
                "page_count": 1,
                "signature": "limits-v1",
                "identity_kind": "managed",
                "build_state": "ready",
            }
        ).encode(),
    )
    assert list(directory.glob("*.partial.zip"))

    reopened = ArchiveExtractionPool(directory, max_bytes=4096)

    assert reopened.is_complete(source, 1, "limits-v1")
    assert list(directory.glob("*.partial.zip")) == []
    assert len(list(directory.glob("*.zip"))) == 1


def test_zip_pool_replaces_an_existing_ready_manifest(tmp_path: Path) -> None:
    pool = ArchiveExtractionPool(tmp_path / "cache", max_bytes=4096)
    source = "file:managed-content"
    pool.put(source, "pages/00000000", b"page")
    pool.mark_complete(source, 1, "old-limits")

    pool.mark_complete(source, 1, "new-limits")

    assert not pool.is_complete(source, 1, "old-limits")
    assert pool.is_complete(source, 1, "new-limits")


def test_hidden_pool_page_eviction_invalidates_ready_manifest(tmp_path: Path) -> None:
    pool = HiddenImageExtractionPool(tmp_path / ".archive_image_pages", max_bytes=4096)
    source = _write_source(tmp_path)
    pool.put(source, "pages/00000000", b"page")
    pool.mark_complete(source, 1, "depth=2")

    assert pool.is_complete(source, 1, "depth=2")

    pool.resize(1)

    assert pool.get(source, "pages/00000000") is None
    assert not pool.is_complete(source, 1, "depth=2")


def test_hidden_pool_soft_budget_evicts_an_inactive_document_as_one_unit(tmp_path: Path) -> None:
    pool = HiddenImageExtractionPool(tmp_path / ".archive_image_pages", max_bytes=100)
    first = "file:first"
    second = "file:second"

    pool.put_many(first, {"pages/0": b"A" * 80, "pages/1": b"B" * 80})
    # The current document is the protected writer and may exceed the budget.
    assert pool.get(first, "pages/0") is not None
    assert pool.get(first, "pages/1") is not None

    pool.put(second, "pages/0", b"C" * 80)

    assert pool.get(first, "pages/0") is None
    assert pool.get(first, "pages/1") is None
    assert pool.get(second, "pages/0") == b"C" * 80


def test_hidden_image_pool_uses_hidden_folder_and_non_image_extension(tmp_path: Path) -> None:
    directory = tmp_path / ".archive_image_pages"
    pool = HiddenImageExtractionPool(directory, max_bytes=4096)
    source = _write_source(tmp_path)

    pool.put(source, "chapter/001.png", b"PNG-PAYLOAD")

    assert pool.get(source, "chapter/001.png") == b"PNG-PAYLOAD"
    files = [
        path
        for path in directory.rglob("*")
        if path.is_file() and path.name != ".joyread-archive-cache-schema"
    ]
    assert files
    assert all(path.suffix == ".jrcache" for path in files)
    assert all("001" not in path.name and ".png" not in path.name for path in files)
    assert directory.name.startswith(".")


def test_hidden_image_pool_clears_legacy_metadata_keyed_cache_on_upgrade(tmp_path: Path) -> None:
    directory = tmp_path / ".archive_image_pages"
    legacy_dir = directory / "legacy-book-key"
    legacy_dir.mkdir(parents=True)
    legacy_payload = legacy_dir / "legacy-page.jrcache"
    legacy_payload.write_bytes(b"legacy")

    pool = HiddenImageExtractionPool(directory, max_bytes=4096)

    assert pool.current_bytes == 0
    assert not legacy_payload.exists()
    assert (directory / ".joyread-archive-cache-schema").read_text(encoding="ascii") == "3"


def test_ephemeral_lease_is_removed_on_close(tmp_path: Path) -> None:
    pool = ArchiveExtractionPool(tmp_path / "cache", max_bytes=4096)
    lease = ArchiveCacheLease(pool, "session:reader", ArchiveCacheScope.EPHEMERAL)
    lease.put("001.png", b"temporary")

    lease.close()

    assert pool.get("session:reader", "001.png") is None


def test_ephemeral_lease_promotes_to_cross_session_content_key(tmp_path: Path) -> None:
    pool = ArchiveExtractionPool(tmp_path / "cache", max_bytes=4096)
    lease = ArchiveCacheLease(pool, "session:reader", ArchiveCacheScope.EPHEMERAL)
    lease.put_many({"001.png": b"one", "002.png": b"two"})

    assert lease.promote("external:sha256:digest")
    lease.close()

    assert pool.get("session:reader", "001.png") is None
    assert pool.get("external:sha256:digest", "001.png") == b"one"
    assert pool.get("external:sha256:digest", "002.png") == b"two"


def test_promotion_merges_missing_pages_into_a_ready_target(tmp_path: Path) -> None:
    pool = ArchiveExtractionPool(tmp_path / "cache", max_bytes=4096)
    target = "external:sha256:digest"
    pool.put(target, "pages/00000000", b"target-page")
    pool.mark_complete(target, 1, "ready-limits")
    lease = ArchiveCacheLease(pool, "session:reader", ArchiveCacheScope.EPHEMERAL)
    lease.put("pages/00000001", b"ephemeral-page")

    assert lease.promote(target)
    lease.close()

    assert pool.get(target, "pages/00000000") == b"target-page"
    assert pool.get(target, "pages/00000001") == b"ephemeral-page"
    assert pool.is_complete(target, 1, "ready-limits")


def test_failed_promotion_keeps_lease_ephemeral_for_close_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pool = ArchiveExtractionPool(tmp_path / "cache", max_bytes=4096)
    lease = ArchiveCacheLease(pool, "session:reader", ArchiveCacheScope.EPHEMERAL)
    lease.put("001.png", b"temporary")
    monkeypatch.setattr(pool, "promote", lambda _source, _target: False)

    assert lease.promote("external:sha256:digest") is False
    assert lease.scope == ArchiveCacheScope.EPHEMERAL
    assert lease.document_cache_key == "session:reader"

    lease.close()
    assert pool.get("session:reader", "001.png") is None


def test_zip_pool_promotion_rejects_symlink_target(tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    pool = ArchiveExtractionPool(directory, max_bytes=4096)
    lease = ArchiveCacheLease(pool, "session:reader", ArchiveCacheScope.EPHEMERAL)
    lease.put("001.png", b"temporary")
    target = "external:sha256:digest"
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"outside")
    target_path = directory / f"{archive_cache_storage_key(target)}.partial.zip"
    target_path.symlink_to(outside)

    assert lease.promote(target) is False
    assert outside.read_bytes() == b"outside"

    lease.close()


def test_hidden_pool_promotion_rejects_symlink_target(tmp_path: Path) -> None:
    directory = tmp_path / ".archive_image_pages"
    pool = HiddenImageExtractionPool(directory, max_bytes=4096)
    lease = ArchiveCacheLease(pool, "session:reader", ArchiveCacheScope.EPHEMERAL)
    lease.put("001.png", b"temporary")
    target = "external:sha256:digest"
    outside = tmp_path / "outside"
    outside.mkdir()
    target_path = directory / archive_cache_storage_key(target)
    target_path.symlink_to(outside, target_is_directory=True)

    assert lease.promote(target) is False
    assert tuple(outside.iterdir()) == ()

    lease.close()


def test_active_lease_may_exceed_soft_budget_until_close(tmp_path: Path) -> None:
    pool = ArchiveExtractionPool(tmp_path / "cache", max_bytes=256)
    lease = ArchiveCacheLease(pool, "external:sha256:large", ArchiveCacheScope.PERSISTENT)
    lease.put("001.png", b"x" * 1024)

    assert pool.current_bytes > pool.max_bytes

    lease.close()
    assert pool.current_bytes <= pool.max_bytes


def test_hidden_image_pool_clear_removes_nested_cache_files(tmp_path: Path) -> None:
    directory = tmp_path / ".archive_image_pages"
    pool = HiddenImageExtractionPool(directory, max_bytes=4096)
    source = _write_source(tmp_path)
    pool.put_many(source, {"001.png": b"one", "002.png": b"two"})

    pool.clear()

    assert pool.current_bytes == 0
    assert not directory.exists()
