from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QEnterEvent
from PySide6.QtWidgets import QApplication, QWidget

from joyread.app.event_hook import EventHook
from joyread.app.thumbnail_stream import ThumbnailStreamItem
from joyread.core.reader import ReaderDirection
from joyread.ui.widgets.reader_controls import ReaderProgressSlider
from joyread.ui.widgets.reader_preview import ReaderPreviewController
from tests.unit.test_reader_viewmodel import _viewmodel, _png_bytes
from tests.unit.test_thumbnail_stream import _ManualTaskService, _controller
from tests.unit.test_archive_cache_policy import _plan


def test_preview_and_topic_share_results_and_release_independently():
    tasks = _ManualTaskService()
    stream = _controller(tasks)
    ready, reads = [], []
    def loader(indices, emit):
        reads.extend(indices)
        for index in indices:
            emit(ThumbnailStreamItem(index, b"image"))
    stream.set_source("book", 50, (100, 142), loader)
    stream.thumbnail_ready.connect(lambda i, _: ready.append(i))
    stream.set_interest((2,), (3,))
    stream.set_preview(10, loader)
    stream.set_preview(20, loader)
    assert len(tasks.tasks) == 1
    tasks.run(0)
    assert stream.active_indices == (20,)
    stream.set_interest((), ())
    tasks.run(1)
    assert reads == [2, 20]
    stream.set_preview(None)
    stream.set_interest((20,), ())
    assert len(tasks.tasks) == 2  # Preview -> Topic cache hit.
    stream.set_preview(2, loader, enabled=False)
    assert ready[-1] == 2  # Topic -> Preview cache hit, even before debounce.


def test_preview_debounce_does_not_block_topic_or_start_source_work():
    tasks = _ManualTaskService()
    stream = _controller(tasks)
    loader = lambda indices, emit: [emit(ThumbnailStreamItem(i, b"image")) for i in indices]
    stream.set_source("book", 50, (100, 142), loader)
    stream.set_preview(20, loader, enabled=False)
    assert not tasks.tasks
    stream.set_interest((2,), ())
    assert stream.active_indices == (2,)
    stream.set_preview(None)
    tasks.run(0)
    assert len(tasks.tasks) == 1


@pytest.mark.parametrize("random_access", [False, True])
def test_preview_cache_only_policy_and_topic_reuse(tmp_path, random_access):
    vm = _viewmodel(tmp_path, prefetch_before=0, prefetch_after=0)
    vm.open_path(tmp_path / "book.cbz")
    vm._page_cache.clear()
    document = vm._document
    document.allows_random_preview = lambda index: random_access
    document.read_cached_pages = lambda indices: {}
    reads = []
    original = document.read_page
    document.read_page = lambda index: (reads.append(index), original(index))[1]
    states = []
    vm.preview_changed.connect(lambda *args: states.append(args))
    vm.set_preview_target(4, (100, 142), load=False)
    assert not reads and states[-1][1] == "loading"
    vm.set_preview_target(4, (100, 142), load=True)
    assert reads == ([4] if random_access else [])
    assert states[-1][1] == ("ready" if random_access else "loading")
    if not random_access:
        # Normal reading filled its cache; preview can now render without a read.
        from joyread.app.reader_page_pipeline import ReaderPagePayload
        document.read_cached_pages = lambda indices: {4: ReaderPagePayload(4, _png_bytes(), (600, 900))}
        vm.retry_preview()
        assert states[-1][1] == "ready" and not reads
    ready = []
    vm.topic_thumbnail_ready.connect(lambda index, data: ready.append(index))
    vm.release_preview()
    vm.set_topic_thumbnail_interest((4,), (), (100, 142))
    assert ready == [4]
    vm.cancel()


def test_preview_waits_for_foreground_and_reports_failure_once(tmp_path):
    vm = _viewmodel(tmp_path, prefetch_before=0, prefetch_after=0)
    vm.open_path(tmp_path / "book.cbz")
    vm._page_cache.clear()
    vm._document.allows_random_preview = lambda _: True
    calls = []
    def fail(index):
        calls.append(index)
        raise ValueError("bad image")
    vm._document.read_page = fail
    vm.loading_page_index = 1
    vm.set_preview_target(4, (100, 142), load=True)
    assert not calls
    vm.loading_page_index = None
    vm.retry_preview()
    assert calls == [4] and vm._preview_status == "unavailable"
    vm.retry_preview()
    assert calls == [4]
    vm.cancel()


@pytest.mark.parametrize("pool_bytes", [0, 64 * 1024 * 1024])
def test_solid_cache_read_never_extracts_even_when_warmup_disallowed(tmp_path, pool_bytes):
    session, pool = _plan(tmp_path, pool_bytes=pool_bytes)
    def forbidden(*args):
        pytest.fail("cache-only preview attempted extraction")
    session._read_entries = forbidden
    assert not session.allows_random_preview(0)
    assert session.read_cached_pages((0, 1)) == [None, None]
    if pool_bytes:
        session._cache_lease.put_many({session._cache_page_key(0): _png_bytes()})
        assert session.read_cached_pages((0, 1))[0] is not None
        assert session.read_cached_pages((0, 1))[1] is None
    session.close()


class PreviewVM:
    page_count = 100
    current_index = 5
    settings = SimpleNamespace(direction=ReaderDirection.LEFT_TO_RIGHT)
    def __init__(self):
        self.preview_changed = EventHook()
        self.requests = []
    def set_preview_target(self, index, size, *, load=False):
        self.requests.append((index, size, load))
        self.preview_changed.emit(index, "loading", None)
    def retry_preview(self):
        pass
    def release_preview(self):
        pass


@pytest.fixture
def preview(qtbot):
    shell = QWidget()
    shell.resize(600, 400)
    slider = ReaderProgressSlider(shell)
    slider.setGeometry(20, 340, 560, 20)
    slider.setRange(0, 99)
    slider.set_reading_direction(ReaderDirection.LEFT_TO_RIGHT)
    vm = PreviewVM()
    controller = ReaderPreviewController(shell, slider, vm)
    qtbot.addWidget(shell)
    shell.show()
    yield controller, vm
    controller.close()
    shell.close()


def test_preview_hover_fade_reentry_and_wrap_content(preview, qtbot):
    controller, vm = preview
    point = QPointF(10, 10)
    QApplication.sendEvent(controller.slider, QEnterEvent(point, point, QPointF(controller.slider.mapToGlobal(point.toPoint()))))
    assert controller.hover.isActive() and not controller.bubble.isVisible()
    qtbot.waitUntil(controller.bubble.isVisible, timeout=500)
    compact = controller.bubble.size()
    QApplication.sendEvent(controller.slider, QEvent(QEvent.Type.Leave))
    qtbot.wait(60)
    assert 0 < controller.bubble.opacity < 0.8
    QApplication.sendEvent(controller.slider, QEnterEvent(point, point, QPointF(controller.slider.mapToGlobal(point.toPoint()))))
    assert controller.bubble.opacity == 0.8 and not controller.hover.isActive()
    vm.preview_changed.emit(controller.target, "ready", _png_bytes())
    assert controller.bubble.height() > compact.height()
    QApplication.sendEvent(controller.slider, QEvent(QEvent.Type.Leave))
    qtbot.waitUntil(lambda: not controller.bubble.isVisible(), timeout=500)


@pytest.mark.parametrize("cancel", [None, "escape", "right", "deactivate", "outside"])
def test_drag_commits_once_or_cancels_without_seek(preview, qtbot, cancel):
    controller, vm = preview
    commits = []
    controller.seek_requested.connect(commits.append)
    slider = controller.slider
    qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=QPoint(200, 10))
    assert controller.dragging and controller.bubble.isVisible()
    qtbot.mouseMove(slider, QPoint(350, 10))
    target = slider.value()
    assert vm.current_index == 5
    if cancel == "escape":
        qtbot.keyClick(slider, Qt.Key.Key_Escape)
    elif cancel == "right":
        qtbot.mouseClick(slider, Qt.MouseButton.RightButton)
    elif cancel == "deactivate":
        QApplication.sendEvent(controller.shell, QEvent(QEvent.Type.WindowDeactivate))
    point = QPoint(900, 10) if cancel == "outside" else QPoint(350, 10)
    qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=point)
    assert commits == ([] if cancel else [target])
    assert not controller.dragging and not controller.bubble.isVisible()
    assert not controller.hover.isActive()
    assert QWidget.mouseGrabber() is not slider
    if cancel:
        assert slider.value() == 5


@pytest.mark.parametrize("direction", list(ReaderDirection))
def test_preview_index_mapping_and_edge_clamp(preview, direction):
    controller, vm = preview
    controller.slider.set_reading_direction(direction)
    rtl = direction == ReaderDirection.RIGHT_TO_LEFT
    assert controller._index_at(-100) == (99 if rtl else 0)
    assert controller._index_at(10000) == (0 if rtl else 99)
    for x in (0, controller.slider.width()):
        controller._position = x
        controller._activate()
        bubble = controller.bubble
        assert bubble.x() >= 6 and bubble.geometry().right() < controller.shell.width() - 6
        assert bubble.geometry().bottom() < controller.slider.y()


def test_reader_state_refresh_preserves_drag_target(qtbot):
    from joyread.infrastructure.resources.resource_loader import ResourceLoader
    from joyread.ui.widgets.reader_controls import ReaderFooter
    footer = ReaderFooter(ResourceLoader())
    qtbot.addWidget(footer)
    footer.set_page_state(2, 100, ReaderDirection.LEFT_TO_RIGHT)
    footer.slider.setSliderDown(True)
    footer.slider.setValue(70)
    footer.set_page_state(3, 100, ReaderDirection.LEFT_TO_RIGHT)
    assert footer.slider.value() == 70


@pytest.mark.parametrize("ratio", [1, 1.25, 2])
@pytest.mark.parametrize("language", ["English", "Chinese", "Japanese"])
def test_preview_localized_content_and_physical_size(preview, monkeypatch, ratio, language):
    from joyread.infrastructure.i18n import locale_service
    from math import ceil
    controller, vm = preview
    monkeypatch.setattr(controller.slider, "devicePixelRatioF", lambda: ratio)
    original = locale_service.active_language_code()
    try:
        locale_service.load_language(language)
        controller._position = 250
        controller._activate()
        assert vm.requests[-1][1] == (ceil(100 * ratio), ceil(142 * ratio))
        metrics = controller.bubble.fontMetrics()
        assert controller.bubble.width() >= metrics.horizontalAdvance(controller.bubble._text()) + 16
        vm.preview_changed.emit(controller.target, "ready", _png_bytes())
        assert controller.bubble.width() == 116
        assert controller.bubble.height() >= 186
    finally:
        locale_service.load_language(original)


def test_topic_demand_is_not_downgraded_to_cache_only():
    tasks = _ManualTaskService()
    stream = _controller(tasks)
    reads = []
    def topic(indices, emit):
        reads.extend(indices)
        for index in indices:
            emit(ThumbnailStreamItem(index, b"image"))
    stream.set_source("book", 10, (100, 142), topic)
    stream.set_preview(3, lambda indices, emit: None)
    stream.set_interest((3,), ())
    tasks.run(0)  # The cache-only request already in flight finishes empty.
    tasks.run(1)
    assert reads == [3]


def test_source_capability_is_per_page_in_mixed_archive(tmp_path):
    from joyread.core.archive.records import ArchiveSource
    sources = [ArchiveSource("nested-solid", ".7z", requires_sequential_warmup=True),
               ArchiveSource("nested-zip", ".zip", requires_sequential_warmup=False)]
    session, _ = _plan(tmp_path, pages=2, sources=sources)
    assert not session.allows_random_preview(0)
    assert session.allows_random_preview(1)
    session.close()


def test_real_reader_preview_loads_and_topic_reuses_without_seeking(qtbot, tmp_path, monkeypatch):
    from zipfile import ZipFile
    from joyread.app.app_context import create_app_context
    from joyread.ui.views.reader_window import ReaderWindow
    source = tmp_path / "preview.cbz"
    with ZipFile(source, "w") as archive:
        for index in range(30):
            archive.writestr(f"{index:03}.png", _png_bytes())
    context = create_app_context()
    window = ReaderWindow(context, source)
    qtbot.addWidget(window)
    try:
        window.show()
        shell = window.shell
        vm = shell.viewmodel
        qtbot.waitUntil(lambda: vm.page_count == 30 and vm.layout_result is not None, timeout=5000)
        initial = vm.current_index
        shell.footer.show()
        preview = shell.preview
        preview._position = shell.footer.slider.width() / 2
        preview._activate()
        qtbot.waitUntil(lambda: preview.bubble.status == "ready", timeout=5000)
        target, size = preview.target, preview._size
        assert vm.current_index == initial
        assert not preview.bubble.pixmap.isNull()
        preview.close()
        def forbidden(*args):
            pytest.fail("Topic regenerated an existing preview thumbnail")
        monkeypatch.setattr(vm._thumbnail_renderer, "render_encoded", forbidden)
        monkeypatch.setattr(vm._thumbnail_renderer, "render_prepared", forbidden)
        ready = []
        vm.topic_thumbnail_ready.connect(lambda index, data: ready.append(index))
        vm.set_topic_thumbnail_interest((target,), (), size)
        assert ready == [target]
    finally:
        window.close()
        context.close()
