"""Retired random-page work stops between native stages and releases normally."""

from threading import Event

import pytest
from PIL import Image
from PySide6.QtGui import QImage

from joyread.app.reader_page_scheduler import ReaderPageScheduler
from joyread.app.thumbnail_stream import ThumbnailStreamController, ThumbnailStreamItem
from joyread.core.thumbnail_cancellation import ThumbnailCancelled
from joyread.infrastructure.qt_task_service import TaskService
from joyread.infrastructure import thumbnail_renderer as rendering
from tests.unit.test_thumbnail_service import _thumbnail_service, _synthetic_book, _png_bytes


@pytest.mark.parametrize("change", ["scroll", "release", "size", "source"])
def test_cancel_during_read_skips_render_and_drains_slot(tmp_path, qtbot, monkeypatch, change):
    service = _thumbnail_service(tmp_path)
    source = service.open_thumbnail_source(_synthetic_book(tmp_path, pages=3))
    assert source is not None
    entered, release = Event(), Event()
    read = source.read_thumbnail_pages
    def blocking_read(*args):
        pages = read(*args)
        entered.set()
        assert release.wait(4)
        return pages
    monkeypatch.setattr(source, "read_thumbnail_pages", blocking_read)
    renders = []
    monkeypatch.setattr(service, "_render_thumbnail_page", lambda *a, **kw: renders.append(a) or b"rendered")
    tasks, scheduler = TaskService(2), ReaderPageScheduler(capacity=1)
    cache = service.issue_thumbnail_cache_client("cancellation-test")
    controller = ThumbnailStreamController(tasks, cache, task_name="cancel-test", random_page_scheduler=scheduler)
    received, failures = [], []
    controller.thumbnail_ready.connect(lambda *item: received.append(item))
    controller.thumbnail_failed.connect(failures.append)
    def load(indices, emit, cancelled=None):
        service.stream_thumbnails(source, indices, (30, 40),
                                  lambda item: emit(ThumbnailStreamItem(item.page_index, item.image_bytes)),
                                  cancelled=cancelled)
    try:
        controller.set_source(source.source_id, 3, (30, 40), load, allows_parallel=lambda _: True)
        controller.set_interest((0,))
        qtbot.waitUntil(entered.is_set)
        if change == "scroll":
            controller.set_interest((1,))
        elif change == "release":
            controller.release_interest()
        else:
            controller.set_source("new" if change == "source" else source.source_id, 3,
                                  (60, 80) if change == "size" else (30, 40), load,
                                  allows_parallel=lambda _: True)
        assert scheduler.active_count == 1
        # Withdraw the replacement too: this test observes only the old read.
        controller.release_interest()
        release.set()
        qtbot.waitUntil(lambda: scheduler.active_count == 0 and tasks.pending_task_count() == 0)
        assert renders == [] and received == [] and failures == []
    finally:
        release.set()
        controller.cancel()
        scheduler.close()
        tasks.shutdown()
        source.close()
        service.close()


@pytest.mark.parametrize("stage", ["decode", "resize", "blur", "foreground", "encode"])
def test_cancellation_skips_later_render_stages(monkeypatch, stage):
    cancelled = Event()
    calls = []
    def observe(owner, method, label):
        original = getattr(owner, method)
        def call(*args, **kwargs):
            calls.append(label)
            result = original(*args, **kwargs)
            if label == stage:
                cancelled.set()
            return result
        monkeypatch.setattr(owner, method, call)
    payload = _png_bytes((300, 500))
    observe(rendering.QImageReader, "read", "decode")
    observe(rendering, "_resize_to_fill", "resize")
    observe(Image.Image, "filter", "blur")
    observe(rendering, "_resize_to_contain", "foreground")
    observe(Image.Image, "save", "encode")
    with pytest.raises(ThumbnailCancelled):
        rendering.QtThumbnailRenderer().render_encoded(payload, (100, 142), cancelled=cancelled)
    stages = ["decode", "resize", "blur", "foreground", "encode"]
    assert calls == stages[:stages.index(stage) + 1]


@pytest.mark.parametrize("renderer_type", [rendering.QtThumbnailRenderer, rendering.PillowThumbnailRenderer])
def test_cancel_before_decode_and_prepared_render_does_no_work(monkeypatch, renderer_type):
    cancelled = Event()
    cancelled.set()
    renderer = renderer_type()
    with pytest.raises(ThumbnailCancelled):
        renderer.render_encoded(b"not even a valid image", (100, 142), cancelled=cancelled)
    with pytest.raises(ThumbnailCancelled):
        renderer.render_prepared(None, (100, 142), cancelled=cancelled)


@pytest.mark.parametrize("size", [(100, 142), (125, 178), (200, 284)])
@pytest.mark.parametrize("renderer_type", [rendering.QtThumbnailRenderer, rendering.PillowThumbnailRenderer])
def test_live_cancellation_token_preserves_exact_encoded_and_prepared_output(size, renderer_type):
    renderer = renderer_type()
    payload = _png_bytes((320, 150), "#35a275")
    assert renderer.render_encoded(payload, size, cancelled=Event()) == renderer.render_encoded(payload, size)
    frame = (QImage.fromData(payload) if renderer_type is rendering.QtThumbnailRenderer
             else Image.new("RGB", (320, 150), "#35a275"))
    assert renderer.render_prepared(frame, size, cancelled=Event()) == renderer.render_prepared(frame, size)


@pytest.mark.parametrize("preview", [False, True])
def test_reader_loaders_stop_after_read_before_render(tmp_path, monkeypatch, preview):
    from tests.unit.test_reader_viewmodel import _viewmodel
    vm = _viewmodel(tmp_path)
    vm.open_path(tmp_path / "book.cbz")
    vm._page_cache.clear()
    cancelled, received, renders = Event(), [], []
    document = vm._document
    try:
        if preview:
            document.allows_random_preview = lambda _: True
            document.read_cached_pages = lambda _: {}
            vm.set_preview_target(4, (100, 142), load=False)
            loader = vm._topic_thumbnail_stream._preview_loader
            method = "read_page"
        else:
            vm._configure_topic_thumbnail_stream(document, (100, 142))
            loader = vm._topic_thumbnail_stream._loader
            method = "read_pages"
        original = getattr(document, method)
        def cancel_after_read(*args):
            result = original(*args)
            cancelled.set()
            return result
        monkeypatch.setattr(document, method, cancel_after_read)
        monkeypatch.setattr(vm._thumbnail_renderer, "render_encoded", lambda *a, **kw: renders.append(a))
        with pytest.raises(ThumbnailCancelled):
            loader((4,), received.append, cancelled)
        assert not renders and not received
    finally:
        vm.cancel()
