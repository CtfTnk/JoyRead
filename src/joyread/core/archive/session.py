"""Thread-safe archive page session and extraction-cache access."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
import hashlib
from io import BytesIO
from threading import RLock

from PIL import Image, UnidentifiedImageError

from joyread.core.archive.errors import ArchiveReadError, ArchiveResourceLimitError
from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget, ensure_item_size
from joyread.core.archive.models import ArchiveAccessMode, ArchiveContentsEntry, ArchivePage
from joyread.core.archive.records import ArchiveSource, PageRecord
from joyread.core.services.archive_extraction_pool import ArchiveExtractionCache


EXPENSIVE_ARCHIVE_EXTENSIONS = frozenset({".7z", ".cb7", ".rar", ".cbr"})


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
        cache_signature: str = "",
        limits: ArchiveOpenLimits | None = None,
    ) -> None:
        self._pages = list(pages)
        self._read_entries = read_entries
        self._contents = tuple(contents)
        self._document_cache_key = document_cache_key
        self._extraction_cache = extraction_cache
        self._cache_signature = cache_signature
        self._limits = limits or ArchiveOpenLimits()
        self._lock = RLock()
        self._uses_expensive_cache = any(
            record.source.suffix in EXPENSIVE_ARCHIVE_EXTENSIONS
            for record in self._pages
        )
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
        with self._lock:
            if not self._uses_expensive_cache:
                return ArchiveAccessMode.DIRECT
            return (
                ArchiveAccessMode.EXPENSIVE_READY
                if self._cache_is_complete()
                else ArchiveAccessMode.EXPENSIVE_COLD
            )

    def access_mode_for(self, page_index: int) -> ArchiveAccessMode:
        with self._lock:
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

    def thumbnail_batch_size(self, page_index: int) -> int:
        return 8 if self.access_mode_for(page_index) == ArchiveAccessMode.EXPENSIVE_COLD else 1

    def mark_thumbnail_cache_ready(self) -> bool:
        with self._lock:
            if not self._uses_expensive_cache or not self._can_use_document_cache():
                return False
            if any(not self._record_is_cacheable(record) for record in self._pages):
                return False
            assert self._document_cache_key is not None
            assert self._extraction_cache is not None
            expected_keys = tuple(self._cache_page_key(index) for index in range(self.page_count))
            if len(self._extraction_cache.get_many(self._document_cache_key, expected_keys)) != self.page_count:
                return False
            self._extraction_cache.mark_complete(
                self._document_cache_key,
                self.page_count,
                self._cache_signature,
            )
            return self._cache_is_complete()

    def is_not_empty(self) -> bool:
        return self.page_count > 0

    def is_valid_index(self, index: int) -> bool:
        return 0 <= index < self.page_count

    def has_next(self, index: int | None = None) -> bool:
        with self._lock:
            checked_index = self.current_index if index is None else index
            return self.is_valid_index(checked_index + 1)

    def has_previous(self, index: int | None = None) -> bool:
        with self._lock:
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
        with self._lock:
            if not self.is_valid_index(index):
                return None
            record = self._pages[index]
            if record.dimensions is not None:
                return record.dimensions
            budget = ArchiveOperationBudget(self._limits.max_operation_bytes)
            if self._record_is_cacheable(record):
                assert self._document_cache_key is not None
                assert self._extraction_cache is not None
                cached = self._extraction_cache.get(self._document_cache_key, self._cache_page_key(index))
                if cached is not None:
                    ensure_item_size(len(cached), self._limits.max_extracted_item_bytes, record.display_path)
                    budget.consume(len(cached), record.display_path)
                    dimensions = _required_dimensions(cached, self._limits, record.display_path)
                    record.dimensions = dimensions
                    return dimensions
            payload = self._read_entries(
                record.source,
                ((record.name, record.password),),
                budget,
            ).get(record.name)
            if payload is None:
                return None
            dimensions = _required_dimensions(payload, self._limits, record.display_path)
            record.dimensions = dimensions
            if self._record_is_cacheable(record):
                assert self._document_cache_key is not None
                assert self._extraction_cache is not None
                self._extraction_cache.put(self._document_cache_key, self._cache_page_key(index), payload)
            return dimensions

    def get_page(self, index: int) -> ArchivePage | None:
        return self.get_pages((index,))[0]

    def get_pages(self, indices: Iterable[int]) -> list[ArchivePage | None]:
        with self._lock:
            requested = list(indices)
            results: list[ArchivePage | None] = [None] * len(requested)
            missing: list[tuple[int, int, PageRecord]] = []
            budget = ArchiveOperationBudget(self._limits.max_operation_bytes)

            for result_index, page_index in enumerate(requested):
                if not self.is_valid_index(page_index):
                    continue
                record = self._pages[page_index]
                if self._record_is_cacheable(record):
                    assert self._document_cache_key is not None
                    assert self._extraction_cache is not None
                    cached = self._extraction_cache.get(self._document_cache_key, self._cache_page_key(page_index))
                    if cached is not None:
                        # A cache hit is still an archive read for the current
                        # session policy: check both byte budgets and pixels.
                        ensure_item_size(len(cached), self._limits.max_extracted_item_bytes, record.display_path)
                        budget.consume(len(cached), record.display_path)
                        page = archive_page_from_bytes(page_index, record, cached, self._limits)
                        if page is not None:
                            record.dimensions = page.dimensions
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
                    record.dimensions = page.dimensions
                    results[result_index] = page
                    if self._record_is_cacheable(record):
                        cache_payloads[self._cache_page_key(page_index)] = payload

            if cache_payloads:
                assert self._document_cache_key is not None
                assert self._extraction_cache is not None
                self._extraction_cache.put_many(self._document_cache_key, cache_payloads)

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
        with self._lock:
            return self.get_image(self.current_index)

    def seek(self, index: int) -> bool:
        with self._lock:
            if not self.is_valid_index(index):
                return False
            self.current_index = index
            return True

    def next(self) -> bytes | None:
        with self._lock:
            if not self.has_next():
                return None
            self.current_index += 1
            return self.get_image(self.current_index)

    def previous(self) -> bytes | None:
        with self._lock:
            if not self.has_previous():
                return None
            self.current_index -= 1
            return self.get_image(self.current_index)

    def _can_use_document_cache(self) -> bool:
        return (
            self._uses_expensive_cache
            and self._document_cache_key is not None
            and self._extraction_cache is not None
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
        assert self._document_cache_key is not None
        assert self._extraction_cache is not None
        return self._extraction_cache.is_complete(
            self._document_cache_key,
            self.page_count,
            self._cache_signature,
        )

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
