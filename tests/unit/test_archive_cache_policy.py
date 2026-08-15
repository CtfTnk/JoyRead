"""The one document cache policy shared by foreground reads and warmup."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import py7zr
import pytest
from PIL import Image

from joyread.core.archive import ArchiveImageService, ArchiveOpenLimits
from joyread.core.archive.models import ArchiveCachePolicy, ArchiveConversionStatus
from joyread.core.archive.records import ArchiveSource, PageRecord
from joyread.core.archive.session import ArchiveImageSession
from joyread.core.reader.session_service import ReaderSessionService
from joyread.core.services.archive_cache_lease import ArchiveCacheLease, ArchiveCacheScope
from joyread.core.services.archive_extraction_pool import (
    ArchiveExtractionPool,
    HiddenImageExtractionPool,
)

PAGE = b"\xff\xd8" + b"payload" * 64
_DEFAULT_BULK_EXTRACT = object()


def _png_bytes(size: tuple[int, int], color: str = "#ffffff") -> bytes:
    image = Image.new("RGB", size, color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _write_7z(path: Path, pages: int = 8) -> None:
    with py7zr.SevenZipFile(path, "w") as archive:
        for index in range(pages):
            archive.writestr(_png_bytes((60 + index, 40)), f"{index:03d}.png")


def _plan(
    tmp_path: Path,
    *,
    pages: int = 4,
    suffix: str = ".7z",
    sizes: list[int | None] | None = None,
    pool_bytes: int = 64 * 1024 * 1024,
    bulk_extract=_DEFAULT_BULK_EXTRACT,  # noqa: ANN001
    limits: ArchiveOpenLimits | None = None,
    password: str | None = None,
    allow_persistent_cache: bool = True,
    with_lease: bool = True,
    sources: list[ArchiveSource] | None = None,
) -> tuple[ArchiveImageSession, ArchiveExtractionPool]:
    pool = ArchiveExtractionPool(tmp_path / "pool", pool_bytes)
    lease = (
        ArchiveCacheLease(pool, "file:book", ArchiveCacheScope.PERSISTENT)
        if with_lease
        else None
    )
    archive = tmp_path / f"book{suffix}"
    archive.write_bytes(b"stub")
    default_source = ArchiveSource(
        label=archive.name,
        suffix=suffix,
        path=archive,
        allow_persistent_cache=allow_persistent_cache,
        requires_sequential_warmup=suffix in {".7z", ".cb7", ".rar", ".cbr"},
    )
    records = [
        PageRecord(
            display_path=f"p{i}.jpg",
            source=(sources[i] if sources else default_source),
            name=f"p{i}.jpg",
            password=password,
            size=(sizes[i] if sizes else len(PAGE)),
        )
        for i in range(pages)
    ]
    session = ArchiveImageSession(
        records,
        lambda *_a, **_k: {},
        bulk_extract=(
            (lambda *_a, **_k: None)
            if bulk_extract is _DEFAULT_BULK_EXTRACT
            else bulk_extract
        ),
        cache_lease=lease,
        cache_signature="sig",
        limits=limits,
    )
    return session, pool


def test_a_fitting_expensive_document_is_convertible(tmp_path: Path) -> None:
    session, _pool = _plan(tmp_path)

    assert session.cache_plan.policy is ArchiveCachePolicy.BULK_CONVERT
    assert session.cache_plan.allows_background_warmup
    assert session.cache_plan.allows_persistent_page_writes
    assert session.requires_sequential_warmup


def test_a_cheap_container_never_touches_the_extraction_cache(tmp_path: Path) -> None:
    session, _pool = _plan(tmp_path, suffix=".cbz")

    assert session.cache_plan.policy is ArchiveCachePolicy.DIRECT
    assert not session.cache_plan.allows_background_warmup


def test_a_document_larger_than_the_cache_is_on_demand_only(tmp_path: Path) -> None:
    session, _pool = _plan(tmp_path, sizes=[4 * 1024 * 1024] * 4, pool_bytes=1024 * 1024)

    assert session.cache_plan.policy is ArchiveCachePolicy.ON_DEMAND_ONLY
    assert session.cache_plan.reason == "larger_than_cache"
    assert not session.cache_plan.allows_background_warmup
    assert not session.cache_plan.allows_persistent_page_writes
    assert not session.requires_sequential_warmup


def test_a_document_with_unplannable_sizes_is_on_demand_only(tmp_path: Path) -> None:
    session, _pool = _plan(tmp_path, sizes=[1024, None, 1024, 1024])

    assert session.cache_plan.policy is ArchiveCachePolicy.ON_DEMAND_ONLY
    assert session.cache_plan.reason == "unknown_page_sizes"
    assert not session.requires_sequential_warmup


def test_a_document_larger_than_the_operation_budget_is_on_demand_only(
    tmp_path: Path,
) -> None:
    session, _pool = _plan(
        tmp_path,
        sizes=[1024] * 4,
        limits=ArchiveOpenLimits(max_operation_bytes=3072),
    )

    assert session.cache_plan.policy is ArchiveCachePolicy.ON_DEMAND_ONLY
    assert session.cache_plan.reason == "larger_than_operation_budget"
    assert not session.cache_plan.allows_background_warmup
    assert not session.cache_plan.allows_persistent_page_writes
    assert not session.requires_sequential_warmup


def test_an_oversized_document_is_refused_before_the_capability_test(
    tmp_path: Path,
) -> None:
    """Order matters: refusing bulk and then filling the same cache page by
    page through the sequential path would spend more to reach the same place.
    """

    session, _pool = _plan(
        tmp_path,
        sizes=[4 * 1024 * 1024] * 4,
        pool_bytes=1024 * 1024,
        bulk_extract=None,
    )
    # ``bulk_extract=None`` alone would mean SEQUENTIAL_WARM.
    session_without_budget_pressure, _ = _plan(tmp_path, bulk_extract=None)
    assert (
        session_without_budget_pressure.cache_plan.policy
        is ArchiveCachePolicy.SEQUENTIAL_WARM
    )

    assert session.cache_plan.policy is ArchiveCachePolicy.ON_DEMAND_ONLY
    assert not session.requires_sequential_warmup


def test_encrypted_pages_are_on_demand_only(tmp_path: Path) -> None:
    """Decrypted bytes must never become a durable cache product, and warming
    a document that can cache nothing would re-read it for no benefit."""

    session, _pool = _plan(tmp_path, password="secret")

    assert session.cache_plan.policy is ArchiveCachePolicy.ON_DEMAND_ONLY
    assert session.cache_plan.reason == "no_cacheable_page"
    assert not session.requires_sequential_warmup


def test_a_session_without_a_cache_is_on_demand_only(tmp_path: Path) -> None:
    session, _pool = _plan(tmp_path, with_lease=False)

    assert session.cache_plan.policy is ArchiveCachePolicy.ON_DEMAND_ONLY
    assert session.cache_plan.reason == "no_document_cache"
    assert not session.requires_sequential_warmup


def test_a_nested_source_falls_back_to_sequential_warm(tmp_path: Path) -> None:
    nested = ArchiveSource(label="nested.7z", suffix=".7z", data=b"nested")
    session, _pool = _plan(tmp_path, sources=[nested] * 4)

    assert session.cache_plan.policy is ArchiveCachePolicy.SEQUENTIAL_WARM
    assert session.cache_plan.reason == "not_path_backed"
    assert session.cache_plan.allows_persistent_page_writes


def test_the_foreground_never_grows_a_cache_it_may_not_complete(tmp_path: Path) -> None:
    """The persistent-pool non-growth guarantee, through the real service.

    An on-demand-only document must still read, and every page must still be
    correct -- it just must not accumulate into a bundle that can never be
    completed or evicted by its own progress.
    """

    archive_path = tmp_path / "huge.7z"
    _write_7z(archive_path, pages=8)
    # A budget far below the declared page total forces ON_DEMAND_ONLY.
    pool = ArchiveExtractionPool(tmp_path / "pool", 512)
    service = ArchiveImageService(extraction_pool=pool)
    session = service.open(
        archive_path,
        document_cache_key="file:huge",
        allow_persistent_cache=True,
    )
    try:
        assert session.cache_plan.policy is ArchiveCachePolicy.ON_DEMAND_ONLY

        pages = session.get_pages(range(8))

        assert all(page is not None for page in pages), "foreground reads must still work"
        assert pool.current_bytes == 0, "no persistent bundle may be built"
        assert list((pool.directory or tmp_path).glob("*.zip")) == []
    finally:
        session.close()


def test_a_stale_partial_bundle_is_reclaimed_for_an_on_demand_document(
    tmp_path: Path,
) -> None:
    """Earlier builds warmed everything, so one can already be on disk."""

    archive_path = tmp_path / "huge.7z"
    _write_7z(archive_path, pages=8)
    pool = ArchiveExtractionPool(tmp_path / "pool", 512)
    # Whatever an older build left behind, under this document's identity.
    pool.put_many("file:huge", {"pages/stale/00000000": b"x" * 512})
    assert pool.current_bytes > 0

    service = ArchiveImageService(extraction_pool=pool)
    reader_service = ReaderSessionService(service)
    reader_service.warm_disk_cache(
        archive_path,
        document_cache_key="file:huge",
        allow_persistent_cache=True,
    )

    assert pool.current_bytes == 0, "an unfinishable partial bundle must be reclaimed"


def test_a_published_bundle_is_never_reclaimed(tmp_path: Path) -> None:
    """A complete cache is readable and worth keeping even under a budget that
    would no longer allow building it."""

    pool = ArchiveExtractionPool(tmp_path / "pool", 4096)
    pool.put_many("file:book", {"a": b"x" * 64})
    assert pool.publish_complete("file:book", ("a",), 1, "sig") is True
    before = pool.current_bytes

    assert pool.purge_unpublished("file:book") is False
    assert pool.current_bytes == before
    assert pool.get("file:book", "a") == b"x" * 64


def test_a_bundle_published_under_other_limits_is_never_reclaimed(
    tmp_path: Path,
) -> None:
    """The book key ignores the limits snapshot, so a session with a different
    signature must not read another snapshot's finished product as "partial".

    Change a depth limit, reopen the same book, and the new session's page
    count and signature no longer match the manifest -- but that manifest still
    describes a complete, readable cache for whoever is using the old limits.
    """

    pool = ArchiveExtractionPool(tmp_path / "pool", 1 << 20)
    pool.put_many("file:book", {"pages/aaaa/00000000": b"x" * 64})
    assert pool.publish_complete(
        "file:book", ("pages/aaaa/00000000",), 1, "limits-A"
    ) is True
    before = pool.current_bytes

    # A second reader on different limits: more pages, a different signature.
    assert pool.is_complete("file:book", 9, "limits-B") is False
    assert pool.purge_unpublished("file:book") is False

    assert pool.current_bytes == before
    assert pool.get("file:book", "pages/aaaa/00000000") == b"x" * 64


def test_purge_unpublished_drops_a_partial_bundle(tmp_path: Path) -> None:
    pool = ArchiveExtractionPool(tmp_path / "pool", 1 << 20)
    pool.put_many("file:book", {"a": b"x" * 64})

    assert pool.purge_unpublished("file:book") is True
    assert pool.current_bytes == 0
    assert pool.purge_unpublished("file:book") is False


def test_purge_unpublished_drops_an_unreadable_bundle(tmp_path: Path) -> None:
    """A corrupt bundle cannot serve a page, so it is not a published product."""

    pool = ArchiveExtractionPool(tmp_path / "pool", 1 << 20)
    pool.put_many("file:book", {"a": b"x" * 64})
    next((pool.directory or tmp_path).glob("*.zip")).write_bytes(b"not a zip")

    assert pool.purge_unpublished("file:book") is True
    assert pool.current_bytes == 0


def test_hidden_pool_keeps_a_bundle_published_under_other_limits(
    tmp_path: Path,
) -> None:
    pool = HiddenImageExtractionPool(tmp_path / "hidden", 1 << 20)
    pool.put_many("file:book", {"a": b"x" * 64})
    assert pool.publish_complete("file:book", ("a",), 1, "limits-A") is True

    assert pool.purge_unpublished("file:book") is False
    assert pool.get("file:book", "a") == b"x" * 64

    pool.put_many("file:other", {"b": b"y" * 64})
    assert pool.purge_unpublished("file:other") is True
    assert pool.get("file:other", "b") is None


def test_conversion_reports_on_demand_only_rather_than_a_capability_gap(
    tmp_path: Path,
) -> None:
    session, _pool = _plan(tmp_path, sizes=[4 * 1024 * 1024] * 4, pool_bytes=1024 * 1024)

    result = session.convert_to_cache()

    assert result.status is ArchiveConversionStatus.ON_DEMAND_ONLY
    assert not result.is_published


def test_a_backend_that_cannot_represent_a_member_degrades_to_unsupported(
    tmp_path: Path,
) -> None:
    """A listfile cannot carry a newline, and the backend says so. That is a
    capability gap, not a failure: bounded sequential warming may still run."""

    from joyread.core.archive.formats.seven_zip_command import MemberNameNotRepresentable

    def refusing_extract(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise MemberNameNotRepresentable("member name contains a line break")

    session, _pool = _plan(tmp_path, bulk_extract=refusing_extract)
    assert session.cache_plan.policy is ArchiveCachePolicy.BULK_CONVERT

    result = session.convert_to_cache()

    assert result.status is ArchiveConversionStatus.UNSUPPORTED
    assert result.reason == "backend_cannot_represent"


def test_a_closing_session_reports_failure_not_a_capability_gap(tmp_path: Path) -> None:
    """Lifecycle teardown must not send the warmup down a slower path."""

    session, _pool = _plan(tmp_path)
    session.close()

    result = session.convert_to_cache()

    assert result.status is ArchiveConversionStatus.FAILED
    assert result.reason == "session_closing"


@pytest.mark.parametrize("policy_pages", [1, 4])
def test_every_policy_value_answers_both_capability_questions(
    tmp_path: Path,
    policy_pages: int,
) -> None:
    session, _pool = _plan(tmp_path, pages=policy_pages)
    plan = session.cache_plan

    assert isinstance(plan.allows_background_warmup, bool)
    assert isinstance(plan.allows_persistent_page_writes, bool)
    assert plan.declared_page_bytes == policy_pages * len(PAGE)


def test_a_cache_that_can_store_nothing_is_on_demand_only(tmp_path: Path) -> None:
    """A zero budget is the strongest reason to refuse, not a reason to skip.

    Reading `0` as "no budget configured" sent a whole-archive extraction to a
    cache that cannot store one byte of it: the conversion ran in full, every
    write failed, and the FAILED result then also blocked the bounded path.
    """

    archive_path = tmp_path / "book.7z"
    _write_7z(archive_path, pages=6)
    # The service's own "no extraction pool configured" default.
    service = ArchiveImageService()
    extractions: list[int] = []
    real = service._seven_zip_backend.extract_members  # noqa: SLF001

    def counting(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        extractions.append(1)
        return real(*args, **kwargs)

    service._seven_zip_backend.extract_members = counting  # noqa: SLF001

    session = service.open(
        archive_path,
        document_cache_key="file:book",
        allow_persistent_cache=True,
    )
    try:
        assert session.cache_plan.policy is ArchiveCachePolicy.ON_DEMAND_ONLY
        assert session.cache_plan.reason == "no_cache_budget"
    finally:
        session.close()

    ReaderSessionService(service).warm_disk_cache(
        archive_path,
        document_cache_key="file:book",
        allow_persistent_cache=True,
    )

    assert extractions == [], "nothing may be extracted for a cache that stores nothing"


def test_a_zero_budget_pool_with_a_directory_is_also_on_demand_only(
    tmp_path: Path,
) -> None:
    session, _pool = _plan(tmp_path, pool_bytes=0)

    assert session.cache_plan.policy is ArchiveCachePolicy.ON_DEMAND_ONLY
    assert session.cache_plan.reason == "no_cache_budget"
