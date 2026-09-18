"""Viewport-driven thumbnail loading shared by image-oriented ViewModels."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from joyread.app.event_hook import EventHook
from joyread.app.tasking import TaskExecutor, TaskHandle, TaskPriority, TaskStatus
from joyread.core.archive.batching import MAX_SEQUENTIAL_BATCH_ITEMS
from joyread.core.services.cache_service import ThumbnailCacheClient, ThumbnailCacheKey


ThumbnailEmitter = Callable[["ThumbnailStreamItem"], None]
ThumbnailLoader = Callable[[tuple[int, ...], ThumbnailEmitter], None]
BatchSizeProvider = Callable[[int], int]
BatchPlanner = Callable[[tuple[int, ...]], tuple[int, ...]]


@dataclass(frozen=True)
class ThumbnailStreamItem:
    page_index: int
    image_bytes: bytes


class ThumbnailStreamController:
    """Serial interest scheduler with shared-cache pinning."""

    def __init__(
        self,
        task_service: TaskExecutor,
        cache_client: ThumbnailCacheClient,
        *,
        task_name: str,
    ) -> None:
        self.thumbnail_ready: EventHook[tuple[int, bytes]] = EventHook()
        self.thumbnail_failed: EventHook[int] = EventHook()
        self._task_service = task_service
        self._cache = cache_client
        self._task_name = task_name
        self._source_id: str | None = None
        self._page_count = 0
        self._size = (1, 1)
        self._loader: ThumbnailLoader | None = None
        self._batch_size_for: BatchSizeProvider = lambda _index: 1
        self._batch_planner: BatchPlanner | None = None
        self._visible: tuple[int, ...] = ()
        self._prefetch: tuple[int, ...] = ()
        self._interest: frozenset[int] = frozenset()
        self._delivered: set[int] = set()
        self._direction = 0
        self._queue: list[int] = []
        self._active_indices: tuple[int, ...] = ()
        self._handle: TaskHandle[object] | None = None
        self._generation = 0
        self._submitting = False
        self._pump_deferred = False
        self._preview_index: int | None = None
        self._preview_loader: ThumbnailLoader | None = None
        self._preview_enabled = False
        self._active_preview = False

    def set_preview(self, index: int | None, loader: ThumbnailLoader | None = None, *, enabled: bool = True) -> None:
        """Replace only preview demand; an executing batch drains into the cache."""
        self._preview_index = index if index is not None and 0 <= index < self._page_count else None
        self._preview_loader = loader
        self._preview_enabled = enabled
        self._delivered.discard(self._preview_index)
        visible, prefetch = self._visible, self._prefetch
        self.set_interest(visible, prefetch, force=True)

    @property
    def source_id(self) -> str | None:
        return self._source_id

    @property
    def page_count(self) -> int:
        return self._page_count

    @property
    def active_indices(self) -> tuple[int, ...]:
        return self._active_indices

    def set_source(
        self,
        source_id: str,
        page_count: int,
        size: tuple[int, int],
        loader: ThumbnailLoader,
        *,
        batch_size_for: BatchSizeProvider | None = None,
        batch_planner: BatchPlanner | None = None,
    ) -> None:
        normalized_size = (max(1, int(size[0])), max(1, int(size[1])))
        if (
            source_id == self._source_id
            and page_count == self._page_count
            and normalized_size == self._size
            and loader is self._loader
        ):
            return
        self.cancel()
        self._source_id = source_id
        self._page_count = max(0, int(page_count))
        self._size = normalized_size
        self._loader = loader
        self._batch_size_for = batch_size_for or (lambda _index: 1)
        self._batch_planner = batch_planner

    def promote_source(self, source_id: str) -> None:
        """Re-key cached variants after an external document gains a hash identity."""

        target = str(source_id).strip()
        previous = self._source_id
        if not target or previous is None or target == previous:
            return
        self._generation += 1
        if self._handle is not None:
            self._handle.cancel()
        self._handle = None
        self._active_indices = ()
        self._queue = []
        self._cache.promote_source(previous, target)
        self._source_id = target
        self._cache.set_pins(frozenset(self._key(index) for index in self._interest))
        visible, prefetch = self._visible, self._prefetch
        self._visible = ()
        self._prefetch = ()
        self.set_interest(visible, prefetch)

    def set_interest(
        self,
        visible_indices: Iterable[int],
        prefetch_indices: Iterable[int] = (),
        *, force: bool = False,
    ) -> None:
        if self._source_id is None or self._loader is None or self._page_count <= 0:
            self.release_interest()
            return
        visible = _unique_valid_indices(visible_indices, self._page_count)
        visible_set = set(visible)
        prefetch = tuple(
            index
            for index in _unique_valid_indices(prefetch_indices, self._page_count)
            if index not in visible_set
        )
        if not force and visible == self._visible and prefetch == self._prefetch:
            return

        # Scrolling changes demand, not source identity. A bounded running batch
        # finishes once; its useful results survive and the next batch uses the
        # latest queue. Cancelling here cannot interrupt decoding/archive I/O
        # and would create overlapping workers whose results are thrown away.
        if visible and self._visible:
            movement = (min(visible) + max(visible)) - (min(self._visible) + max(self._visible))
            if movement:
                self._direction = 1 if movement > 0 else -1
        self._visible = visible
        self._prefetch = prefetch
        preview = () if self._preview_index is None else (self._preview_index,)
        self._interest = frozenset((*visible, *prefetch, *preview))
        self._delivered.intersection_update(self._interest)
        self._cache.set_pins(frozenset(self._key(index) for index in self._interest))

        ordered = _center_out(visible)
        if visible and self._direction:
            low, high = min(visible), max(visible)
            prefetch = tuple(sorted(prefetch, key=lambda index: (
                0 if (index > high if self._direction > 0 else index < low) else 1,
                min(abs(index - low), abs(index - high)),
            )))
        ordered.extend(prefetch)
        ordered = list(dict.fromkeys((*preview, *ordered)))
        missing: list[int] = []
        for page_index in ordered:
            if page_index in self._delivered:
                continue
            cached = self._cache.get(self._key(page_index))
            if cached is None:
                if page_index == self._preview_index and not self._preview_enabled and page_index not in (*visible, *prefetch):
                    continue
                if page_index not in self._active_indices:
                    missing.append(page_index)
                continue
            self._delivered.add(page_index)
            self.thumbnail_ready.emit(page_index, cached)

        self._queue = missing
        self._pump()

    def release_interest(self) -> None:
        self._generation += 1
        if self._handle is not None:
            self._handle.cancel()
        self._handle = None
        self._active_indices = ()
        self._queue = []
        self._visible = ()
        self._prefetch = ()
        self._interest = frozenset()
        self._delivered.clear()
        self._direction = 0
        self._cache.release()

    def cancel(self) -> None:
        self.release_interest()
        self._source_id = None
        self._page_count = 0
        self._loader = None
        self._batch_size_for = lambda _index: 1
        self._batch_planner = None
        self._preview_index = None
        self._preview_loader = None
        self._preview_enabled = False

    def refresh(self) -> None:
        self._delivered.clear()
        visible = self._visible
        prefetch = self._prefetch
        self._visible = ()
        self._prefetch = ()
        self.set_interest(visible, prefetch)

    def _pump(self) -> None:
        if self._handle is not None and self._handle.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            return
        self._handle = None
        self._active_indices = ()
        if self._loader is None or not self._queue:
            return

        selected = self._planned_prefix()
        self._queue = self._queue[len(selected) :]
        self._active_indices = selected
        generation = self._generation
        priority = TaskPriority.HIGH if any(index in self._visible for index in selected) else TaskPriority.NORMAL

        # Topic demand retains its normal source policy even when preview also
        # points at that page; preview must not downgrade it to cache-only.
        preview_batch = (
            selected == (self._preview_index,)
            and self._preview_loader is not None
            and self._preview_enabled
            and self._preview_index not in (*self._visible, *self._prefetch)
        )
        loader = self._preview_loader if preview_batch else self._loader
        self._active_preview = preview_batch
        if preview_batch:
            priority = TaskPriority.NORMAL

        def work(emit_item: ThumbnailEmitter) -> None:
            loader(selected, emit_item)

        submit_stream = getattr(self._task_service, "submit_stream", None)
        if callable(submit_stream):
            self._submitting = True
            try:
                handle = submit_stream(
                    f"{self._task_name}-{selected[0]}",
                    work,
                    on_item=lambda item, generation=generation: self._handle_item(generation, item),
                    on_success=lambda _result, generation=generation: self._handle_finished(generation),
                    on_failure=lambda _error, generation=generation: self._handle_failed(generation, selected),
                    priority=priority,
                )
                if not self._pump_deferred and handle.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
                    self._handle = handle
            finally:
                self._submitting = False
                self._resume_deferred_pump()
            return

        items: list[ThumbnailStreamItem] = []

        def collect() -> None:
            work(items.append)

        submit = self._task_service.submit
        self._submitting = True
        try:
            try:
                handle = submit(
                    f"{self._task_name}-{selected[0]}",
                    collect,
                    on_success=lambda _result, generation=generation: self._handle_collected(generation, items),
                    on_failure=lambda _error, generation=generation: self._handle_failed(generation, selected),
                    priority=priority,
                )
            except TypeError:
                handle = submit(
                    f"{self._task_name}-{selected[0]}",
                    collect,
                    on_success=lambda _result, generation=generation: self._handle_collected(generation, items),
                    on_failure=lambda _error, generation=generation: self._handle_failed(generation, selected),
                )
            if not self._pump_deferred and handle.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
                self._handle = handle
        finally:
            self._submitting = False
            self._resume_deferred_pump()

    def _planned_prefix(self) -> tuple[int, ...]:
        candidates = tuple(self._queue[:MAX_SEQUENTIAL_BATCH_ITEMS])
        if not candidates:
            return ()
        if candidates[0] == self._preview_index and self._preview_enabled:
            return candidates[:1]
        if self._batch_planner is None:
            batch_size = max(
                1,
                min(MAX_SEQUENTIAL_BATCH_ITEMS, int(self._batch_size_for(candidates[0]))),
            )
            return candidates[:batch_size]
        planned = tuple(dict.fromkeys(int(index) for index in self._batch_planner(candidates)))
        prefix: list[int] = []
        for candidate in candidates:
            if candidate not in planned:
                break
            prefix.append(candidate)
        return tuple(prefix) or (candidates[0],)

    def _handle_collected(self, generation: int, items: list[ThumbnailStreamItem]) -> None:
        for item in items:
            self._handle_item(generation, item)
        self._handle_finished(generation)

    def _handle_item(self, generation: int, item: ThumbnailStreamItem) -> None:
        if generation != self._generation or self._source_id is None:
            return
        page_index = int(item.page_index)
        if not 0 <= page_index < self._page_count:
            return
        self._cache.put(self._key(page_index), item.image_bytes)
        if page_index in self._interest:
            self._delivered.add(page_index)
            self.thumbnail_ready.emit(page_index, item.image_bytes)

    def _handle_finished(self, generation: int) -> None:
        if generation != self._generation:
            return
        # Topic may have requested a page while a cache-only preview was
        # already checking it. An empty check must not swallow that new demand.
        if self._active_preview:
            topic = frozenset((*self._visible, *self._prefetch))
            needed = [index for index in self._active_indices if index in topic and index not in self._delivered]
            self._queue = list(dict.fromkeys((*needed, *self._queue)))
        self._active_preview = False
        self._handle = None
        self._active_indices = ()
        if self._submitting:
            self._pump_deferred = True
        else:
            self._pump()

    def _handle_failed(self, generation: int, indices: tuple[int, ...]) -> None:
        if generation != self._generation:
            return
        for page_index in indices:
            if page_index in self._interest:
                self.thumbnail_failed.emit(page_index)
        self._handle = None
        self._active_indices = ()
        if self._submitting:
            self._pump_deferred = True
        else:
            self._pump()

    def _resume_deferred_pump(self) -> None:
        if not self._pump_deferred:
            return
        self._pump_deferred = False
        self._pump()

    def _key(self, page_index: int) -> ThumbnailCacheKey:
        assert self._source_id is not None
        return ThumbnailCacheKey(self._source_id, int(page_index), self._size[0], self._size[1])


def _unique_valid_indices(indices: Iterable[int], page_count: int) -> tuple[int, ...]:
    return tuple(dict.fromkeys(int(index) for index in indices if 0 <= int(index) < page_count))


def _center_out(indices: tuple[int, ...]) -> list[int]:
    if len(indices) <= 1:
        return list(indices)
    ordered = sorted(indices)
    center = (ordered[0] + ordered[-1]) / 2.0
    return sorted(ordered, key=lambda index: (abs(index - center), index))
