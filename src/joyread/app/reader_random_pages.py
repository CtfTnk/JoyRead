"""Per-Reader demand and result ownership for the shared random-page scheduler."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event
from time import perf_counter
from typing import Generic

from joyread.app.reader_page_models import (
    FrameT, PageFrameDecoder, PreparedReaderPage, ReaderDocumentSource,
    ReaderFrameCache, ReaderPageRequest,
)
from joyread.app.reader_page_scheduler import PageDemand, ReaderPageScheduler
from joyread.app.tasking import TaskExecutor
from joyread.core.diagnostics import reader_perf_enabled, reader_perf_event


class RandomPagePipeline(Generic[FrameT]):
    def __init__(
        self, scheduler: ReaderPageScheduler, executor: TaskExecutor,
        decoder: PageFrameDecoder[FrameT], cache: ReaderFrameCache[FrameT], *,
        on_ready: Callable[[PreparedReaderPage[FrameT]], None],
        on_failed: Callable[[int, Exception, int], None],
        work_group: object | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._executor = executor
        self._decoder = decoder
        self._cache = cache
        self._on_ready = on_ready
        self._on_failed = on_failed
        self._owner = object()
        self._work_group = work_group
        self._wanted: set[tuple] = set()
        self._revision = 0

    def cancel(self) -> None:
        self._revision += 1
        self._wanted.clear()
        self._scheduler.cancel(self._owner)

    def request(
        self, source: ReaderDocumentSource, requests: tuple[ReaderPageRequest, ...],
    ) -> None:
        self._revision += 1
        revision = self._revision
        demands: list[PageDemand] = []
        hits: list[PreparedReaderPage[FrameT]] = []
        wanted: set[tuple] = set()
        for request in requests:
            key = (id(source), request.generation, request.page_index,
                   request.target_width, request.target_height, request.device_pixel_ratio)
            cached = self._cache.get_suitable(
                request.page_index, request.target_width, request.target_height,
            )
            if cached is not None:
                hits.append(PreparedReaderPage(
                    cached.page_index, cached.frame, cached.source_dimensions,
                    cached.rendered_dimensions, request.generation,
                ))
                continue
            wanted.add(key)
            demands.append(PageDemand(
                key, request.priority,
                lambda cancelled, request=request: self._prepare(source, request, cancelled),
                lambda result, key=key, request=request: self._ready(key, request, result),
                lambda error, key=key, request=request: self._failed(key, request, error),
                group=self._work_group,
            ))
        self._wanted = wanted
        self._scheduler.replace(self._owner, self._executor, demands)
        for page in hits:
            if self._revision != revision:
                break
            self._on_ready(page)

    def _prepare(
        self, source: ReaderDocumentSource, request: ReaderPageRequest, cancelled: Event,
    ) -> PreparedReaderPage[FrameT] | None:
        perf_enabled = reader_perf_enabled()
        started = perf_counter() if perf_enabled else 0.0
        try:
            payload = source.read_page(request.page_index)
            if cancelled.is_set():
                return None
            if payload is None:
                raise RuntimeError(f"Page {request.page_index + 1} is unavailable.")
            page = self._decoder.decode(payload, request)
            return None if cancelled.is_set() else page
        finally:
            if perf_enabled:
                reader_perf_event(
                    "reader.prepare.worker", page=request.page_index,
                    generation=request.generation,
                    target=(request.target_width, request.target_height),
                    elapsed_ms=round((perf_counter() - started) * 1000.0, 3),
                )

    def _ready(self, key: tuple, request: ReaderPageRequest, page: PreparedReaderPage[FrameT]) -> None:
        if key not in self._wanted:
            return
        self._wanted.remove(key)
        # Completion is on the GUI thread. Freshness and cache publication are
        # one transaction relative to navigation, resize and session close.
        self._cache.put(request.page_index, page, request.target_width, request.target_height)
        self._on_ready(page)

    def _failed(self, key: tuple, request: ReaderPageRequest, error: Exception) -> None:
        if key in self._wanted:
            self._wanted.remove(key)
            self._on_failed(request.page_index, error, request.generation)
