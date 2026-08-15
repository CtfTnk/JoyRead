from __future__ import annotations

from pathlib import Path

import pytest

from joyread.core.archive.errors import ArchiveCancelled
from joyread.core.archive.models import ArchiveCachePolicy, ArchiveConversionStatus
from joyread.core.archive.records import ArchiveSource, PageRecord
from joyread.core.archive.session import ArchiveImageSession
from joyread.core.services.archive_cache_lease import ArchiveCacheLease, ArchiveCacheScope
from joyread.core.services.archive_extraction_pool import ArchiveExtractionPool

PAGE = b"\xff\xd8" + b"payload" * 64


def _source(tmp_path: Path, *, suffix: str = ".7z", data: bytes | None = None) -> ArchiveSource:
    archive = tmp_path / f"book{suffix}"
    archive.write_bytes(b"stub")
    return ArchiveSource(
        label=archive.name,
        suffix=suffix,
        path=None if data is not None else archive,
        data=data,
    )


def _session(
    tmp_path: Path,
    *,
    pages: int = 5,
    bulk_extract=None,  # noqa: ANN001
    sources: list[ArchiveSource] | None = None,
    sizes: list[int | None] | None = None,
    pool_bytes: int = 64 * 1024 * 1024,
) -> tuple[ArchiveImageSession, ArchiveCacheLease, ArchiveExtractionPool]:
    pool = ArchiveExtractionPool(tmp_path / "pool", pool_bytes)
    lease = ArchiveCacheLease(pool, "file:book", ArchiveCacheScope.PERSISTENT)
    source = sources[0] if sources else _source(tmp_path)
    records = [
        PageRecord(
            display_path=f"p{i}.jpg",
            source=(sources[i] if sources else source),
            name=f"p{i}.jpg",
            password=None,
            size=(sizes[i] if sizes else len(PAGE)),
        )
        for i in range(pages)
    ]
    session = ArchiveImageSession(
        records,
        lambda *_a, **_k: {},
        bulk_extract=bulk_extract,
        cache_lease=lease,
        cache_signature="sig",
    )
    return session, lease, pool


def _staging_extract(*, omit: set[str] | None = None, payload: bytes = PAGE):  # noqa: ANN202
    omitted = omit or set()

    def extract(source, members, destination, password, **_kwargs):  # noqa: ANN001, ANN002
        destination.mkdir(parents=True, exist_ok=True)
        for name in members:
            if name in omitted:
                continue
            (destination / name).write_bytes(payload)

    return extract


def test_conversion_publishes_the_whole_document(tmp_path: Path) -> None:
    session, lease, _pool = _session(tmp_path, bulk_extract=_staging_extract())

    result = session.convert_to_cache()

    assert result.status is ArchiveConversionStatus.PUBLISHED
    assert result.is_published
    assert lease.is_complete(5, "sig")
    assert len(lease.contains_many(tuple(session._cache_page_key(i) for i in range(5)))) == 5  # noqa: SLF001


def test_a_missing_staged_page_is_never_published(tmp_path: Path) -> None:
    """7-Zip exits 0 for absent members, so the page set decides success."""

    session, lease, _pool = _session(tmp_path, bulk_extract=_staging_extract(omit={"p3.jpg"}))

    result = session.convert_to_cache()

    assert result.status is ArchiveConversionStatus.FAILED
    assert result.reason == "staged_page_missing"
    assert not lease.is_complete(5, "sig")


def test_a_failed_cache_write_is_never_published(tmp_path: Path) -> None:
    session, lease, pool = _session(tmp_path, bulk_extract=_staging_extract())
    pool.put_many = lambda *_a, **_k: False  # type: ignore[method-assign]

    result = session.convert_to_cache()

    assert result.status is ArchiveConversionStatus.FAILED
    assert result.reason == "cache_write_failed"
    assert not lease.is_complete(5, "sig")


def test_a_bundle_reset_between_groups_is_caught_before_publishing(tmp_path: Path) -> None:
    """The defect: per-group checks passed while earlier groups were gone.

    An append that finds the bundle unreadable recreates it from scratch, so a
    conversion verified group by group could publish a book holding only its
    last group. Verification has to cover the whole document atomically with
    the publish.
    """

    session, lease, pool = _session(tmp_path, pages=20, bulk_extract=_staging_extract())
    real_put_many = pool.put_many
    calls: list[int] = []

    def resetting_put_many(key, payloads):  # noqa: ANN001, ANN202
        calls.append(len(payloads))
        if len(calls) == 2:
            # Whatever the first group wrote disappears: eviction, a corrupt
            # bundle recreated by the next append, an external cache wipe.
            for bundle in (pool.directory or tmp_path).glob("*.zip"):
                bundle.unlink()
        return real_put_many(key, payloads)

    pool.put_many = resetting_put_many  # type: ignore[method-assign]

    result = session.convert_to_cache()

    assert len(calls) >= 2, "the book must be written in more than one group"
    assert result.status is ArchiveConversionStatus.FAILED
    assert result.reason == "document_unverified"
    assert not lease.is_complete(20, "sig")


def test_direct_conversion_blocks_unpublished_cleanup_between_groups(tmp_path: Path) -> None:
    """The session guard must work even without the warmup coordinator."""

    session, lease, pool = _session(
        tmp_path,
        pages=20,
        bulk_extract=_staging_extract(),
    )
    observer = ArchiveCacheLease(pool, "file:book", ArchiveCacheScope.PERSISTENT)
    real_put_many = pool.put_many
    purge_results: list[bool] = []

    def probing_put_many(key, payloads):  # noqa: ANN001, ANN202
        written = real_put_many(key, payloads)
        if not purge_results:
            purge_results.append(observer.purge_unpublished())
        return written

    pool.put_many = probing_put_many  # type: ignore[method-assign]

    result = session.convert_to_cache()

    assert purge_results == [False]
    assert result.status is ArchiveConversionStatus.PUBLISHED
    assert lease.is_complete(20, "sig")


def test_staged_files_survive_until_the_cache_confirms_them(tmp_path: Path) -> None:
    """Deleting staging before verification would lose the only other copy."""

    seen: list[int] = []

    session, _lease, pool = _session(tmp_path, pages=40, bulk_extract=_staging_extract())
    real_contains = pool.contains_many

    def counting_contains(key, names):  # noqa: ANN001, ANN202
        # Every staged file for this group must still exist at verify time.
        seen.append(len(names))
        return real_contains(key, names)

    pool.contains_many = counting_contains  # type: ignore[method-assign]

    assert session.convert_to_cache().is_published
    assert seen, "verification never ran"
    assert max(seen) <= 16, "groups must stay bounded by item count"


def test_a_page_over_the_byte_target_is_written_on_its_own(tmp_path: Path) -> None:
    """The byte bound is a grouping target; one page is indivisible.

    ``put_many`` carries whole payloads, so a page larger than the target
    cannot be split. It gets a group to itself instead of riding along with
    others, which keeps peak conversion memory at max(target, largest page).
    """

    big = b"\xff\xd8" + b"x" * (20 * 1024 * 1024)
    small = b"\xff\xd8" + b"y" * 1024

    def extract(source, members, destination, password, **_kwargs):  # noqa: ANN001, ANN002
        destination.mkdir(parents=True, exist_ok=True)
        for name in members:
            (destination / name).write_bytes(big if name == "p2.jpg" else small)

    session, _lease, pool = _session(tmp_path, pages=5, bulk_extract=extract)
    groups: list[int] = []
    real_put_many = pool.put_many

    def recording_put_many(key, payloads):  # noqa: ANN001, ANN202
        groups.append(sum(len(value) for value in payloads.values()))
        return real_put_many(key, payloads)

    pool.put_many = recording_put_many  # type: ignore[method-assign]

    assert session.convert_to_cache().is_published
    oversized = [size for size in groups if size > 16 * 1024 * 1024]
    assert len(oversized) == 1, groups
    assert oversized[0] == len(big), "the oversized page must travel alone"


def test_one_member_listed_twice_publishes_both_pages(tmp_path: Path) -> None:
    """A staged file is deleted once its group is confirmed, so both cache
    keys for a duplicated member have to be written from the same group."""

    pool = ArchiveExtractionPool(tmp_path / "pool", 8 * 1024 * 1024)
    lease = ArchiveCacheLease(pool, "file:book", ArchiveCacheScope.PERSISTENT)
    source = _source(tmp_path)
    records = [
        PageRecord(display_path=f"p{i}.jpg", source=source, name=name, password=None, size=len(PAGE))
        for i, name in enumerate(("a.jpg", "b.jpg", "a.jpg"))
    ]
    session = ArchiveImageSession(
        records,
        lambda *_a, **_k: {},
        bulk_extract=_staging_extract(),
        cache_lease=lease,
        cache_signature="sig",
    )

    assert session.convert_to_cache().is_published
    assert lease.is_complete(3, "sig")
    keys = tuple(session._cache_page_key(i) for i in range(3))  # noqa: SLF001
    assert lease.contains_many(keys) == frozenset(keys)


def test_conversion_is_skipped_for_multiple_sources(tmp_path: Path) -> None:
    (tmp_path / "b").mkdir(exist_ok=True)
    others = [_source(tmp_path), _source(tmp_path / "b")]
    sources = [others[0], others[0], others[1], others[0], others[0]]
    session, lease, _pool = _session(tmp_path, sources=sources, bulk_extract=_staging_extract())

    result = session.convert_to_cache()

    assert session.cache_plan.policy is ArchiveCachePolicy.SEQUENTIAL_WARM
    assert result.status is ArchiveConversionStatus.UNSUPPORTED
    assert result.reason == "multiple_sources"
    assert not lease.is_complete(5, "sig")


def test_conversion_is_unsupported_without_a_bulk_backend(tmp_path: Path) -> None:
    session, _lease, _pool = _session(tmp_path, bulk_extract=None)

    assert session.cache_plan.policy is ArchiveCachePolicy.SEQUENTIAL_WARM
    assert session.convert_to_cache().status is ArchiveConversionStatus.UNSUPPORTED


def test_a_book_larger_than_the_cache_is_on_demand_only(tmp_path: Path) -> None:
    """One book must not evict every other bundle and hold the pool over budget."""

    session, _lease, _pool = _session(
        tmp_path,
        sizes=[4 * 1024 * 1024] * 5,
        pool_bytes=1024 * 1024,
        bulk_extract=_staging_extract(),
    )

    assert session.cache_plan.policy is ArchiveCachePolicy.ON_DEMAND_ONLY
    assert session.cache_plan.reason == "larger_than_cache"
    result = session.convert_to_cache()

    assert result.status is ArchiveConversionStatus.ON_DEMAND_ONLY


def test_an_unknown_page_size_is_never_treated_as_zero(tmp_path: Path) -> None:
    """A missing declared size shrinks both the pool test and the output cap."""

    session, _lease, _pool = _session(
        tmp_path,
        sizes=[4 * 1024 * 1024, None, 4 * 1024 * 1024, 4 * 1024 * 1024, 4 * 1024 * 1024],
        pool_bytes=1024 * 1024,
        bulk_extract=_staging_extract(),
    )

    assert session.cache_plan.policy is ArchiveCachePolicy.ON_DEMAND_ONLY
    assert session.cache_plan.reason == "unknown_page_sizes"
    assert session.convert_to_cache().status is ArchiveConversionStatus.ON_DEMAND_ONLY


def test_the_output_cap_never_derives_from_a_partial_declared_total(tmp_path: Path) -> None:
    session, _lease, _pool = _session(
        tmp_path,
        sizes=[8 * 1024 * 1024, None, 8 * 1024 * 1024],
        pages=3,
        pool_bytes=512 * 1024 * 1024,
        bulk_extract=_staging_extract(),
    )

    # 16 MiB of known sizes must not become the ceiling for a book whose real
    # size is unknown; the operation and pool budgets are the honest bounds.
    assert session._conversion_output_cap() > 16 * 1024 * 1024 * 2  # noqa: SLF001


def test_cancellation_stops_conversion_without_publishing(tmp_path: Path) -> None:
    session, lease, _pool = _session(tmp_path, pages=40, bulk_extract=_staging_extract())

    with pytest.raises(ArchiveCancelled):
        session.convert_to_cache(is_cancelled=lambda: True)

    assert not lease.is_complete(40, "sig")


def test_cancellation_just_before_publication_stops_the_publish(tmp_path: Path) -> None:
    """Everything is staged and written; the caller withdraws at the last tick."""

    ticks: list[int] = []

    def is_cancelled() -> bool:
        ticks.append(1)
        # Stay alive through the per-page walk, then withdraw.
        return len(ticks) > 5

    session, lease, _pool = _session(tmp_path, pages=5, bulk_extract=_staging_extract())

    with pytest.raises(ArchiveCancelled):
        session.convert_to_cache(is_cancelled=is_cancelled)

    assert not lease.is_complete(5, "sig")


def test_an_already_complete_cache_reports_success_without_extracting(tmp_path: Path) -> None:
    calls: list[int] = []

    def extract(source, members, destination, password, **_kwargs):  # noqa: ANN001, ANN002
        calls.append(1)
        destination.mkdir(parents=True, exist_ok=True)
        for name in members:
            (destination / name).write_bytes(PAGE)

    session, _lease, _pool = _session(tmp_path, bulk_extract=extract)
    assert session.convert_to_cache().status is ArchiveConversionStatus.PUBLISHED
    second = session.convert_to_cache()

    assert second.status is ArchiveConversionStatus.ALREADY_PUBLISHED
    assert second.is_published
    assert len(calls) == 1, "a converted document must not be converted again"
