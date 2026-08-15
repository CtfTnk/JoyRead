"""Warmup wiring: one bulk conversion, and a deliberately narrow fallback."""

from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import py7zr
import pytest
from PIL import Image

from joyread.core.archive import ArchiveAccessMode, ArchiveImageService
from joyread.core.archive.errors import ArchiveCancelled, ArchiveResourceLimitError
from joyread.core.archive.models import (
    ArchiveCachePlan,
    ArchiveCachePolicy,
    ArchiveConversionResult,
    ArchiveConversionStatus,
)
from joyread.core.archive.records import ArchiveSource
from joyread.core.reader.session_service import ReaderSessionService
from joyread.core.services.archive_extraction_pool import ArchiveExtractionPool


def _png_bytes(size: tuple[int, int], color: str = "#ffffff") -> bytes:
    image = Image.new("RGB", size, color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _write_7z(path: Path, pages: int = 6) -> None:
    with py7zr.SevenZipFile(path, "w") as archive:
        for index in range(pages):
            archive.writestr(_png_bytes((40 + index, 20)), f"{index:03d}.png")


class _CountingSession:
    """A session whose policy and conversion outcome the test dictates."""

    page_count = 6

    def __init__(
        self,
        result: ArchiveConversionResult | Exception | None = None,
        *,
        policy: ArchiveCachePolicy = ArchiveCachePolicy.BULK_CONVERT,
        reason: str = "",
    ) -> None:
        self._result = result
        self.cache_plan = ArchiveCachePlan(policy, reason)
        self.reads: list[tuple[int, ...]] = []
        self.conversions = 0
        self.discards = 0
        self.ready = False
        self.closed = False
        self.build_depth = 0
        self.read_build_depths: list[int] = []
        self.ready_build_depth: int | None = None
        self.discard_build_depths: list[int] = []

    @contextmanager
    def cache_build_guard(self):  # noqa: ANN201
        self.build_depth += 1
        try:
            yield True
        finally:
            self.build_depth -= 1

    def convert_to_cache(self, *, is_cancelled=None):  # noqa: ANN001, ANN201
        del is_cancelled
        self.conversions += 1
        if isinstance(self._result, Exception):
            raise self._result
        assert self._result is not None
        return self._result

    def discard_unpublished_cache(self) -> bool:
        self.discards += 1
        self.discard_build_depths.append(self.build_depth)
        return True

    def get_pages(self, indices):  # noqa: ANN001, ANN201
        batch = tuple(indices)
        self.reads.append(batch)
        self.read_build_depths.append(self.build_depth)
        return [
            SimpleNamespace(index=index, image_bytes=b"page", dimensions=(1, 1))
            for index in batch
        ]

    def mark_thumbnail_cache_ready(self) -> None:
        self.ready = True
        self.ready_build_depth = self.build_depth

    def close(self) -> None:
        self.closed = True


def _service_with(session: _CountingSession, monkeypatch: pytest.MonkeyPatch) -> ReaderSessionService:
    service = ReaderSessionService(SimpleNamespace())  # type: ignore[arg-type]
    monkeypatch.setattr(service, "open_archive", lambda *_a, **_k: session)
    return service


def test_a_published_conversion_replaces_the_chunked_warmup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _CountingSession(
        ArchiveConversionResult(ArchiveConversionStatus.PUBLISHED, "", 6)
    )

    _service_with(session, monkeypatch).warm_disk_cache(tmp_path / "book.7z")

    assert session.conversions == 1
    assert session.reads == [], "a converted document must not be read page by page"
    assert session.closed is True


def test_an_unsupported_container_still_warms_in_bounded_batches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A capability gap is the one case that may use the slower path."""

    session = _CountingSession(
        ArchiveConversionResult(ArchiveConversionStatus.UNSUPPORTED, "backend_cannot_represent", 6)
    )

    _service_with(session, monkeypatch).warm_disk_cache(tmp_path / "book.7z", chunk_size=3)

    assert session.reads == [(0, 1, 2), (3, 4, 5)]
    assert session.ready is True
    assert session.read_build_depths == [1, 1]
    assert session.ready_build_depth == 1
    assert session.build_depth == 0


def test_a_sequential_policy_warms_without_attempting_conversion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _CountingSession(policy=ArchiveCachePolicy.SEQUENTIAL_WARM, reason="no_bulk_backend")

    _service_with(session, monkeypatch).warm_disk_cache(tmp_path / "book.rar", chunk_size=3)

    assert session.conversions == 0, "a non-bulk policy must not call the converter"
    assert session.reads == [(0, 1, 2), (3, 4, 5)]
    assert session.ready is True
    assert session.read_build_depths == [1, 1]
    assert session.ready_build_depth == 1
    assert session.build_depth == 0


def test_an_on_demand_only_document_is_never_warmed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The defect this replaces: a document refused a whole-document cache
    product still built one, page by page, through the slower path."""

    session = _CountingSession(
        policy=ArchiveCachePolicy.ON_DEMAND_ONLY, reason="larger_than_cache"
    )

    _service_with(session, monkeypatch).warm_disk_cache(tmp_path / "huge.7z", chunk_size=6)

    assert session.conversions == 0
    assert session.reads == [], "no background reads may run at all"
    assert session.ready is False
    assert session.discards == 1, "a stale partial bundle must be reclaimed"
    assert session.discard_build_depths == [0]
    assert session.closed is True


def test_an_on_demand_only_result_stops_the_warmup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The session may refuse even when the plan looked convertible."""

    session = _CountingSession(
        ArchiveConversionResult(ArchiveConversionStatus.ON_DEMAND_ONLY, "larger_than_cache", 6)
    )

    _service_with(session, monkeypatch).warm_disk_cache(tmp_path / "huge.7z", chunk_size=6)

    assert session.reads == []
    assert session.discards == 1


def test_a_failed_conversion_never_falls_back_to_chunked_reads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Re-reading the same input the slow way walks past whatever just failed."""

    session = _CountingSession(
        ArchiveConversionResult(ArchiveConversionStatus.FAILED, "cache_write_failed", 6)
    )

    _service_with(session, monkeypatch).warm_disk_cache(tmp_path / "book.7z")

    assert session.reads == []
    assert session.ready is False
    assert session.closed is True


def test_a_resource_limit_stops_the_warmup_instead_of_retrying_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _CountingSession(
        ArchiveResourceLimitError("operation_bytes", actual=2, maximum=1, subject="book")
    )
    service = _service_with(session, monkeypatch)

    with pytest.raises(ArchiveResourceLimitError):
        service.warm_disk_cache(tmp_path / "book.7z")

    assert session.reads == []
    assert session.closed is True


def test_cancellation_ends_the_warmup_quietly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _CountingSession(ArchiveCancelled("cancelled"))

    _service_with(session, monkeypatch).warm_disk_cache(tmp_path / "book.7z")

    assert session.reads == []
    assert session.ready is False
    assert session.closed is True


def test_bulk_capability_is_bound_only_to_containers_that_support_it(
    tmp_path: Path,
) -> None:
    service = ArchiveImageService(extraction_pool=ArchiveExtractionPool(tmp_path / "pool", 1 << 20))
    seven_zip = ArchiveSource(label="book.7z", suffix=".7z", path=tmp_path / "book.7z")
    zip_source = ArchiveSource(label="book.cbz", suffix=".cbz", path=tmp_path / "book.cbz")
    nested = ArchiveSource(label="nested.7z", suffix=".7z", data=b"nested")

    assert service._bulk_extract_for(seven_zip) is not None  # noqa: SLF001
    assert service._bulk_extract_for(zip_source) is None  # noqa: SLF001
    assert service._bulk_extract_for(nested) is None  # noqa: SLF001


def test_a_real_7z_warms_through_one_conversion_and_then_reads_from_the_pool(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "sample.7z"
    _write_7z(archive_path, pages=6)
    pool = ArchiveExtractionPool(tmp_path / "pool", 8 * 1024 * 1024)
    archive_service = ArchiveImageService(extraction_pool=pool)
    service = ReaderSessionService(archive_service)

    cold = archive_service.open(
        archive_path,
        document_cache_key="file:sample",
        allow_persistent_cache=True,
    )
    assert cold.access_mode is ArchiveAccessMode.EXPENSIVE_COLD
    cold.close()

    backend = archive_service._seven_zip_backend  # noqa: SLF001
    bulk_calls: list[int] = []
    page_reads: list[int] = []
    real_extract_members = backend.extract_members
    real_read_entries = backend.read_entries

    def counting_extract_members(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        bulk_calls.append(1)
        return real_extract_members(*args, **kwargs)

    def counting_read_entries(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        page_reads.append(1)
        return real_read_entries(*args, **kwargs)

    backend.extract_members = counting_extract_members  # type: ignore[method-assign]
    backend.read_entries = counting_read_entries  # type: ignore[method-assign]
    try:
        service.warm_disk_cache(
            archive_path,
            document_cache_key="file:sample",
            allow_persistent_cache=True,
        )
    finally:
        backend.extract_members = real_extract_members  # type: ignore[method-assign]
        backend.read_entries = real_read_entries  # type: ignore[method-assign]

    assert bulk_calls == [1], "warming must issue exactly one bulk extraction"
    assert page_reads == [], "no page was read on demand during warmup"

    ready = archive_service.open(
        archive_path,
        document_cache_key="file:sample",
        allow_persistent_cache=True,
    )
    try:
        assert ready.access_mode is ArchiveAccessMode.EXPENSIVE_READY
        assert ready.requires_sequential_warmup is False
        # Every page now comes from the pool: the backend is never consulted.
        ready._read_entries = _refuse_backend_reads  # noqa: SLF001
        assert all(page is not None for page in ready.get_pages(range(6)))
    finally:
        ready.close()


def _refuse_backend_reads(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
    raise AssertionError("a converted document must not touch the archive backend")
