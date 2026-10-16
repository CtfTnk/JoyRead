"""Render the approved Pen panel chrome and protect its allocation across updates."""

from dataclasses import replace
from math import ceil, floor

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QColor, QFontDatabase, QFontMetrics
from PySide6.QtWidgets import QApplication, QWidget

from joyread.infrastructure.i18n import locale_service
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.book_detail import BookDetailPanel
from joyread.ui.widgets.floating_panel_header import FloatingPanelHeader
from joyread.ui.widgets.mode_switches import ListModeSwitchWidget
from joyread.ui.widgets.reader_controls import ReaderSwitch, ReaderTopicButtonGroup
from joyread.ui.widgets.reader_topic_panel import ReaderTopicMode, ReaderTopicPanel
from tests.support.in_memory_book_repository import InMemoryBookRepository


@pytest.fixture
def resources(qapp):
    resources = ResourceLoader()
    for path in resources.font_paths():
        QFontDatabase.addApplicationFont(str(path))
    qapp.setStyleSheet(resources.load_stylesheet())
    locale_service.init(resources.locale_dir(), None, "English")
    yield resources
    locale_service.load_language("English")


@pytest.mark.parametrize("kind", ("detail", "topic"))
def test_panel_header_stays_fixed_while_body_scrolls_and_updates(qtbot, resources, kind):
    book = InMemoryBookRepository().list_books()[0]
    if kind == "detail":
        panel = BookDetailPanel(resources)
        panel.set_book(book)
        panel.set_thumbnail_page_count(book.uuid, 80)
        panel.resize(902, 753)
        body, scroll = panel._scroll, panel._scroll
        expected_body = (8, 52, 886, 695)
    else:
        panel = ReaderTopicPanel(resources)
        panel.reset_thumbnails(80)
        body, scroll = panel._stack, panel._thumbnails_scroll
        expected_body = (8, 52, 769, 700)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitUntil(lambda: scroll.verticalScrollBar().maximum() > 0)

    assert body.geometry().getRect() == expected_body
    if kind == "detail":
        qtbot.waitUntil(lambda: scroll.viewport().width() == 876)
        assert scroll.widget().width() == 850
        assert scroll.widget().x() == 13
    header = panel._header
    close = header.close_button
    assert close.mapTo(panel, QPoint()).toTuple() == (panel.width() - 44, 12)
    assert header.title.mapTo(panel, QPoint()).x() == 16
    assert header.title.height() >= QFontMetrics(header.title.font()).height()
    before = panel.size(), header.geometry(), close.geometry()
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    for index in range(100):
        if kind == "detail":
            panel.set_book(replace(book, title="Long title " * 25 if index % 2 else book.title))
        else:
            panel.set_mode(ReaderTopicMode.BOOKMARKS)
            panel.set_mode(ReaderTopicMode.THUMBNAILS)
        QApplication.processEvents()
    assert (panel.size(), header.geometry(), close.geometry()) == before
    assert scroll.verticalScrollBar().value() == scroll.verticalScrollBar().maximum()

    panel.resize(400, 500)
    QApplication.processEvents()
    panel.resize(before[0])
    QApplication.processEvents()
    assert (panel.size(), header.geometry(), close.geometry()) == before
    assert body.geometry().getRect() == expected_body


def test_panel_titles_follow_mode_and_language_without_reconstruction(qtbot, resources):
    detail, topic = BookDetailPanel(resources), ReaderTopicPanel(resources)
    qtbot.addWidget(detail)
    qtbot.addWidget(topic)
    for language, details, titles, close in (
        ("English", "Details", ("Contents", "Bookmarks", "Thumbnails"), "Close"),
        ("Chinese", "详情", ("目录", "书签", "缩略图"), "关闭"),
        ("Japanese", "詳細", ("目次", "しおり", "サムネイル"), "閉じる"),
    ):
        locale_service.load_language(language)
        assert detail._header.title.full_text == details
        assert detail._header.close_button.accessibleName() == close
        for mode, title in zip(ReaderTopicMode, titles, strict=True):
            topic.set_mode(mode)
            assert topic._header.title.full_text == title
            assert topic._header.close_button.toolTip() == close


def _pixel(image, point):
    scale = image.devicePixelRatio()
    return image.pixelColor(int(point[0] * scale), int(point[1] * scale))


def _icon_ink(image):
    # A transparent normal button retains the SVG's antialias alpha. Hover
    # already composites those edge pixels into its fill, so compare the
    # visible dark pixels on a light surface rather than raw RGB channels.
    def visible_lightness(x, y):
        color = image.pixelColor(x, y)
        return (color.lightness() * color.alpha() + 255 * (255 - color.alpha())) / 255

    return {
        (x, y) for x in range(image.width()) for y in range(image.height())
        if visible_lightness(x, y) < 80
    }


def _opaque_ink(image):
    # Anti-aliased fringe pixels blend differently over the darker hover fill.
    return {
        (x, y) for x in range(image.width()) for y in range(image.height())
        if image.pixelColor(x, y).alpha() == 255 and image.pixelColor(x, y).lightness() < 4
    }


@pytest.mark.parametrize("profile", ("quiet", "contrast"))
def test_close_button_has_no_border_in_any_state_and_keeps_icon_fixed(qtbot, resources, profile):
    host = QWidget()
    host.setProperty("glassBackdrop", profile)
    host.setObjectName("CloseButtonTestHost")
    host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    host.setStyleSheet("QWidget#CloseButtonTestHost { background: white; }")
    host.resize(200, 80)
    header = FloatingPanelHeader(resources, "Details", body_inset=8, parent=host)
    header.setGeometry(0, 12, 200, 28)
    qtbot.addWidget(host)
    host.show()
    button = header.close_button
    qtbot.mouseMove(host, QPoint(2, 70))
    normal = button.grab().toImage()
    allocation = button.geometry(), button.contentsRect()
    qtbot.mouseMove(button)
    hover = button.grab().toImage()
    qtbot.mousePress(button, Qt.MouseButton.LeftButton)
    pressed = button.grab().toImage()
    try:
        palette = Theme.glass_palettes[profile]
        assert QColor(*palette.close_hover).lightness() < 235
        assert QColor(*palette.close_pressed).lightness() < QColor(*palette.close_hover).lightness()
        for image, fill in ((normal, QColor(0, 0, 0, 0)), (hover, QColor(*palette.close_hover)), (pressed, QColor(*palette.close_pressed))):
            for point in ((14, 0), (0, 14), (27, 14), (14, 27)):
                assert max(abs(a - b) for a, b in zip(_pixel(image, point).getRgb(), fill.getRgb(), strict=True)) <= 1
            assert max(abs(a - b) for a, b in zip(_pixel(image, (4, 14)).getRgb(), fill.getRgb(), strict=True)) <= 1
        assert _icon_ink(normal)
        assert _icon_ink(normal) == _icon_ink(hover) == _icon_ink(pressed)
        assert (button.geometry(), button.contentsRect()) == allocation
    finally:
        qtbot.mouseRelease(button, Qt.MouseButton.LeftButton)


def test_detail_actions_use_translucent_surfaces_without_fading_content_or_changing_commands(qtbot, resources):
    panel = BookDetailPanel(resources)
    qtbot.addWidget(panel)
    book = InMemoryBookRepository().list_books()[0]
    panel.set_book(book)
    host = QWidget()
    qtbot.addWidget(host)
    host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    host.resize(300, 100)
    controls = panel._option_button.parentWidget()
    controls.setParent(host)
    controls.setGeometry(0, 0, 300, 48)
    # Measure the surface independently of the existing read-button shadow.
    panel._read_button.setGraphicsEffect(None)
    calls = []
    panel.read_requested.connect(lambda uuid: calls.append(("read", uuid)))
    panel.favourite_requested.connect(lambda uuid: calls.append(("favourite", uuid)))
    panel.menu_requested.connect(lambda uuid, _point: calls.append(("more", uuid)))
    host.show()
    controls.show()
    QApplication.processEvents()
    buttons = (panel._read_button, panel._favourite_button, panel._option_button)
    for button in buttons:
        qtbot.mouseMove(host, QPoint(2, 90))
        # The offscreen plugin can retain a QFrame's hover state when a move
        # is delivered directly to its ancestor instead of through hit-testing.
        QApplication.sendEvent(button, QEvent(QEvent.Type.Leave))
        QApplication.processEvents()
        allocation = button.geometry(), button.contentsRect()
        local_point = QPoint(button.width() // 2, 3)
        point = button.mapTo(host, local_point)
        color = _pixel(host.grab().toImage(), point.toTuple())
        assert color == QColor(*Theme.color_detail_button_background_rgba)
        normal = button.grab().toImage()
        qtbot.mouseMove(button)
        QApplication.processEvents()
        hover = button.grab().toImage()
        color = _pixel(host.grab().toImage(), point.toTuple())
        expected = QColor(*Theme.color_detail_button_hover_rgba)
        assert max(abs(a - b) for a, b in zip(color.getRgb(), expected.getRgb(), strict=True)) <= 1
        assert _opaque_ink(normal)
        assert _opaque_ink(normal) == _opaque_ink(hover)
        assert (button.geometry(), button.contentsRect()) == allocation
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert calls == [(command, book.uuid) for command in ("read", "favourite", "more")]


@pytest.mark.parametrize("kind", ("shelf", "reader", "topic"))
def test_selected_switch_cell_has_a_complete_independent_border(qtbot, resources, kind):
    if kind == "shelf":
        switch = ListModeSwitchWidget(resources)
        button = switch._buttons[switch.value]
    elif kind == "reader":
        switch = ReaderSwitch(resources, (("First", "icon_read-from-left.svg", "first"), ("Second", "icon_read-from-right.svg", "second")))
        button = switch._buttons[switch.value]
    else:
        switch = ReaderTopicButtonGroup(resources)
        switch.set_active_mode(ReaderTopicMode.THUMBNAILS)
        button = switch.thumbnail_button
    qtbot.addWidget(switch)
    switch.show()
    qtbot.mouseMove(switch, QPoint(1, 1))
    QApplication.processEvents()
    image = switch.grab().toImage()
    assert button.y() == 4
    assert button.size().toTuple() == (28, 28)
    edge = Theme.color_topic_combo_active_edge if kind == "topic" else Theme.color_control_selected_edge
    scale = image.devicePixelRatio()
    expected = QColor(edge)
    for x, y, horizontal in ((14, 0, False), (0, 14, True), (27, 14, True), (14, 27, False)):
        px, py = (button.x() + x) * scale, (button.y() + y) * scale
        start = px if horizontal else py
        # A 1px logical stroke can straddle physical pixels at fractional
        # scale. Inspect its whole band; at least one pixel has >=50% edge
        # coverage, while a missing edge leaves the lighter track/fill.
        pixels = [
            image.pixelColor(value, floor(py)) if horizontal
            else image.pixelColor(floor(px), value)
            for value in range(floor(start), ceil(start + scale))
        ]
        darkest = min(pixels, key=lambda color: color.lightness())
        for actual, target in zip(darkest.getRgb()[:3], expected.getRgb()[:3], strict=True):
            assert target - 1 <= actual <= target + (255 - target) / 2 + 1
    if kind == "topic":
        # The combo has an opaque white track, independent of gray switches.
        assert _pixel(image, (50, 2)) == QColor("white")
