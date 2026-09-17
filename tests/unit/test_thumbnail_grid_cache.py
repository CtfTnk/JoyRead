"""Decoded thumbnail retention and scrolling presentation regressions."""

from io import BytesIO

from PIL import Image
from PySide6.QtCore import QPoint, Qt

from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.book_detail import DetailThumbnailGrid
from joyread.ui.widgets.thumbnail_placeholder import thumbnail_placeholder


def image_bytes(size=(100, 142), color="red"):
    out = BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def grid(qtbot):
    widget = DetailThumbnailGrid()
    qtbot.addWidget(widget)
    widget.set_thumbnail_count(1000)
    return widget


def test_reverse_scroll_reuses_widget_and_decoded_pixels(qtbot):
    view = grid(qtbot)
    payload = image_bytes()
    view.set_interest((0, 1))
    view.set_thumbnail(0, payload)
    original = view._thumbnails[0]
    pixels = original._pixmap.cacheKey()
    view.set_interest((10, 11))
    assert original.isHidden() and 0 not in view._thumbnails
    view.set_interest((0, 1))
    view.set_thumbnail(0, payload)
    assert view._thumbnails[0] is original
    assert original._pixmap.cacheKey() == pixels
    clicked = []
    view.thumbnail_clicked.connect(clicked.append)
    qtbot.mouseClick(original, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
    assert clicked == [0]


def test_replacement_density_and_content_are_not_deduplicated(qtbot):
    view = grid(qtbot)
    view.set_interest((0,))
    view.set_thumbnail(0, image_bytes())
    widget = view._thumbnails[0]
    old = widget._pixmap.cacheKey()
    view.set_thumbnail(0, image_bytes((200, 284)))
    assert widget._pixmap.width() == 200 and widget._pixmap.cacheKey() != old
    view.set_thumbnail(0, image_bytes((200, 284), "blue"))
    assert widget._pixmap.toImage().pixelColor(10, 10).name() == "#0000ff"


def test_retention_obeys_byte_budget_and_lru(qtbot, monkeypatch):
    view = grid(qtbot)
    payload = image_bytes()
    view.set_interest((0,))
    view.set_thumbnail(0, payload)
    size = view._thumbnails[0].memory_bytes
    monkeypatch.setattr(Theme, "thumbnail_retained_byte_limit", 2 * size)
    for index in range(1, 5):
        view.set_interest((index,))
        view.set_thumbnail(index, payload)
    assert tuple(view._retained) == (2, 3)
    assert view._retained_bytes == 2 * size
    view.set_interest((2,))
    view.set_interest((5,))
    assert tuple(view._retained) == (4, 2)


def test_retention_obeys_widget_limit_and_clears_on_release_or_new_book(qtbot, monkeypatch):
    view = grid(qtbot)
    monkeypatch.setattr(Theme, "thumbnail_retained_widget_limit", 2)
    for index in range(10):
        view.set_interest((index,))
        view.set_thumbnail(index, image_bytes())
        assert len(view._retained) <= 2
    view.set_interest(())
    assert not view._thumbnails and not view._retained and view._retained_bytes == 0
    view.set_interest((0,))
    view.set_thumbnail(0, image_bytes())
    view.set_interest((1,))
    view.set_thumbnail_count(100, reset=True)
    assert not view._thumbnails and not view._retained


def test_placeholder_is_cached_at_native_density(qapp):
    for ratio in (1.0, 1.25, 2.0):
        pixmap = thumbnail_placeholder(ratio)
        assert pixmap.devicePixelRatioF() == ratio
        assert pixmap.cacheKey() == thumbnail_placeholder(ratio).cacheKey()
        assert pixmap.toImage().pixelColor(0, 0).alpha() == 0
    assert thumbnail_placeholder(2).width() == 2 * Theme.detail_thumbnail_width
