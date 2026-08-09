"""Tests for memory-bounded sequential archive read planning."""

from __future__ import annotations

from types import SimpleNamespace

from joyread.core.archive.batching import MAX_SEQUENTIAL_BATCH_BYTES, plan_read_batch
from joyread.core.archive.records import ArchiveSource, PageRecord
from joyread.core.archive.session import ArchiveImageSession
from joyread.core.reader.session_service import ReaderSessionService


MIB = 1024 * 1024


def test_batch_planner_preserves_order_and_limits_items() -> None:
    candidates = tuple((index, 1 * MIB) for index in range(12))

    assert plan_read_batch(candidates) == tuple(range(8))


def test_batch_planner_stops_before_256_mib_boundary() -> None:
    candidates = ((7, 128 * MIB), (3, 128 * MIB), (9, 1))

    assert plan_read_batch(candidates) == (7, 3)


def test_batch_planner_isolates_unknown_and_oversized_entries() -> None:
    assert plan_read_batch(((4, None), (5, 1))) == (4,)
    assert plan_read_batch(((4, MAX_SEQUENTIAL_BATCH_BYTES + 1), (5, 1))) == (4,)
    assert plan_read_batch(((4, 1), (5, None))) == (4,)


def test_archive_session_only_batches_cold_sequential_pages_from_same_source() -> None:
    first_source = ArchiveSource(
        "first.7z",
        ".7z",
        data=b"archive",
        requires_sequential_warmup=True,
    )
    nested_source = ArchiveSource(
        "nested.7z",
        ".7z",
        data=b"nested",
        requires_sequential_warmup=True,
    )
    pages = (
        PageRecord("0.jpg", first_source, "0.jpg", None, 200 * MIB),
        PageRecord("1.jpg", first_source, "1.jpg", None, 60 * MIB),
        PageRecord("2.jpg", nested_source, "2.jpg", None, 1 * MIB),
    )
    session = ArchiveImageSession(pages, lambda _source, _entries, _budget: {})

    assert session.plan_read_batch((0, 1, 2)) == (0,)
    assert session.plan_read_batch((1, 2)) == (1,)


def test_disk_warmup_uses_the_session_batch_planner(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    class _WarmupSession:
        page_count = 10

        def __init__(self) -> None:
            self.reads: list[tuple[int, ...]] = []
            self.plans: list[tuple[int, ...]] = []
            self.ready = False
            self.closed = False

        def plan_read_batch(self, candidates, *, max_items=8):  # noqa: ANN001, ANN201
            self.plans.append(tuple(candidates))
            return tuple(candidates[: min(2, max_items)])

        def get_pages(self, indices):  # noqa: ANN001, ANN201
            batch = tuple(indices)
            self.reads.append(batch)
            return [
                SimpleNamespace(index=index, image_bytes=b"page", dimensions=(1, 1))
                for index in batch
            ]

        def mark_thumbnail_cache_ready(self) -> None:
            self.ready = True

        def close(self) -> None:
            self.closed = True

    session = _WarmupSession()
    service = ReaderSessionService(SimpleNamespace())  # type: ignore[arg-type]
    monkeypatch.setattr(service, "open_archive", lambda *_args, **_kwargs: session)

    service.warm_disk_cache(tmp_path / "book.7z")

    assert session.reads == [(0, 1), (2, 3), (4, 5), (6, 7), (8, 9)]
    assert session.plans == [
        tuple(range(0, 8)),
        tuple(range(2, 10)),
        tuple(range(4, 10)),
        tuple(range(6, 10)),
        (8, 9),
    ]
    assert session.ready is True
    assert session.closed is True
