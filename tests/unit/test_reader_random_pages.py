from __future__ import annotations

from threading import Barrier, Event, get_ident

import pytest

from joyread.app.reader_page_pipeline import (
    EncodedPageFrameDecoder, ReaderPagePayload, ReaderPagePipeline, SessionReaderDocumentSource,
)
from joyread.app.reader_page_scheduler import ReaderPageScheduler
from joyread.core.services.cache_service import BoundedByteCache, NamespacedPageCache
from joyread.infrastructure.qt_task_service import TaskService


class Source:
    page_count = 10

    def __init__(self, *, random_pages=range(10)):
        self.random_pages = set(random_pages)
        self.reads = []
        self.threads = []
        self.closed = False
        self.before_read = lambda _index: None

    def allows_parallel_page_preparation(self, index):
        return index in self.random_pages

    def read_page(self, index):
        self.reads.append(index)
        self.threads.append(get_ident())
        self.before_read(index)
        return ReaderPagePayload(index, bytes([index]), (100, 150))

    def close(self):
        self.closed = True


def setup_pipeline(source, *, scheduler=None, normal=None, random=None, decoder=None):
    normal = normal or TaskService(1)
    random = random or TaskService(4)
    scheduler = scheduler or ReaderPageScheduler()
    cache = NamespacedPageCache(BoundedByteCache(512, sizer=lambda _value: 1))
    ready, failures = [], []
    pipeline = ReaderPagePipeline(
        normal, decoder or EncodedPageFrameDecoder(), cache,
        on_ready=ready.append, on_failed=lambda *args: failures.append(args),
        random_page_scheduler=scheduler, random_page_executor=random,
    )
    pipeline.set_source(source, generation=1)
    return pipeline, scheduler, normal, random, cache, ready, failures


def request(pipeline, visible, prefetch=(), *, width=100):
    pipeline.request(visible, prefetch, target_width=width, target_height=150,
                     device_pixel_ratio=1, generation=1, focus_indices=visible)


def cleanup(pipeline, scheduler, normal, random):
    pipeline.cancel()
    scheduler.close()
    random.shutdown()
    normal.shutdown()


def test_two_visible_pages_really_overlap_off_gui_and_return_on_gui(qtbot):
    source, both = Source(), Barrier(2)
    source.before_read = lambda _index: both.wait(timeout=3)
    pipeline, scheduler, normal, random, cache, ready, failures = setup_pipeline(source)
    callbacks = []
    original = pipeline._on_ready
    pipeline._on_ready = lambda page: (callbacks.append(get_ident()), original(page))
    try:
        request(pipeline, (0, 1))
        qtbot.waitUntil(lambda: len(ready) == 2 or bool(failures), timeout=4000)
        qtbot.waitUntil(lambda: scheduler.active_count == 0)
        assert not failures
        assert {page.page_index for page in ready} == {0, 1}
        assert len(set(source.threads)) == 2 and get_ident() not in source.threads
        assert callbacks == [get_ident(), get_ident()]
    finally:
        cleanup(pipeline, scheduler, normal, random)


def test_queued_prefetch_becomes_current_without_waiting_for_older_prefetch(qtbot):
    source, started, release = Source(), Event(), Event()
    def block(index):
        if index == 0:
            started.set()
            assert release.wait(3)
    source.before_read = block
    pipeline, scheduler, normal, random, cache, ready, failures = setup_pipeline(
        source, scheduler=ReaderPageScheduler(capacity=1),
    )
    try:
        request(pipeline, (), (0, 2, 3, 7, 8))
        assert started.wait(1)
        request(pipeline, (8,), (2, 3, 7))
        release.set()
        qtbot.waitUntil(lambda: len(ready) == 4)
        assert source.reads == [0, 8, 7, 3, 2]
        assert [page.page_index for page in ready] == [8, 7, 3, 2]
        assert cache.get(0) is None and not failures
    finally:
        release.set()
        cleanup(pipeline, scheduler, normal, random)


@pytest.mark.parametrize('action', ['cancel', 'resize', 'replace_source'])
def test_retired_decode_never_publishes_or_inserts_stale_frames(qtbot, action):
    source, entered, release = Source(), Event(), Event()
    class Decoder(EncodedPageFrameDecoder):
        def decode(self, payload, request):
            if request.target_width == 100:
                entered.set()
                assert release.wait(3)
            return super().decode(payload, request)
    pipeline, scheduler, normal, random, cache, ready, failures = setup_pipeline(source, decoder=Decoder())
    try:
        request(pipeline, (0,))
        assert entered.wait(1)
        if action == 'cancel':
            pipeline.cancel_pending_pages()
        elif action == 'resize':
            request(pipeline, (0,), width=200)
        else:
            pipeline.set_source(Source(), generation=2)
        release.set()
        qtbot.waitUntil(lambda: scheduler.active_count == 0)
        assert cache.get(0, 100, 150) is None
        assert len(ready) == (1 if action == 'resize' else 0)
        assert not failures
    finally:
        release.set()
        cleanup(pipeline, scheduler, normal, random)


def test_mixed_nested_sources_keep_ineligible_pages_on_serial_executor(qtbot):
    source = Source(random_pages={0, 2})
    pipeline, scheduler, normal, random, cache, ready, failures = setup_pipeline(source)
    normal_reads, random_reads = [], []
    normal_submit = normal.submit_stream
    random_submit = random.submit
    def record_serial(*args, **kwargs):
        normal_reads.append(args[0])
        return normal_submit(*args, **kwargs)
    def record_random(*args, **kwargs):
        random_reads.append(args[0])
        return random_submit(*args, **kwargs)
    normal.submit_stream, random.submit = record_serial, record_random
    try:
        request(pipeline, (0, 1), (2, 3))
        qtbot.waitUntil(lambda: len(ready) == 4)
        assert len(normal_reads) == 1 and len(random_reads) == 2
        assert sorted(source.reads) == [0, 1, 2, 3] and not failures
    finally:
        cleanup(pipeline, scheduler, normal, random)


def test_pdf_and_unknown_sessions_never_enter_random_scheduler():
    class PdfLike:
        page_count = 1
        requires_sequential_warmup = False
        def prepare_thumbnail_pages(self, *args):
            return []
    source = SessionReaderDocumentSource(PdfLike())
    assert source.allows_random_preview(0)
    assert not source.allows_parallel_page_preparation(0)


def test_one_failed_random_page_does_not_block_other_demand(qtbot):
    source = Source()
    def fail(index):
        if index == 0:
            raise ValueError('bad page')
    source.before_read = fail
    pipeline, scheduler, normal, random, cache, ready, failures = setup_pipeline(source)
    try:
        request(pipeline, (0, 1), (2,))
        qtbot.waitUntil(lambda: len(ready) == 2 and len(failures) == 1)
        qtbot.waitUntil(lambda: scheduler.active_count == 0)
        assert {page.page_index for page in ready} == {1, 2}
        assert failures[0][0] == 0
    finally:
        cleanup(pipeline, scheduler, normal, random)


def test_real_cbz_serial_and_parallel_frames_have_identical_pixels(qtbot, tmp_path):
    from hashlib import sha256
    from io import BytesIO
    from zipfile import ZipFile
    from PIL import Image
    from PySide6.QtGui import QImage
    from joyread.core.archive import ArchiveImageService
    from joyread.infrastructure.reader_image_decoder import QtPageFrameDecoder, qimage_frame_bytes

    archive_path = tmp_path / 'quality.cbz'
    with ZipFile(archive_path, 'w') as archive:
        for index, fmt in enumerate(('PNG', 'JPEG', 'WEBP')):
            image = Image.effect_noise((300, 450), 40).convert('RGB')
            image.paste((20, index * 80, 180), (40, 60, 170, 230))
            buffer = BytesIO()
            exif = Image.Exif()
            exif[274] = 6
            image.save(buffer, format=fmt, **({'exif': exif} if fmt == 'JPEG' else {}))
            archive.writestr(f'{index:03d}.{fmt.lower()}', buffer.getvalue())
    hashes = []
    for parallel in (False, True):
        session = ArchiveImageService().open(archive_path)
        source = SessionReaderDocumentSource(session)
        normal, random, scheduler = TaskService(1), TaskService(4), ReaderPageScheduler()
        ready, failures = [], []
        cache = NamespacedPageCache(BoundedByteCache(512 * 1024**2, sizer=qimage_frame_bytes))
        pipeline = ReaderPagePipeline(
            normal, QtPageFrameDecoder(), cache, on_ready=ready.append,
            on_failed=lambda *args: failures.append(args),
            random_page_scheduler=scheduler if parallel else None, random_page_executor=random,
        )
        pipeline.set_source(source, generation=1)
        try:
            pipeline.request((0, 1, 2), (), target_width=180, target_height=240,
                             device_pixel_ratio=2, generation=1)
            qtbot.waitUntil(lambda: len(ready) == 3 or bool(failures))
            assert not failures
            result = {}
            for page in ready:
                rgba = page.frame.convertToFormat(QImage.Format.Format_RGBA8888)
                result[page.page_index] = (page.rendered_dimensions, page.source_dimensions,
                                          page.frame.devicePixelRatio(), sha256(bytes(rgba.constBits())).hexdigest())
            hashes.append(result)
        finally:
            cleanup(pipeline, scheduler, normal, random)
            session.close()
    assert hashes[0] == hashes[1]
