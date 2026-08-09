"""Thread-safe archive page session and extraction-cache access."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
import hashlib
from io import BytesIO
from threading import BoundedSemaphore, RLock
from time import perf_counter

from PIL import Image, UnidentifiedImageError

from joyread.core.archive.errors import ArchiveReadError, ArchiveResourceLimitError
from joyread.core.archive.batching import (
    MAX_SEQUENTIAL_BATCH_BYTES,
    MAX_SEQUENTIAL_BATCH_ITEMS,
    plan_read_batch as plan_sequential_read_batch,
)
from joyread.core.diagnostics import cache_identity_kind, reader_perf_enabled, reader_perf_event
from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget, ensure_item_size
from joyread.core.archive.models import ArchiveAccessMode, ArchiveContentsEntry, ArchivePage
from joyread.core.archive.records import ArchiveSource, PageRecord
from joyread.core.services.archive_extraction_pool import ArchiveExtractionCache
from joyread.core.services.archive_cache_lease import ArchiveCacheLease, ArchiveCacheScope


EXPENSIVE_ARCHIVE_EXTENSIONS = frozenset({".7z", ".cb7", ".rar", ".cbr"})

# Pillow's process-global threshold would impose an undocumented ~179 MP hard
# failure even when JoyRead's configurable guardrail is 400 MP or unlimited.
# Archive payloads are checked against ``ArchiveOpenLimits.max_image_pixels``
# immediately after this header read and before any Qt/Pillow pixel decode.
Image.MAX_IMAGE_PIXELS = None


ReadEntries = Callable[
    [ArchiveSource, Sequence[tuple[str, str | None]], ArchiveOperationBudget], dict[str, bytes]
]


class ArchiveImageSession:
    """Bounded, thread-safe access to image pages discovered in one archive."""

    def __init__(
        self,
        pages: Iterable[PageRecord],
        read_entries: ReadEntries,
        contents: Iterable[ArchiveContentsEntry] = (),
        *,
        document_cache_key: str | None = None,
        extraction_cache: ArchiveExtractionCache | None = None,
        cache_lease: ArchiveCacheLease | None = None,
        cache_signature: str = "",
        limits: ArchiveOpenLimits | None = None,
    ) -> None:
        self._pages = tuple(pages)
        self._read_entries = read_entries
        self._contents = tuple(contents)
        if cache_lease is not None and (document_cache_key is not None or extraction_cache is not None):
            raise ValueError("Pass cache_lease or legacy cache arguments, not both.")
        if cache_lease is None and document_cache_key is not None and extraction_cache is not None:
            cache_lease = ArchiveCacheLease(
                extraction_cache,
                document_cache_key,
                ArchiveCacheScope.PERSISTENT,
            )
        self._cache_lease = cache_lease
        self._cache_signature = cache_signature
        self._limits = limits or ArchiveOpenLimits()
        self._state_lock = RLock()
        self._closing = False
        self._closed = False
        self._active_reads = 0
        self._lease_finalized = False
        self._close_started = 0.0
        self._uses_expensive_cache = any(
            record.source.suffix in EXPENSIVE_ARCHIVE_EXTENSIONS
            for record in self._pages
        )
        self._requires_sequential_warmup = any(
            record.source.requires_sequential_warmup
            for record in self._pages
            if record.source.suffix in EXPENSIVE_ARCHIVE_EXTENSIONS
        )
        # Every read opens an independent backend handle. Plain ZIP can sustain
        # two concurrent random reads; encrypted ZIP and expensive formats are
        # serialized per document to avoid extractor contention.
        supports_two_reads = all(
            record.source.suffix in {".zip", ".cbz"} and record.password is None
            for record in self._pages
        )
        self._read_slots = BoundedSemaphore(2 if supports_two_reads else 1)
        self.current_index = 0

    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def index_range(self) -> range:
        return range(0, self.page_count)

    @property
    def contents(self) -> tuple[ArchiveContentsEntry, ...]:
        return self._contents

    @property
    def access_mode(self) -> ArchiveAccessMode:
        if not self._uses_expensive_cache:
            return ArchiveAccessMode.DIRECT
        return (
            ArchiveAccessMode.EXPENSIVE_READY
            if self._cache_is_complete()
            else ArchiveAccessMode.EXPENSIVE_COLD
        )

    def access_mode_for(self, page_index: int) -> ArchiveAccessMode:
        if not self.is_valid_index(page_index):
            return ArchiveAccessMode.DIRECT
        record = self._pages[page_index]
        if record.source.suffix not in EXPENSIVE_ARCHIVE_EXTENSIONS:
            return ArchiveAccessMode.DIRECT
        return (
            ArchiveAccessMode.EXPENSIVE_READY
            if self._cache_is_complete()
            else ArchiveAccessMode.EXPENSIVE_COLD
        )

    @property
    def requires_sequential_warmup(self) -> bool:
        return self._requires_sequential_warmup and not self._cache_is_complete()

    def thumbnail_batch_size(self, page_index: int) -> int:
        if self.access_mode_for(page_index) != ArchiveAccessMode.EXPENSIVE_COLD:
            return 1
        return 8 if self._pages[page_index].source.requires_sequential_warmup else 1

    def plan_read_batch(
        self,
        candidates: Iterable[int],
        *,
        max_items: int = MAX_SEQUENTIAL_BATCH_ITEMS,
        max_declared_bytes: int = MAX_SEQUENTIAL_BATCH_BYTES,
    ) -> tuple[int, ...]:
        """Plan one read without exceeding sequential py7zr batch bounds."""

        valid = tuple(dict.fromkeys(int(index) for index in candidates if self.is_valid_index(int(index))))
        if not valid:
            return ()
        first = valid[0]
        first_record = self._pages[first]
        if (
            self.access_mode_for(first) != ArchiveAccessMode.EXPENSIVE_COLD
            or not first_record.source.requires_sequential_warmup
        ):
            return (first,)
        same_source: list[tuple[int, int | None]] = []
        for page_index in valid:
            record = self._pages[page_index]
            if record.source is not first_record.source or not record.source.requires_sequential_warmup:
                break
            same_source.append((page_index, record.size))
        selected = plan_sequential_read_batch(
            same_source,
            max_items=min(MAX_SEQUENTIAL_BATCH_ITEMS, max(1, int(max_items))),
            max_declared_bytes=min(
                MAX_SEQUENTIAL_BATCH_BYTES,
                max(1, int(max_declared_bytes)),
            ),
        )
        if reader_perf_enabled():
            reader_perf_event(
                "archive.batch.planned",
                targets=len(selected),
                declared_bytes=sum(self._pages[index].size or 0 for index in selected),
                has_unknown_size=any(self._pages[index].size is None for index in selected),
            )
        return selected or (first,)

    def mark_thumbnail_cache_ready(self) -> bool:
        if not self._uses_expensive_cache or not self._can_use_document_cache():
            return False
        if any(not self._record_is_cacheable(record) for record in self._pages):
            return False
        expected_keys = tuple(self._cache_page_key(index) for index in range(self.page_count))
        assert self._cache_lease is not None
        if len(self._cache_lease.get_many(expected_keys)) != self.page_count:
            return False
        self._cache_lease.mark_complete(self.page_count, self._cache_signature)
        return self._cache_is_complete()

    def is_not_empty(self) -> bool:
        return self.page_count > 0

    def is_valid_index(self, index: int) -> bool:
        return 0 <= index < self.page_count

    def has_next(self, index: int | None = None) -> bool:
        with self._state_lock:
            checked_index = self.current_index if index is None else index
            return self.is_valid_index(checked_index + 1)

    def has_previous(self, index: int | None = None) -> bool:
        with self._state_lock:
            checked_index = self.current_index if index is None else index
            return self.is_valid_index(checked_index - 1)

    def get_image(self, index: int) -> bytes | None:
        page = self.get_page(index)
        return page.image_bytes if page is not None else None

    def get_images(self, start: int, count: int) -> list[bytes | None]:
        if count <= 0:
            return []
        return [page.image_bytes if page is not None else None for page in self.get_pages(range(start, start + count))]

    def get_dimensions(self, index: int) -> tuple[int, int] | None:
        page = self.read_pages((index,))[0]
        return page.dimensions if page is not None else None

    def get_page(self, index: int) -> ArchivePage | None:
        return self.read_pages((index,))[0]

    def get_pages(self, indices: Iterable[int]) -> list[ArchivePage | None]:
        """Blocking compatibility alias for :meth:`read_pages`."""

        return self.read_pages(indices)

    def read_pages(self, indices: Iterable[int]) -> list[ArchivePage | None]:
        """Read bounded page payloads for worker-side consumers.

        No mutable page metadata is written here. Backend reads are limited by
        the document capability semaphore while cache I/O uses the pool's own
        lock, so unrelated cache operations do not serialize the whole call.
        """

        requested = list(indices)
        with self._state_lock:
            if self._closing or self._closed:
                return [None] * len(requested)
            self._active_reads += 1
        perf_enabled = reader_perf_enabled()
        started = perf_counter() if perf_enabled else 0.0
        loaded_bytes = 0
        try:
            result = self._read_registered_pages(requested)
            loaded_bytes = sum(len(page.image_bytes) for page in result if page is not None)
            return result
        finally:
            active_reads, finalized = self._finish_registered_read()
            if perf_enabled:
                reader_perf_event(
                    "archive.session.read",
                    pages=len(requested),
                    bytes=loaded_bytes,
                    elapsed_ms=round((perf_counter() - started) * 1000.0, 3),
                    active_reads=active_reads,
                    finalized=finalized,
                    identity_kind=self._cache_identity_kind(),
                )

    def _read_registered_pages(
        self,
        requested: list[int],
    ) -> list[ArchivePage | None]:
        # This method runs only while ``_active_reads`` owns the cache lease.
        # Keeping the whole operation under that registration guarantees close
        # cannot release or purge the lease between a cache lookup and write.
        with self._read_slots:
            results: list[ArchivePage | None] = [None] * len(requested)
            missing: list[tuple[int, int, PageRecord]] = []
            budget = ArchiveOperationBudget(self._limits.max_operation_bytes)

            for result_index, page_index in enumerate(requested):
                if not self.is_valid_index(page_index):
                    continue
                record = self._pages[page_index]
                if self._record_is_cacheable(record):
                    assert self._cache_lease is not None
                    cached = self._cache_lease.get(self._cache_page_key(page_index))
                    if cached is not None:
                        # A cache hit is still an archive read for the current
                        # session policy: check both byte budgets and pixels.
                        ensure_item_size(len(cached), self._limits.max_extracted_item_bytes, record.display_path)
                        budget.consume(len(cached), record.display_path)
                        page = archive_page_from_bytes(page_index, record, cached, self._limits)
                        if page is not None:
                            results[result_index] = page
                            continue
                missing.append((result_index, page_index, record))

            groups: OrderedDict[tuple[int, str | None], list[tuple[int, int, PageRecord]]] = OrderedDict()
            for item in missing:
                record = item[2]
                groups.setdefault((id(record.source), record.password), []).append(item)

            cache_payloads: dict[str, bytes] = {}
            for group in groups.values():
                source = group[0][2].source
                requests = [(record.name, record.password) for _result_index, _page_index, record in group]
                payloads = self._read_entries(source, requests, budget)
                for result_index, page_index, record in group:
                    payload = payloads.get(record.name)
                    if payload is None:
                        continue
                    page = archive_page_from_bytes(page_index, record, payload, self._limits)
                    if page is None:
                        continue
                    results[result_index] = page
                    if self._record_is_cacheable(record):
                        cache_payloads[self._cache_page_key(page_index)] = payload

            if cache_payloads:
                assert self._cache_lease is not None
                self._cache_lease.put_many(cache_payloads)

            return results

    def get_aspect_ratio(self, index: int) -> tuple[float, float] | None:
        dimensions = self.get_dimensions(index)
        if dimensions is None:
            return None
        width, height = dimensions
        if height == 0:
            return None
        return (float(width) / float(height), 1.0)

    def get_horizontal_aspect_ratio(self, indices: Iterable[int]) -> tuple[float, float] | None:
        ratios: list[tuple[float, float]] = []
        for index in indices:
            ratio = self.get_aspect_ratio(index)
            if ratio is None:
                return None
            ratios.append(ratio)
        if not ratios:
            return None
        return (sum(width for width, _height in ratios), 1.0)

    def current(self) -> bytes | None:
        with self._state_lock:
            index = self.current_index
        return self.get_image(index)

    def seek(self, index: int) -> bool:
        with self._state_lock:
            if not self.is_valid_index(index):
                return False
            self.current_index = index
            return True

    def next(self) -> bytes | None:
        with self._state_lock:
            if not self.is_valid_index(self.current_index + 1):
                return None
            self.current_index += 1
            index = self.current_index
        return self.get_image(index)

    def previous(self) -> bytes | None:
        with self._state_lock:
            if not self.is_valid_index(self.current_index - 1):
                return None
            self.current_index -= 1
            index = self.current_index
        return self.get_image(index)

    def _can_use_document_cache(self) -> bool:
        return (
            self._uses_expensive_cache
            and self._cache_lease is not None
            and not self._cache_lease.is_closed
        )

    def _record_is_cacheable(self, record: PageRecord) -> bool:
        return (
            self._can_use_document_cache()
            and record.password is None
            and record.source.allow_persistent_cache
        )

    def _cache_is_complete(self) -> bool:
        if not self._can_use_document_cache():
            return False
        assert self._cache_lease is not None
        return self._cache_lease.is_complete(
            self.page_count,
            self._cache_signature,
        )

    def promote_cache(self, persistent_key: str) -> bool:
        with self._state_lock:
            if self._closing or self._closed:
                return False
            lease = self._cache_lease
        return lease.promote(persistent_key) if lease is not None else False

    def close(self) -> None:
        lease: ArchiveCacheLease | None = None
        finalized = False
        with self._state_lock:
            if self._closing or self._closed:
                return
            self._closing = True
            self._close_started = perf_counter()
            active_reads = self._active_reads
            if active_reads == 0:
                finalized, lease = self._finalize_close_locked()
        reader_perf_event(
            "archive.session.close_requested",
            active_reads=active_reads,
            deferred=active_reads > 0,
            identity_kind=self._cache_identity_kind(),
        )
        if lease is not None:
            lease.close()
        if finalized:
            reader_perf_event(
                "archive.session.closed",
                active_reads=0,
                identity_kind=self._cache_identity_kind(),
                drain_ms=round((perf_counter() - self._close_started) * 1000.0, 3),
            )

    def _finish_registered_read(self) -> tuple[int, bool]:
        lease: ArchiveCacheLease | None = None
        finalized = False
        with self._state_lock:
            self._active_reads = max(0, self._active_reads - 1)
            active_reads = self._active_reads
            if self._closing and active_reads == 0:
                finalized, lease = self._finalize_close_locked()
        if lease is not None:
            lease.close()
        if finalized:
            reader_perf_event(
                "archive.session.closed",
                active_reads=0,
                identity_kind=self._cache_identity_kind(),
                drain_ms=round((perf_counter() - self._close_started) * 1000.0, 3),
            )
        return active_reads, finalized

    def _finalize_close_locked(self) -> tuple[bool, ArchiveCacheLease | None]:
        if self._lease_finalized:
            return False, None
        self._lease_finalized = True
        self._closed = True
        return True, self._cache_lease

    def _cache_identity_kind(self) -> str:
        lease = self._cache_lease
        return cache_identity_kind(lease.document_cache_key) if lease is not None else "none"

    def _cache_page_key(self, page_index: int) -> str:
        # Scope page indices to the scanner/policy signature as well: a changed
        # depth limit can legitimately assign a different image to the same
        # flattened index while a partial old bundle still exists on disk.
        policy_key = hashlib.sha256(self._cache_signature.encode("utf-8")).hexdigest()[:16]
        return f"pages/{policy_key}/{page_index:08d}"


def archive_page_from_bytes(
    index: int,
    record: PageRecord,
    payload: bytes,
    limits: ArchiveOpenLimits,
) -> ArchivePage | None:
    ensure_item_size(len(payload), limits.max_extracted_item_bytes, record.display_path)
    dimensions = _required_dimensions(payload, limits, record.display_path)
    return ArchivePage(
        index=index,
        image_bytes=payload,
        dimensions=dimensions,
        display_path=record.display_path,
    )


def dimensions_from_bytes(
    payload: bytes,
    limits: ArchiveOpenLimits | None = None,
    subject: str | None = None,
) -> tuple[int, int] | None:
    try:
        with Image.open(BytesIO(payload)) as image:
            width, height = (int(image.width), int(image.height))
            maximum = limits.max_image_pixels if limits is not None else None
            if maximum is not None and width * height > maximum:
                raise ArchiveResourceLimitError(
                    "image_pixels",
                    actual=width * height,
                    maximum=maximum,
                    subject=subject,
                )
            return (width, height)
    except (OSError, UnidentifiedImageError):
        return None


def _required_dimensions(
    payload: bytes,
    limits: ArchiveOpenLimits,
    subject: str,
) -> tuple[int, int]:
    dimensions = dimensions_from_bytes(payload, limits, subject)
    if dimensions is None:
        raise ArchiveReadError(f"Could not decode archive image: {subject}")
    return dimensions
