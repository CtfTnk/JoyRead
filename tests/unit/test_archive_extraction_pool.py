"""Tests for ArchiveExtractionPool."""

from __future__ import annotations

import json
import time
from pathlib import Path
from zipfile import ZipFile

import pytest

from joyread.core.services import archive_extraction_pool as pool_module
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


@pytest.mark.parametrize("pool_type", [ArchiveExtractionPool, HiddenImageExtractionPool])
def test_persistent_oversized_bundle_survives_close_and_restart(tmp_path: Path, pool_type) -> None:  # noqa: ANN001
    directory = tmp_path / "cache"
    pool = pool_type(directory, max_bytes=256)
    lease = ArchiveCacheLease(pool, "external:sha256:large", ArchiveCacheScope.PERSISTENT)
    lease.put("001.png", b"x" * 1024)

    assert pool.current_bytes > pool.max_bytes

    lease.close()
    assert pool.current_bytes > pool.max_bytes
    assert pool.get("external:sha256:large", "001.png") == b"x" * 1024

    reopened = pool_type(directory, max_bytes=256)
    assert reopened.current_bytes > reopened.max_bytes
    assert reopened.get("external:sha256:large", "001.png") == b"x" * 1024


@pytest.mark.parametrize("pool_type", [ArchiveExtractionPool, HiddenImageExtractionPool])
def test_competing_writer_evicts_inactive_oversized_bundle(tmp_path: Path, pool_type) -> None:  # noqa: ANN001
    pool = pool_type(tmp_path / "cache", max_bytes=256)
    lease = ArchiveCacheLease(pool, "external:sha256:large", ArchiveCacheScope.PERSISTENT)
    lease.put("001.png", b"x" * 1024)
    lease.close()

    pool.put("external:sha256:next", "001.png", b"next")

    assert pool.get("external:sha256:large", "001.png") is None
    assert pool.get("external:sha256:next", "001.png") == b"next"
    assert pool.current_bytes <= pool.max_bytes


@pytest.mark.parametrize("pool_type", [ArchiveExtractionPool, HiddenImageExtractionPool])
def test_explicit_resize_finishes_when_active_oversized_lease_closes(tmp_path: Path, pool_type) -> None:  # noqa: ANN001
    pool = pool_type(tmp_path / "cache", max_bytes=4096)
    lease = ArchiveCacheLease(pool, "external:sha256:large", ArchiveCacheScope.PERSISTENT)
    lease.put("001.png", b"x" * 1024)

    pool.resize(256)
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


def test_contains_many_verifies_without_reading_payloads(tmp_path: Path) -> None:
    """Verification must not pull a whole book into memory."""

    pool = ArchiveExtractionPool(tmp_path, 64 * 1024 * 1024)
    key = "file:book"
    assert pool.put_many(key, {"a.jpg": b"x" * 2048, "b.jpg": b"y" * 2048})

    reads: list[str] = []
    original_get = ArchiveExtractionPool.get

    def tracking_get(self, document_cache_key, entry_name):  # noqa: ANN001, ANN202
        reads.append(entry_name)
        return original_get(self, document_cache_key, entry_name)

    ArchiveExtractionPool.get = tracking_get  # type: ignore[method-assign]
    try:
        present = pool.contains_many(key, ("a.jpg", "b.jpg", "missing.jpg"))
    finally:
        ArchiveExtractionPool.get = original_get  # type: ignore[method-assign]

    assert present == frozenset({"a.jpg", "b.jpg"})
    assert reads == [], "contains_many must not read entry payloads"


def test_put_many_reports_failure_so_callers_do_not_publish(tmp_path: Path) -> None:
    """A silent write failure would publish a bundle that is missing pages."""

    pool = ArchiveExtractionPool(tmp_path, 64 * 1024 * 1024)
    key = "file:book"
    assert pool.put_many(key, {"a.jpg": b"x" * 1024}) is True

    # Replace the bundle with a directory so the append genuinely fails.
    bundle = next(tmp_path.glob("*.partial.zip"))
    bundle.unlink()
    bundle.mkdir()

    assert pool.put_many(key, {"b.jpg": b"y" * 1024}) is False


def test_mark_complete_reports_whether_it_published(tmp_path: Path) -> None:
    pool = ArchiveExtractionPool(tmp_path, 64 * 1024 * 1024)
    key = "file:book"
    pool.put_many(key, {"a.jpg": b"x" * 1024})

    assert pool.mark_complete(key, 1, "sig") is True
    assert pool.is_complete(key, 1, "sig")
    # Publishing twice is not a failure; the bundle is already final.
    assert pool.mark_complete(key, 1, "sig") is True


def test_a_failed_write_keeps_mark_complete_from_publishing(tmp_path: Path) -> None:
    """The whole point of checkable writes: never publish a short bundle."""

    pool = ArchiveExtractionPool(tmp_path, 64 * 1024 * 1024)
    key = "file:book"
    pool.put_many(key, {"a.jpg": b"x" * 1024})
    bundle = next(tmp_path.glob("*.partial.zip"))
    bundle.unlink()
    bundle.mkdir()

    assert pool.mark_complete(key, 2, "sig") is False
    assert not pool.is_complete(key, 2, "sig")


def test_publish_complete_refuses_a_document_with_missing_entries(tmp_path: Path) -> None:
    """Per-group checks cannot see the whole book; publication has to."""

    pool = ArchiveExtractionPool(tmp_path, 8 * 1024 * 1024)
    pool.put_many("file:book", {"a": b"1", "b": b"2"})

    assert pool.publish_complete("file:book", ("a", "b", "c"), 3, "sig") is False
    assert pool.is_complete("file:book", 3, "sig") is False
    assert pool.publish_complete("file:book", ("a", "b"), 2, "sig") is True
    assert pool.is_complete("file:book", 2, "sig") is True


def test_publish_complete_detects_a_bundle_reset_between_writes(tmp_path: Path) -> None:
    pool = ArchiveExtractionPool(tmp_path, 8 * 1024 * 1024)
    pool.put_many("file:book", {"a": b"1", "b": b"2"})
    # An append that finds the bundle unreadable recreates it from scratch.
    next(tmp_path.glob("*.partial.zip")).write_bytes(b"not a zip")
    pool.put_many("file:book", {"c": b"3"})

    assert pool.publish_complete("file:book", ("a", "b", "c"), 3, "sig") is False
    assert pool.is_complete("file:book", 3, "sig") is False


def test_a_failed_publish_rename_still_reports_one_consistent_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manifest, not the filename, is the authoritative completion state.

    Renaming to the published name only helps the startup scan recognise a
    finished bundle. When it fails the document is still complete and readable
    through the indexed partial bundle, and reconciliation renames it on the
    next launch, so reporting failure would force a re-conversion that hits the
    same error forever.
    """

    pool = ArchiveExtractionPool(tmp_path, 8 * 1024 * 1024)
    pool.put_many("file:book", {"a": b"1"})
    real_replace = pool_module.os.replace

    def failing_replace(src, dst):  # noqa: ANN001, ANN202
        if str(dst).endswith(".zip") and not str(dst).endswith(".partial.zip"):
            raise OSError("rename refused")
        return real_replace(src, dst)

    monkeypatch.setattr(pool_module.os, "replace", failing_replace)

    published = pool.publish_complete("file:book", ("a",), 1, "sig")

    assert published is True
    assert pool.is_complete("file:book", 1, "sig") is True
    assert pool.get("file:book", "a") == b"1"


def test_republishing_an_identical_manifest_does_not_rewrite_the_bundle(
    tmp_path: Path,
) -> None:
    """A zip entry cannot be replaced in place, so a rewrite copies the book."""

    pool = ArchiveExtractionPool(tmp_path, 8 * 1024 * 1024)
    pool.put_many("file:book", {"a": b"1"})
    assert pool.publish_complete("file:book", ("a",), 1, "sig") is True
    bundle = next(tmp_path.glob("m-*.zip"))
    before = bundle.stat().st_mtime_ns

    assert pool.publish_complete("file:book", ("a",), 1, "sig") is True

    assert bundle.stat().st_mtime_ns == before
    with ZipFile(bundle) as archive:
        manifests = [name for name in archive.namelist() if "manifest" in name]
    assert len(manifests) == 1


def test_hidden_pool_accounts_for_files_a_failed_batch_already_wrote(
    tmp_path: Path,
) -> None:
    """A partly-installed batch must not leave unindexed, unevictable files."""

    pool = HiddenImageExtractionPool(tmp_path / "hidden", 8 * 1024 * 1024)
    real_write = pool._write_entry  # noqa: SLF001
    attempts: list[str] = []

    def failing_write(book_key, entry_name, data):  # noqa: ANN001, ANN202
        attempts.append(entry_name)
        if len(attempts) > 2:
            return None
        return real_write(book_key, entry_name, data)

    pool._write_entry = failing_write  # type: ignore[method-assign]  # noqa: SLF001

    written = pool.put_many("file:book", {"a": b"1234", "b": b"5678", "c": b"9012"})

    assert written is False
    assert pool.current_bytes == 8, "installed files must be counted"
    assert pool.contains_many("file:book", ("a", "b", "c")) == frozenset({"a", "b"})


def test_hidden_pool_publish_requires_every_entry(tmp_path: Path) -> None:
    pool = HiddenImageExtractionPool(tmp_path / "hidden", 8 * 1024 * 1024)
    pool.put_many("file:book", {"a": b"1", "b": b"2"})

    assert pool.publish_complete("file:book", ("a", "b", "c"), 3, "sig") is False
    assert pool.is_complete("file:book", 3, "sig") is False
    assert pool.publish_complete("file:book", ("a", "b"), 2, "sig") is True
    assert pool.is_complete("file:book", 2, "sig") is True
