from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QImage

from joyread.app.reader_page_pipeline import (
    PreparedReaderPage,
    ReaderPagePayload,
    ReaderPagePipeline,
    ReaderPageRequest,
)
from joyread.app.tasking import TaskHandle, TaskPriority, TaskStatus
from joyread.core.services.cache_service import BoundedByteCache, NamespacedPageCache
from joyread.infrastructure.reader_image_decoder import qimage_frame_bytes


class _ManualExecutor:
    def __init__(self) -> None:
        self.tasks: list[tuple[TaskHandle, Callable, Callable, TaskPriority]] = []

    def submit_stream(self, name, callback, *, on_item, on_success=None, on_failure=None, priority=0):  # noqa: ANN001
        del on_success, on_failure
        handle = TaskHandle(task_id=name, status=TaskStatus.RUNNING)
        self.tasks.append((handle, callback, on_item, TaskPriority(priority)))
        return handle

    def run(self, index: int = 0) -> None:
        handle, callback, on_item, _priority = self.tasks[index]
        callback(on_item)
        if handle.status != TaskStatus.CANCELLED:
            handle.status = TaskStatus.COMPLETED


class _ImmediateExecutor:
    def submit(self, name, callback, *, on_success=None, on_failure=None, priority=0):  # noqa: ANN001
        del priority
        handle = TaskHandle(task_id=name, status=TaskStatus.RUNNING)
        try:
            result = callback()
        except Exception as exc:
            handle.status = TaskStatus.FAILED
            if on_failure is not None:
                on_failure(exc)
            return handle
        handle.status = TaskStatus.COMPLETED
        if on_success is not None:
            on_success(result)
        return handle


class _Source:
    page_count = 10

    def __init__(self) -> None:
        self.reads: list[int] = []
        self.closed = False

    def read_page(self, page_index: int) -> ReaderPagePayload:
        self.reads.append(page_index)
        return ReaderPagePayload(page_index, f"page-{page_index}".encode(), (1000, 1500))

    def close(self) -> None:
        self.closed = True


class _Decoder:
    def __init__(self, *, fail: set[int] | None = None) -> None:
        self.decoded: list[int] = []
        self.fail = fail or set()
        self.on_decode: Callable[[int], None] | None = None

    def decode(self, payload: ReaderPagePayload, request: ReaderPageRequest) -> PreparedReaderPage[str]:
        self.decoded.append(payload.page_index)
        if self.on_decode is not None:
            callback, self.on_decode = self.on_decode, None
            callback(payload.page_index)
        if payload.page_index in self.fail:
            raise RuntimeError("decode failed")
        return PreparedReaderPage(
            payload.page_index,
            f"frame-{payload.page_index}",
            payload.source_dimensions,
            (request.target_width, request.target_height),
            request.generation,
        )


class _FrameCache:
    def __init__(self) -> None:
        self.values: dict[tuple[int, int, int], PreparedReaderPage[str]] = {}
        self.clear_count = 0

    def get_suitable(self, page_index: int, target_width: int, target_height: int):  # noqa: ANN201
        return self.values.get((page_index, target_width, target_height))

    def put(self, page_index: int, value, target_width: int, target_height: int) -> None:  # noqa: ANN001
        self.values[(page_index, target_width, target_height)] = value

    def clear(self) -> int:
        count = len(self.values)
        self.values.clear()
        self.clear_count += 1
        return count


def _pipeline(executor, decoder, cache, ready, failed):  # noqa: ANN001, ANN202
    return ReaderPagePipeline(
        executor,
        decoder,
        cache,
        on_ready=ready.append,
        on_failed=lambda index, error, generation: failed.append((index, error, generation)),
    )


def test_pipeline_publishes_visible_center_first_then_nearest_prefetch() -> None:
    executor = _ManualExecutor()
    decoder = _Decoder()
    cache = _FrameCache()
    ready: list[PreparedReaderPage[str]] = []
    failed: list[tuple[int, Exception, int]] = []
    pipeline = _pipeline(executor, decoder, cache, ready, failed)
    source = _Source()
    pipeline.set_source(source, generation=7)

    pipeline.request(
        (4, 5, 6),
        (2, 3, 7, 8),
        target_width=500,
        target_height=700,
        device_pixel_ratio=2.0,
        generation=7,
    )
    assert executor.tasks[0][3] == TaskPriority.CRITICAL
    executor.run()

    assert source.reads == [5, 4, 6, 3, 7, 2, 8]
    assert [page.page_index for page in ready] == source.reads
    assert {page.rendered_dimensions for page in ready} == {(1000, 1400)}
    assert failed == []


def test_pipeline_drops_a_request_cancelled_during_decode_before_cache_or_publish() -> None:
    executor = _ManualExecutor()
    decoder = _Decoder()
    cache = _FrameCache()
    ready: list[PreparedReaderPage[str]] = []
    failed: list[tuple[int, Exception, int]] = []
    pipeline = _pipeline(executor, decoder, cache, ready, failed)
    source = _Source()
    pipeline.set_source(source, generation=3)
    decoder.on_decode = lambda _index: pipeline.request(
        (8,),
        (),
        target_width=400,
        target_height=600,
        device_pixel_ratio=1.0,
        generation=3,
    )

    pipeline.request(
        (1,),
        (),
        target_width=400,
        target_height=600,
        device_pixel_ratio=1.0,
        generation=3,
    )
    old_handle = executor.tasks[0][0]
    executor.run(0)

    assert old_handle.status == TaskStatus.CANCELLED
    assert ready == []
    assert cache.values == {}

    executor.run(1)
    assert [page.page_index for page in ready] == [8]


def test_pipeline_reports_one_decode_failure_and_continues_stream() -> None:
    executor = _ManualExecutor()
    decoder = _Decoder(fail={2})
    cache = _FrameCache()
    ready: list[PreparedReaderPage[str]] = []
    failed: list[tuple[int, Exception, int]] = []
    pipeline = _pipeline(executor, decoder, cache, ready, failed)
    pipeline.set_source(_Source(), generation=1)

    pipeline.request(
        (2, 3),
        (),
        target_width=300,
        target_height=500,
        device_pixel_ratio=1.0,
        generation=1,
    )
    executor.run()

    assert [item[0] for item in failed] == [2]
    assert [page.page_index for page in ready] == [3]


def test_pipeline_cancel_clears_reader_namespace() -> None:
    executor = _ManualExecutor()
    cache = _FrameCache()
    cache.values[(0, 100, 100)] = PreparedReaderPage(0, "frame", (100, 100), (100, 100), 1)
    pipeline = _pipeline(executor, _Decoder(), cache, [], [])
    pipeline.set_source(_Source(), generation=1)

    pipeline.cancel(clear_cache=True)

    assert cache.values == {}
    assert cache.clear_count == 1


def test_pipeline_owns_document_open_and_close_lifecycle() -> None:
    executor = _ImmediateExecutor()
    cache = _FrameCache()
    opened: list[_Source] = []
    failures: list[Exception] = []
    source = _Source()
    pipeline = _pipeline(executor, _Decoder(), cache, [], [])

    pipeline.open_document(
        lambda: source,
        generation=4,
        on_opened=opened.append,
        on_failed=failures.append,
    )

    assert opened == [source]
    assert failures == []
    assert not source.closed

    pipeline.cancel()

    assert source.closed


def test_qimage_frame_cache_uses_scanline_bytes_and_twenty_percent_resize_bucket() -> None:
    backing = BoundedByteCache[tuple, object](10_000, sizer=qimage_frame_bytes)
    namespace = NamespacedPageCache(backing, "reader-a")
    image = QImage(100, 80, QImage.Format.Format_RGBA8888)
    prepared = PreparedReaderPage(0, image, (1000, 800), (100, 80), 1)

    namespace.put(0, prepared, 100, 80)

    assert backing.current_bytes == image.bytesPerLine() * image.height()
    assert namespace.get_suitable(0, 120, 96) is prepared
    assert namespace.get_suitable(0, 121, 97) is None

    namespace.clear()
    assert backing.current_bytes == 0
