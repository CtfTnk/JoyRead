"""Thread-safe archive page session and extraction-cache access."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
import hashlib
from io import BytesIO
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore, RLock
from time import perf_counter

from PIL import Image, UnidentifiedImageError

from joyread.core.archive.errors import (
    ArchiveCancelled,
    ArchiveReadError,
    ArchiveResourceLimitError,
)
from joyread.core.archive.formats.common import read_file_bounded
from joyread.core.archive.batching import (
    MAX_SEQUENTIAL_BATCH_BYTES,
    MAX_SEQUENTIAL_BATCH_ITEMS,
    plan_read_batch as plan_sequential_read_batch,
)
from joyread.core.diagnostics import cache_identity_kind, reader_perf_enabled, reader_perf_event
from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget, ensure_item_size
from joyread.core.archive.models import (
    ArchiveAccessMode,
    ArchiveContentsEntry,
    ArchiveConversionResult,
    ArchiveConversionStatus,
    ArchivePage,
)
from joyread.core.archive.records import ArchiveSource, PageRecord
from joyread.core.services.archive_extraction_pool import ArchiveExtractionCache
from joyread.core.services.archive_cache_lease import ArchiveCacheLease, ArchiveCacheScope


EXPENSIVE_ARCHIVE_EXTENSIONS = frozenset({".7z", ".cb7", ".rar", ".cbr"})

# Pillow's process-global threshold would impose an undocumented ~179 MP hard
# failure even when JoyRead's configurable guardrail is 400 MP or unlimited.
# Archive payloads are checked against ``ArchiveOpenLimits.max_image_pixels``
# immediately after this header read and before any Qt/Pillow pixel decode.
Image.MAX_IMAGE_PIXELS = None


logger = logging.getLogger(__name__)

ReadEntries = Callable[
    [ArchiveSource, Sequence[tuple[str, str | None]], ArchiveOperationBudget], dict[str, bytes]
]
#: Extract the named members of one source into a directory in a single pass.
BulkExtract = Callable[..., None]


# Bulk conversion writes to the pool in groups bounded by *both* item count
# and bytes. `put_many` holds the pool lock for a whole append, so foreground
# cache reads queue behind it: measured on 174 pages, one whole-book write
# blocked a read for 19.3 ms against 2.76 ms at 16 items, while the write
# throughput given up is noise beside the ~330 ms extraction. The byte bound
# keeps that true when pages are unusually large.
CONVERSION_GROUP_ITEMS = 16
# A *target*, not a hard cap. One page is the smallest indivisible unit a
# `put_many` can carry, so a page bigger than the target is written on its own
# and the group is that page. Splitting it would require streaming a single
# member into the bundle, which `put_many` cannot express; the real ceiling on
# one page stays `ArchiveOpenLimits.max_extracted_item_bytes`. Peak conversion
# memory is therefore max(target, largest page), and the sample corpus does
# contain single images near 50 MB.
CONVERSION_GROUP_TARGET_BYTES = 16 * 1024 * 1024
# Declared sizes come from archive metadata and are untrusted, so this is a
# planning allowance, not the enforced limit; the runtime written-bytes bound
# in the extractor is what actually stops a runaway.
CONVERSION_DECLARED_TOLERANCE = 1.25


class ArchiveImageSession:
    """Bounded, thread-safe access to image pages discovered in one archive."""

    def __init__(
        self,
        pages: Iterable[PageRecord],
        read_entries: ReadEntries,
        contents: Iterable[ArchiveContentsEntry] = (),
        *,
        bulk_extract: BulkExtract | None = None,
        document_cache_key: str | None = None,
        extraction_cache: ArchiveExtractionCache | None = None,
        cache_lease: ArchiveCacheLease | None = None,
        cache_signature: str = "",
        limits: ArchiveOpenLimits | None = None,
    ) -> None:
        self._pages = tuple(pages)
        self._read_entries = read_entries
        self._bulk_extract = bulk_extract
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

    def convert_to_cache(
        self,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ArchiveConversionResult:
        """Extract every page once and publish the whole document to the cache.

        Solid archives make per-batch reads quadratic: each one re-decompresses
        its own prefix and pays the backend's decompression dictionary again.
        One pass costs about what a single mid-archive page costs, so the whole
        book is converted and served from the pool afterwards.

        The result distinguishes "this session cannot be bulk converted" from
        "the conversion ran and failed", because only the former may be retried
        through the slower chunked path. Guardrail outcomes -- a resource limit,
        a stall, a timeout, cancellation, or a disk error -- are raised, not
        returned, so they cannot be mistaken for either.
        """

        blocker = self._bulk_conversion_blocker()
        if blocker is not None:
            status, reason = blocker
            logger.debug("Bulk conversion not attempted: %s (%s)", reason, status.value)
            reader_perf_event(
                "archive.convert.skipped",
                status=status.value,
                reason=reason,
                pages=self.page_count,
            )
            return ArchiveConversionResult(status, reason, self.page_count)
        if self._cache_is_complete():
            return ArchiveConversionResult(
                ArchiveConversionStatus.ALREADY_PUBLISHED,
                "cache_complete",
                self.page_count,
            )

        assert self._cache_lease is not None and self._bulk_extract is not None
        records = self._pages
        source = records[0].source
        password = records[0].password
        budget = ArchiveOperationBudget(self._limits.max_operation_bytes)
        # Aggregate cap for the whole conversion, distinct from the per-member
        # limit that bounds any single page.
        max_output_bytes = self._conversion_output_cap()

        with self._state_lock:
            if self._closing or self._closed:
                return ArchiveConversionResult(
                    ArchiveConversionStatus.SKIPPED, "session_closing", self.page_count
                )
            self._active_reads += 1
        started = perf_counter()
        result = ArchiveConversionResult(
            ArchiveConversionStatus.FAILED, "incomplete", len(records)
        )
        try:
            reader_perf_event(
                "archive.convert.started",
                pages=len(records),
                declared_bytes=self._declared_page_bytes()[0],
                cap_bytes=max_output_bytes,
                group_items=CONVERSION_GROUP_ITEMS,
                group_target_bytes=CONVERSION_GROUP_TARGET_BYTES,
                identity_kind=self._cache_identity_kind(),
            )
            with TemporaryDirectory(prefix="joyread-convert-") as staging:
                staging_root = Path(staging)
                self._bulk_extract(
                    source,
                    tuple(record.name for record in records),
                    staging_root,
                    password,
                    limits=self._limits,
                    budget=budget,
                    max_output_bytes=max_output_bytes,
                    is_cancelled=is_cancelled,
                )
                result = self._publish_staged_pages(
                    staging_root, budget, is_cancelled=is_cancelled
                )
        finally:
            self._finish_registered_read()
            reader_perf_event(
                "archive.convert.completed"
                if result.is_published
                else "archive.convert.failed",
                status=result.status.value,
                reason=result.reason,
                pages=len(records),
                written_bytes=budget.used,
                elapsed_ms=round((perf_counter() - started) * 1000.0, 3),
            )
        return result

    def _publish_staged_pages(
        self,
        staging_root: Path,
        budget: ArchiveOperationBudget,
        *,
        is_cancelled: Callable[[], bool] | None,
    ) -> ArchiveConversionResult:
        """Move staged pages into the cache, then publish once all are present."""

        assert self._cache_lease is not None
        resolved_root = staging_root.resolve()
        page_keys = tuple(self._cache_page_key(index) for index in range(self.page_count))
        # A container may legally list one member twice, which the scanner
        # flattens into two pages. Both cache keys have to be written from the
        # same staged file, and in the same group, because the file is deleted
        # as soon as its group is confirmed.
        keys_by_member: OrderedDict[str, list[str]] = OrderedDict()
        for page_index, record in enumerate(self._pages):
            keys_by_member.setdefault(record.name, []).append(page_keys[page_index])
        group: dict[str, Path] = {}
        group_bytes = 0

        def flush() -> str | None:
            """Write one group, or return why the conversion must stop."""

            nonlocal group, group_bytes
            if not group:
                return None
            payloads: dict[str, bytes] = {}
            for cache_key, path in group.items():
                payloads[cache_key] = read_file_bounded(
                    path,
                    path.name,
                    max_item_bytes=self._limits.max_extracted_item_bytes,
                    budget=budget,
                )
            if not self._cache_lease.put_many(payloads):
                return "cache_write_failed"
            # Verify from metadata before dropping our only other copy. This is
            # what makes deleting the staged files safe; the whole-document
            # check still happens atomically at publish time.
            present = self._cache_lease.contains_many(tuple(payloads))
            if present != frozenset(payloads):
                return "cache_group_unverified"
            for path in set(group.values()):
                path.unlink(missing_ok=True)
            group = {}
            group_bytes = 0
            return None

        for member_index, (member, cache_keys) in enumerate(keys_by_member.items()):
            if is_cancelled is not None and is_cancelled():
                raise ArchiveCancelled("Archive conversion cancelled.")
            staged = (staging_root / member).resolve()
            if not staged.is_relative_to(resolved_root) or not staged.is_file():
                logger.warning(
                    "Bulk conversion is missing staged member %d of %d; not publishing",
                    member_index + 1,
                    len(keys_by_member),
                )
                return ArchiveConversionResult(
                    ArchiveConversionStatus.FAILED, "staged_page_missing", self.page_count
                )
            size = staged.stat().st_size
            if group and (
                len(group) + len(cache_keys) > CONVERSION_GROUP_ITEMS
                or group_bytes + size > CONVERSION_GROUP_TARGET_BYTES
            ):
                # A page over the target lands in a group of its own here,
                # which is the intended behaviour: see the constant's comment.
                failure = flush()
                if failure is not None:
                    logger.warning("Bulk conversion stopped: %s", failure)
                    return ArchiveConversionResult(
                        ArchiveConversionStatus.FAILED, failure, self.page_count
                    )
            for cache_key in cache_keys:
                group[cache_key] = staged
            group_bytes += size
        failure = flush()
        if failure is not None:
            logger.warning("Bulk conversion stopped: %s", failure)
            return ArchiveConversionResult(
                ArchiveConversionStatus.FAILED, failure, self.page_count
            )
        # Last possible moment: publishing after a cancellation would leave a
        # document marked ready that the caller asked us to abandon.
        if is_cancelled is not None and is_cancelled():
            raise ArchiveCancelled("Archive conversion cancelled.")
        if not self._cache_lease.publish_complete(
            page_keys,
            self.page_count,
            self._cache_signature,
        ):
            logger.warning("Bulk conversion did not verify as a whole document")
            return ArchiveConversionResult(
                ArchiveConversionStatus.FAILED, "document_unverified", self.page_count
            )
        return ArchiveConversionResult(
            ArchiveConversionStatus.PUBLISHED, "", self.page_count
        )

    def _bulk_conversion_blocker(
        self,
    ) -> tuple[ArchiveConversionStatus, str] | None:
        """Why this session must not be bulk converted, or ``None`` if it can."""

        unsupported = ArchiveConversionStatus.UNSUPPORTED
        skipped = ArchiveConversionStatus.SKIPPED
        if self._bulk_extract is None:
            return (unsupported, "no_bulk_backend")
        if not self._can_use_document_cache():
            return (unsupported, "no_document_cache")
        if not self._pages:
            return (skipped, "no_pages")
        if any(not self._record_is_cacheable(record) for record in self._pages):
            return (unsupported, "uncacheable_page")
        # One pass covers one container. A session assembled from several
        # sources, or from a nested archive held in memory, must not be marked
        # complete after converting only part of it.
        if len({id(record.source) for record in self._pages}) != 1:
            return (unsupported, "multiple_sources")
        source = self._pages[0].source
        if source.path is None:
            return (unsupported, "not_path_backed")
        declared, has_unknown = self._declared_page_bytes()
        if has_unknown:
            # Without every declared size neither the pool-fit test nor the
            # aggregate output cap can be computed honestly, and treating an
            # unknown size as zero under-counts both. The chunked path bounds
            # each read on its own and handles this correctly.
            return (skipped, "unknown_page_sizes")
        budget = self._cache_lease.cache_max_bytes if self._cache_lease else 0
        if declared and budget and declared > budget:
            # One book must not monopolise the shared pool: it would evict
            # every other unpinned bundle and keep the pool over budget.
            return (skipped, "larger_than_pool")
        return None

    def _declared_page_bytes(self) -> tuple[int, bool]:
        """Total declared page bytes, and whether any page size is unknown.

        An unknown size must never be folded in as zero: it would silently
        shrink both the pool-fit test and the aggregate conversion cap, and a
        cap derived from a partial total kills the extraction part-way through.
        """

        total = 0
        has_unknown = False
        for record in self._pages:
            if record.size is None:
                has_unknown = True
                continue
            total += max(0, int(record.size))
        return total, has_unknown

    def _conversion_output_cap(self) -> int | None:
        """Aggregate ceiling for one conversion, never the per-member limit."""

        declared, has_unknown = self._declared_page_bytes()
        candidates = [self._limits.max_operation_bytes]
        if declared and not has_unknown:
            candidates.append(int(declared * CONVERSION_DECLARED_TOLERANCE))
        if self._cache_lease is not None and self._cache_lease.cache_max_bytes > 0:
            # Writing more than the whole pool can hold cannot end well even if
            # the operation budget would allow it.
            candidates.append(self._cache_lease.cache_max_bytes)
        known = [value for value in candidates if value is not None]
        return min(known) if known else None

    def mark_thumbnail_cache_ready(self) -> bool:
        if not self._uses_expensive_cache or not self._can_use_document_cache():
            return False
        if any(not self._record_is_cacheable(record) for record in self._pages):
            return False
        expected_keys = tuple(self._cache_page_key(index) for index in range(self.page_count))
        assert self._cache_lease is not None
        # Metadata-only: the old ``get_many`` check read every page back into
        # memory just to count them.
        self._cache_lease.publish_complete(
            expected_keys,
            self.page_count,
            self._cache_signature,
        )
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
