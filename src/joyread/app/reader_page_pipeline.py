"""Application-layer orchestration for responsive reader page preparation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Generic, Protocol, TypeVar

from joyread.app.tasking import TaskExecutor, TaskHandle, TaskPriority


FrameT = TypeVar("FrameT")


@dataclass(frozen=True)
class ReaderPageRequest:
    page_index: int
    target_width: int
    target_height: int
    device_pixel_ratio: float
    generation: int
    priority: TaskPriority = TaskPriority.HIGH


@dataclass(frozen=True)
class ReaderPagePayload:
    page_index: int
    image_bytes: bytes
    source_dimensions: tuple[int, int]


@dataclass(frozen=True)
class PreparedReaderPage(Generic[FrameT]):
    page_index: int
    frame: FrameT
    source_dimensions: tuple[int, int]
    rendered_dimensions: tuple[int, int]
    generation: int

    @property
    def dimensions(self) -> tuple[int, int]:
        """Compatibility name consumed by the pure layout engine."""

        return self.source_dimensions


class ReaderDocumentSource(Protocol):
    page_count: int

    def read_page(self, page_index: int) -> ReaderPagePayload | None: ...

    def close(self) -> None: ...


class PageFrameDecoder(Protocol[FrameT]):
    def decode(
        self,
        payload: ReaderPagePayload,
        request: ReaderPageRequest,
    ) -> PreparedReaderPage[FrameT]: ...


class EncodedPageFrameDecoder:
    """Qt-free compatibility decoder used by focused ViewModel tests."""

    def decode(
        self,
        payload: ReaderPagePayload,
        request: ReaderPageRequest,
    ) -> PreparedReaderPage[bytes]:
        return PreparedReaderPage(
            page_index=payload.page_index,
            frame=payload.image_bytes,
            source_dimensions=payload.source_dimensions,
            rendered_dimensions=payload.source_dimensions,
            generation=request.generation,
        )


class SessionReaderDocumentSource:
    """Application adapter hiding blocking Core sessions from ViewModels."""

    def __init__(
        self,
        session: object,
        page_loader: Callable[[object, tuple[int, ...]], dict[int, object]] | None = None,
    ) -> None:
        self._session = session
        self._page_loader = page_loader

    @property
    def page_count(self) -> int:
        return max(0, int(getattr(self._session, "page_count", 0)))

    @property
    def contents(self) -> tuple:
        return tuple(getattr(self._session, "contents", ()))

    @property
    def access_mode(self):  # noqa: ANN201 - archive enum or absent for PDF.
        return getattr(self._session, "access_mode", None)

    def thumbnail_batch_size(self, page_index: int) -> int:
        provider = getattr(self._session, "thumbnail_batch_size", None)
        return max(1, min(8, int(provider(page_index)))) if callable(provider) else 1

    @property
    def requires_sequential_warmup(self) -> bool:
        return bool(getattr(self._session, "requires_sequential_warmup", False))

    def read_page(self, page_index: int) -> ReaderPagePayload | None:
        return self.read_pages((page_index,)).get(page_index)

    def read_pages(self, page_indices: tuple[int, ...]) -> dict[int, ReaderPagePayload]:
        if self._page_loader is not None:
            loaded = self._page_loader(self._session, page_indices)
            pages = [loaded.get(index) for index in page_indices]
        elif callable(reader := getattr(self._session, "read_pages", None)):
            pages = reader(page_indices)
        else:
            raise TypeError("Reader sessions must expose read_pages() or use a page loader.")
        payloads: dict[int, ReaderPagePayload] = {}
        for requested_index, page in zip(page_indices, pages, strict=False):
            if page is None:
                continue
            loaded_index = getattr(page, "index", getattr(page, "page_index", requested_index))
            dimensions = getattr(page, "dimensions", None)
            if dimensions is None:
                raise RuntimeError(f"Page {requested_index + 1} has no source dimensions.")
            payloads[int(loaded_index)] = ReaderPagePayload(
                page_index=int(loaded_index),
                image_bytes=page.image_bytes,
                source_dimensions=(int(dimensions[0]), int(dimensions[1])),
            )
        return payloads

    def prepare_page(self, request: ReaderPageRequest, decoder: PageFrameDecoder[FrameT]):
        direct = getattr(self._session, "prepare_page", None)
        if callable(direct):
            return direct(request, decoder)
        payload = self.read_page(request.page_index)
        if payload is None:
            raise RuntimeError(f"Page {request.page_index + 1} is unavailable.")
        return decoder.decode(payload, request)

    def promote_cache(self, persistent_key: str) -> bool:
        promote = getattr(self._session, "promote_cache", None)
        return bool(promote(persistent_key)) if callable(promote) else False

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()


class ReaderFrameCache(Protocol[FrameT]):
    def get_suitable(
        self,
        page_index: int,
        target_width: int,
        target_height: int,
    ) -> PreparedReaderPage[FrameT] | None: ...

    def put(
        self,
        page_index: int,
        value: PreparedReaderPage[FrameT],
        target_width: int,
        target_height: int,
    ) -> None: ...

    def clear(self) -> int: ...


@dataclass(frozen=True)
class _PipelineItem(Generic[FrameT]):
    page_index: int
    generation: int
    request_token: int
    prepared: PreparedReaderPage[FrameT] | None = None
    error: Exception | None = None


class ReaderPagePipeline(Generic[FrameT]):
    """Maintain one replaceable page-preparation task for a reader viewport."""

    def __init__(
        self,
        executor: TaskExecutor,
        decoder: PageFrameDecoder[FrameT],
        frame_cache: ReaderFrameCache[FrameT],
        *,
        on_ready: Callable[[PreparedReaderPage[FrameT]], None],
        on_failed: Callable[[int, Exception, int], None],
    ) -> None:
        self._executor = executor
        self._decoder = decoder
        self._cache = frame_cache
        self._on_ready = on_ready
        self._on_failed = on_failed
        self._source: ReaderDocumentSource | None = None
        self._generation = 0
        self._request_token = 0
        self._handle: TaskHandle[object] | None = None
        self._open_handle: TaskHandle[ReaderDocumentSource] | None = None
        self._pending_indices: set[int] = set()
        self._lock = RLock()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def set_source(self, source: ReaderDocumentSource, generation: int) -> None:
        with self._lock:
            self._cancel_page_locked()
            self._source = source
            self._generation = int(generation)

    def open_document(
        self,
        source_factory: Callable[[], ReaderDocumentSource],
        *,
        generation: int,
        on_opened: Callable[[ReaderDocumentSource], None],
        on_failed: Callable[[Exception], None],
    ) -> None:
        """Open and adopt a document without exposing worker handles to the VM."""

        with self._lock:
            previous = self._detach_source_locked()
            self._generation = int(generation)
            open_token = self._request_token
        self._close_source(previous)

        def complete(source: ReaderDocumentSource) -> None:
            with self._lock:
                if self._generation != generation or self._request_token != open_token:
                    accepted = False
                else:
                    self._source = source
                    self._open_handle = None
                    accepted = True
            if not accepted:
                self._close_source(source)
                return
            on_opened(source)

        def fail(error: Exception) -> None:
            with self._lock:
                if self._generation != generation or self._request_token != open_token:
                    return
                self._open_handle = None
            on_failed(error)

        try:
            handle = self._executor.submit(
                "reader-open",
                source_factory,
                on_success=complete,
                on_failure=fail,
                on_discard=lambda source: source.close(),
                priority=TaskPriority.CRITICAL,
            )
        except TypeError:
            handle = self._executor.submit(
                "reader-open",
                source_factory,
                on_success=complete,
                on_failure=fail,
            )
        with self._lock:
            if self._generation == generation and self._request_token == open_token:
                self._open_handle = handle
            else:
                handle.cancel()

    def request(
        self,
        visible_indices: tuple[int, ...],
        prefetch_indices: tuple[int, ...],
        *,
        target_width: int,
        target_height: int,
        device_pixel_ratio: float,
        generation: int,
    ) -> None:
        width = max(1, int(round(target_width * max(1.0, device_pixel_ratio))))
        height = max(1, int(round(target_height * max(1.0, device_pixel_ratio))))
        with self._lock:
            if self._source is None or generation != self._generation:
                return
            source = self._source
            ordered = _ordered_interest(visible_indices, prefetch_indices, source.page_count)
            if ordered and set(ordered).issubset(self._pending_indices):
                return
            self._cancel_page_locked()
            request_token = self._request_token

        missing: list[ReaderPageRequest] = []
        visible = frozenset(visible_indices)
        for page_index in ordered:
            cached = self._cache.get_suitable(page_index, width, height)
            if cached is not None:
                self._publish_ready(
                    PreparedReaderPage(
                        page_index=cached.page_index,
                        frame=cached.frame,
                        source_dimensions=cached.source_dimensions,
                        rendered_dimensions=cached.rendered_dimensions,
                        generation=generation,
                    ),
                    request_token=request_token,
                )
                continue
            missing.append(
                ReaderPageRequest(
                    page_index=page_index,
                    target_width=width,
                    target_height=height,
                    device_pixel_ratio=max(1.0, float(device_pixel_ratio)),
                    generation=generation,
                    priority=TaskPriority.CRITICAL if page_index in visible else TaskPriority.LOW,
                )
            )
        if not missing:
            return
        with self._lock:
            self._pending_indices = {request.page_index for request in missing}

        def prepare(emit: Callable[[_PipelineItem[FrameT]], None]) -> None:
            for request in missing:
                if not self._is_current(source, generation, request_token):
                    return
                try:
                    direct = getattr(source, "prepare_page", None)
                    if callable(direct):
                        page = direct(request, self._decoder)
                    else:
                        payload = source.read_page(request.page_index)
                        if payload is None:
                            raise RuntimeError(f"Page {request.page_index + 1} is unavailable.")
                        page = self._decoder.decode(payload, request)
                    if not self._is_current(source, generation, request_token):
                        return
                    self._cache.put(
                        request.page_index,
                        page,
                        request.target_width,
                        request.target_height,
                    )
                    emit(_PipelineItem(request.page_index, generation, request_token, prepared=page))
                except Exception as exc:  # One bad page must not stall the rest.
                    emit(_PipelineItem(request.page_index, generation, request_token, error=exc))

        priority = TaskPriority.CRITICAL if visible else TaskPriority.LOW
        submit_stream = getattr(self._executor, "submit_stream", None)
        if callable(submit_stream):
            handle = submit_stream(
                "reader-pages-stream",
                prepare,
                on_item=self._publish_item,
                priority=priority,
            )
        else:
            def prepare_compat() -> None:
                prepare(self._publish_item)

            try:
                handle = self._executor.submit(
                    "reader-pages-stream",
                    prepare_compat,
                    on_success=lambda _result: None,
                    priority=priority,
                )
            except TypeError:
                handle = self._executor.submit(
                    "reader-pages-stream",
                    prepare_compat,
                    on_success=lambda _result: None,
                )
        with self._lock:
            if self._source is source and self._generation == generation:
                self._handle = handle
            else:
                handle.cancel()

    def cancel(self, *, clear_cache: bool = False) -> None:
        with self._lock:
            previous = self._detach_source_locked()
            self._generation += 1
        self._close_source(previous)
        if clear_cache:
            self._cache.clear()

    def _publish_item(self, item: _PipelineItem[FrameT]) -> None:
        with self._lock:
            if item.request_token != self._request_token:
                return
            self._pending_indices.discard(item.page_index)
        if item.error is not None:
            if self._is_generation_current(item.generation, item.request_token):
                self._on_failed(item.page_index, item.error, item.generation)
            return
        if item.prepared is not None:
            self._publish_ready(item.prepared, request_token=item.request_token)

    def _publish_ready(
        self,
        page: PreparedReaderPage[FrameT],
        *,
        request_token: int | None = None,
    ) -> None:
        if self._is_generation_current(page.generation, request_token):
            self._on_ready(page)

    def _is_generation_current(
        self,
        generation: int,
        request_token: int | None = None,
    ) -> bool:
        with self._lock:
            return (
                self._source is not None
                and self._generation == generation
                and (request_token is None or request_token == self._request_token)
            )

    def _is_current(
        self,
        source: ReaderDocumentSource,
        generation: int,
        request_token: int,
    ) -> bool:
        with self._lock:
            return (
                self._source is source
                and self._generation == generation
                and self._request_token == request_token
            )

    def _cancel_page_locked(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
        self._handle = None
        self._pending_indices.clear()
        self._request_token += 1

    def _detach_source_locked(self) -> ReaderDocumentSource | None:
        self._cancel_page_locked()
        if self._open_handle is not None:
            self._open_handle.cancel()
        self._open_handle = None
        source = self._source
        self._source = None
        return source

    def _close_source(self, source: ReaderDocumentSource | None) -> None:
        if source is None:
            return
        submit = getattr(self._executor, "submit", None)
        if not callable(submit):
            source.close()
            return
        try:
            submit(
                "reader-source-close",
                source.close,
                priority=TaskPriority.LOW,
            )
        except TypeError:
            submit("reader-source-close", source.close)


def _ordered_interest(
    visible_indices: tuple[int, ...],
    prefetch_indices: tuple[int, ...],
    page_count: int,
) -> tuple[int, ...]:
    visible = tuple(dict.fromkeys(index for index in visible_indices if 0 <= index < page_count))
    if visible:
        center = sum(visible) / len(visible)
        ordered_visible = sorted(visible, key=lambda index: (abs(index - center), index))
    else:
        center = 0.0
        ordered_visible = []
    prefetch = tuple(
        dict.fromkeys(
            index
            for index in prefetch_indices
            if 0 <= index < page_count and index not in visible
        )
    )
    ordered_prefetch = sorted(prefetch, key=lambda index: (abs(index - center), index))
    return tuple(ordered_visible + ordered_prefetch)
