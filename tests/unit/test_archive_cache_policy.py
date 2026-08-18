"""The one document cache policy shared by foreground reads and warmup."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import py7zr
from zipfile import ZipFile
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


def _write_encrypted_cbz(path: Path, password: str = "pw", pages: int = 4) -> None:
    import pyzipper

    with pyzipper.AESZipFile(
        path, "w", compression=8, encryption=pyzipper.WZ_AES
    ) as archive:
        archive.setpassword(password.encode("utf-8"))
        for index in range(pages):
            archive.writestr(f"{index:03d}.png", _png_bytes((40 + index, 30)))


def _open_encrypted(tmp_path: Path, pool: ArchiveExtractionPool, key: str = "file:secret"):  # noqa: ANN202
    archive = tmp_path / "secret.cbz"
    if not archive.exists():
        _write_encrypted_cbz(archive)
    lease = ArchiveCacheLease(pool, key, ArchiveCacheScope.PERSISTENT)
    session = ArchiveImageService().open(
        archive,
        password_provider=lambda *_a, **_k: "pw",
        cache_lease=lease,
    )
    return session, lease


def test_an_encrypted_document_is_cached_like_any_other_expensive_one(tmp_path: Path) -> None:
    """Supersedes "encrypted pages are on-demand only".

    Refusing the cache kept plaintext off disk, and cost encrypted archives --
    the slowest documents JoyRead reads -- the one mechanism that makes them
    usable. The trade is now taken the other way and stated plainly: extracted
    pages are written to the pool in the clear. Encrypting the pool itself is
    tracked separately; this test is not evidence that it has been done.
    """

    session, _pool = _plan(tmp_path, password="secret")

    assert session.cache_plan.policy is ArchiveCachePolicy.BULK_CONVERT
    assert session.cache_plan.allows_persistent_page_writes
    assert session._cache_lease.scope is ArchiveCacheScope.PERSISTENT  # noqa: SLF001


def test_an_encrypted_document_asks_to_be_warmed_ahead_of_the_reader(tmp_path: Path) -> None:
    """Eligibility alone leaves the first pass through the book as slow as it
    ever was -- every page still decrypts on the turn that needs it. What fixes
    that is being warmed, and warmup only runs for documents that ask."""

    encrypted, _pool = _plan(tmp_path, suffix=".cbz", password="secret")
    plain, _pool2 = _plan(tmp_path, suffix=".cbz")

    assert encrypted.requires_sequential_warmup
    assert not plain.requires_sequential_warmup, "a plain zip still reads directly"


def _write_encrypted_cbz(path: Path, password: str = "pw", pages: int = 4) -> None:
    import pyzipper

    with pyzipper.AESZipFile(
        path, "w", compression=8, encryption=pyzipper.WZ_AES
    ) as archive:
        archive.setpassword(password.encode("utf-8"))
        for index in range(pages):
            archive.writestr(f"{index:03d}.png", _png_bytes((40 + index, 30)))


def _open_encrypted(
    tmp_path: Path,
    pool: ArchiveExtractionPool,
    *,
    key: str = "file:secret",
    scope: ArchiveCacheScope = ArchiveCacheScope.PERSISTENT,
):  # noqa: ANN202
    archive = tmp_path / "secret.cbz"
    if not archive.exists():
        _write_encrypted_cbz(archive)
    lease = ArchiveCacheLease(pool, key, scope)
    session = ArchiveImageService().open(
        archive,
        password_provider=lambda *_a, **_k: "pw",
        cache_lease=lease,
    )
    return session, lease


def test_an_encrypted_zip_reaches_the_pool_at_all(tmp_path: Path) -> None:
    """The measured defect: an encrypted zip reads at ~1.7 s per page because
    CPython decrypts ZipCrypto in a pure-Python byte loop, yet the pool refused
    it for having a zip suffix."""

    pool = ArchiveExtractionPool(tmp_path / "pool", 64 * 1024 * 1024)
    session, lease = _open_encrypted(tmp_path, pool)
    try:
        assert session.cache_plan.allows_persistent_page_writes
        for index in range(session.page_count):
            session.get_image(index)
        assert pool.current_bytes > 0, "reads must populate the pool"
    finally:
        session.close()
        lease.close()


def test_an_encrypted_bundle_outlives_the_session_that_built_it(tmp_path: Path) -> None:
    """The accepted trade, pinned so it cannot be reversed by accident in
    either direction: a reopened encrypted book reads from the pool rather
    than decrypting again, and the plaintext is on disk until evicted."""

    pool_dir = tmp_path / "pool"
    pool = ArchiveExtractionPool(pool_dir, 64 * 1024 * 1024)
    session, lease = _open_encrypted(tmp_path, pool)
    session.get_image(0)
    session.close()
    lease.close()

    assert pool.current_bytes > 0
    assert [path for path in pool_dir.iterdir() if path.suffix == ".zip"]


def test_one_reader_closing_does_not_pull_an_ephemeral_bundle_from_another(
    tmp_path: Path,
) -> None:
    """An unmanaged reader holds an ephemeral lease, whose close deletes the
    bundle. Two live sessions per document is normal -- the Reader and the
    thumbnail stream open the same book independently -- so the delete has to
    wait for the last of them."""

    pool = ArchiveExtractionPool(tmp_path / "pool", 64 * 1024 * 1024)
    scope = ArchiveCacheScope.EPHEMERAL
    first, first_lease = _open_encrypted(tmp_path, pool, key="session:abc", scope=scope)
    second, second_lease = _open_encrypted(tmp_path, pool, key="session:abc", scope=scope)
    first.get_image(0)
    populated = pool.current_bytes
    assert populated > 0

    first.close()
    first_lease.close()

    assert pool.current_bytes == populated, "the surviving session still needs it"

    second.close()
    second_lease.close()

    assert pool.current_bytes == 0


def test_an_encrypted_zip_converts_in_one_pass_rather_than_page_by_page(tmp_path: Path) -> None:
    """The GIL is the reason this matters.

    `zipfile._ZipDecrypter` is a per-byte Python loop measuring ~2.5 MB/s that
    holds the GIL, so it does not parallelise: four decrypting threads take
    four times as long as one and stall the UI thread meanwhile. Warming such a
    document on a worker duplicates the foreground reader's work and makes the
    visible page turn slower. `7zz` does it in another process -- measured at
    1.05 s against ~40 s for the same 100 MB archive.
    """

    pool = ArchiveExtractionPool(tmp_path / "pool", 64 * 1024 * 1024)
    session, lease = _open_encrypted(tmp_path, pool)
    try:
        assert session.cache_plan.policy is ArchiveCachePolicy.BULK_CONVERT
        assert session.requires_sequential_warmup
    finally:
        session.close()
        lease.close()


def test_an_encrypted_document_will_not_be_warmed_without_a_bulk_extractor(
    tmp_path: Path,
) -> None:
    """No `7zz` means the only available warmup is the GIL-bound Python loop,
    which costs the foreground reader more than it ever returns. Better to
    leave the pages on demand than to warm them that way."""

    session, _pool = _plan(tmp_path, suffix=".cbz", password="secret", bulk_extract=None)

    assert not session.requires_sequential_warmup
    # Still pool-eligible: foreground reads are cached, they are just not
    # raced against a background copy of themselves.
    assert session.cache_plan.allows_persistent_page_writes


def test_a_plain_zip_never_converts_even_with_a_bulk_extractor(tmp_path: Path) -> None:
    """A plain zip already has cheap random access, so converting it would be
    pure cost -- the whole point of judging on access cost rather than family.

    Enforced by the cache plan (DIRECT before bulk capability is consulted),
    not by the backend: `supports_bulk_extraction` is capability-only, since
    answering the policy question there meant re-parsing the central directory
    the scan had just parsed, on every open.
    """

    archive = tmp_path / "plain.cbz"
    with ZipFile(archive, "w") as handle:
        handle.writestr("000.png", _png_bytes((40, 30)))
    session = ArchiveImageService().open(archive)
    try:
        assert session.cache_plan.policy is ArchiveCachePolicy.DIRECT
        assert not session.cache_plan.allows_background_warmup
        assert not session.cache_plan.allows_persistent_page_writes
    finally:
        session.close()


def test_marking_a_document_session_scoped_takes_its_bytes_on_last_release(
    tmp_path: Path,
) -> None:
    """The Privacy switch, at the level that enforces it.

    Extracted pages of an encrypted archive are plaintext, so a user who asks
    for them not to be left behind must get that even though the document is
    routinely held by more than one lease -- the Reader's and a background
    warmup's. The mark lives on the pool for exactly that reason.
    """

    pool = ArchiveExtractionPool(tmp_path / "pool", 64 * 1024 * 1024)
    reader, reader_lease = _open_encrypted(tmp_path, pool)
    warmup, warmup_lease = _open_encrypted(tmp_path, pool)
    pool.mark_session_scoped("file:secret")
    reader.get_image(0)
    assert pool.current_bytes > 0

    # The Reader closes first; the warmup lease is still live.
    reader.close()
    reader_lease.close()
    assert pool.current_bytes > 0, "a live warmup still needs the bundle"

    warmup.close()
    warmup_lease.close()

    assert pool.current_bytes == 0
    assert not [path for path in (tmp_path / "pool").iterdir() if path.suffix == ".zip"]


def test_an_unmarked_encrypted_document_keeps_its_cache(tmp_path: Path) -> None:
    """With the switch off, the bundle survives so the next session reads it
    straight from the pool instead of converting again."""

    pool = ArchiveExtractionPool(tmp_path / "pool", 64 * 1024 * 1024)
    session, lease = _open_encrypted(tmp_path, pool)
    session.get_image(0)
    session.close()
    lease.close()

    assert pool.current_bytes > 0
