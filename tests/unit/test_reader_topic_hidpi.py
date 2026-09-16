from io import BytesIO

from PIL import Image
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.widgets.reader_topic_panel import ReaderTopicPanel
from tests.unit.test_reader_viewmodel import _viewmodel


def test_topic_stream_reconfigures_physical_size_without_reopening_reader(tmp_path):
    vm = _viewmodel(tmp_path)
    vm.open_path(tmp_path/'book.cbz')
    document, position = vm._document, vm.current_display_indices
    received = []
    vm.topic_thumbnail_ready.connect(lambda index, data: received.append((index, data)))
    for size in ((100,142), (200,284), (125,178), (100,142)):
        received.clear()
        vm.set_topic_thumbnail_interest((0,), (), size)
        assert [index for index, _ in received] == [0]
        with Image.open(BytesIO(received[0][1])) as image:
            assert image.size == size
        assert vm._document is document and vm.current_display_indices == position
    vm.cancel()


def test_topic_panel_dpr_refresh_keeps_scroll_and_loaded_widgets(qtbot, monkeypatch):
    panel = ReaderTopicPanel(ResourceLoader());qtbot.addWidget(panel)
    monkeypatch.setattr(panel, 'devicePixelRatioF', lambda: 1.0)
    received = []
    panel.thumbnail_interest_changed.connect(lambda *args: received.append(args))
    panel.reset_thumbnails(100);panel.show()
    qtbot.waitUntil(lambda: bool(received))
    bar = panel._thumbnails_scroll.verticalScrollBar()
    bar.setValue(bar.maximum())
    qtbot.waitUntil(lambda: received[-1][0][0] > 0)
    visible, prefetch, size = received[-1]
    assert size == (100,142)
    image = BytesIO();Image.new('RGB',(100,142),'white').save(image,format='PNG')
    panel.set_thumbnail(visible[0], image.getvalue())
    widget = panel._thumbnail_grid._thumbnails[visible[0]]
    geometry, scroll = widget.geometry(), bar.value()
    monkeypatch.setattr(panel, 'devicePixelRatioF', lambda: 2.0)
    QApplication.sendEvent(panel,QEvent(QEvent.Type.DevicePixelRatioChange))
    qtbot.waitUntil(lambda: received[-1][2] == (200,284))
    assert received[-1][:2] == (visible,prefetch)
    assert panel._thumbnail_grid._thumbnails[visible[0]] is widget
    assert widget.geometry() == geometry and bar.value() == scroll
    panel.hide();qtbot.wait(1);received.clear()
    monkeypatch.setattr(panel, 'devicePixelRatioF', lambda: 1.25)
    QApplication.sendEvent(panel,QEvent(QEvent.Type.DevicePixelRatioChange))
    qtbot.wait(1)
    assert received == []
    panel.show()
    qtbot.waitUntil(lambda: bool(received))
    assert received[-1][2] == (125,178) and bar.value() == scroll
