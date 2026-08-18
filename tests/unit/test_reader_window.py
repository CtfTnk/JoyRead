from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
import shiboken6
from PIL import Image
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, qInstallMessageHandler
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea

from joyread.app.app_context import create_app_context
from joyread.app.windows.manager import ApplicationWindowManager
from joyread.core.models.book import Book
from joyread.core.reader import ReaderDirection, ReaderSettings
from joyread.infrastructure.i18n import locale_service
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.reader_viewmodel import (
    ReaderBookmarkItem,
    ReaderContentsItem,
)
from joyread.ui.views.main_window import MainWindow
from joyread.novel.ui.novel_reader_shell import NovelReaderShellWidget
from joyread.ui.views.reader_shell import ReaderShellWidget
from joyread.ui.views.reader_window import ReaderWindow
from joyread.ui.widgets.elided_label import ElidedLabel
from joyread.ui.widgets.reader_controls import ReaderProgressSlider, _bottom_rounded_path, _top_rounded_path
from joyread.ui.widgets.reader_settings_panel import ReaderSettingsPanel
from joyread.ui.widgets.reader_topic_panel import ReaderTopicMode, ReaderTopicPanel
from joyread.ui.widgets.window_chrome import StoplightControlsWidget, TitleControlGroup


def test_bookmark_rename_dialog_uses_active_locale() -> None:
    calls: list[dict[str, object]] = []

    class FakeDialog:
        def show_input(self, *args, **kwargs) -> None:
            calls.append({"args": args, "kwargs": kwargs})

    receiver = SimpleNamespace(dialog_overlay=FakeDialog(), viewmodel=SimpleNamespace(rename_bookmark=lambda *_args: None))

    try:
        locale_service.load_language("Chinese")
        ReaderShellWidget._show_rename_bookmark_dialog(receiver, "bookmark-1", "旧书签")
        NovelReaderShellWidget._show_rename_bookmark_dialog(receiver, "bookmark-2", "旧书签")

        assert [call["args"][:2] for call in calls] == [
            ("重命名书签", "书签名称"),
            ("重命名书签", "书签名称"),
        ]
        assert [call["kwargs"]["confirm_text"] for call in calls] == ["重命名", "重命名"]
        assert [call["kwargs"]["cancel_text"] for call in calls] == ["取消", "取消"]
        assert calls[0]["kwargs"]["validator"]("   ") == "书签名称不能为空。"
    finally:
        locale_service.load_language("English")


def test_reader_window_matches_figma_shell_geometry(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "reader.cbz"
    image = tmp_path / "001.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")
    context = create_app_context()
    window = ReaderWindow(context, source)
    qtbot.addWidget(window)
    window.show()
    window.resize(Theme.reader_width, Theme.reader_height)

    assert window.width() == Theme.reader_width
    assert window.height() == Theme.reader_height
    assert window.minimumWidth() == Theme.reader_min_width
    assert window.minimumHeight() == Theme.reader_min_height
    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert window.header.height() == Theme.reader_banner_height
    assert window.footer.height() == Theme.reader_footer_height
    assert window.left_arrow.size().width() == Theme.reader_side_button_width
    assert window.left_arrow.size().height() == Theme.reader_side_button_height
    assert not window.header.back_button.isVisible()
    assert not window.header.back_spacer.isVisible()
    assert window.shell.mask().isEmpty()
    direction_buttons = window.footer.direction_switch._buttons
    assert list(direction_buttons) == [
        ReaderDirection.RIGHT_TO_LEFT,
        ReaderDirection.LEFT_TO_RIGHT,
        ReaderDirection.TOP_TO_BOTTOM,
    ]
    assert direction_buttons[ReaderDirection.RIGHT_TO_LEFT].toolTip() == "Right-to-left"
    assert direction_buttons[ReaderDirection.RIGHT_TO_LEFT].property("iconName") == "icon_read-from-left.svg"
    assert direction_buttons[ReaderDirection.LEFT_TO_RIGHT].toolTip() == "Left-to-right"
    assert direction_buttons[ReaderDirection.LEFT_TO_RIGHT].property("iconName") == "icon_read-from-right.svg"
    assert direction_buttons[ReaderDirection.TOP_TO_BOTTOM].toolTip() == "Top-to-down"
    assert direction_buttons[ReaderDirection.TOP_TO_BOTTOM].property("iconName") == "icon_read-from-top.svg"

    window.close()
    context.close()


def test_reader_window_inspection_mode_shows_non_macos_title_control_group(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "reader.cbz"
    image = tmp_path / "001.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")
    context = create_app_context()
    context.settings_viewmodel.set_inspect_non_native_title_control(True)
    window = ReaderWindow(context, source)
    qtbot.addWidget(window)
    window.show()

    title_controls = window.header.findChild(TitleControlGroup, "TitleControlGroup")
    stoplights = window.header.findChild(StoplightControlsWidget)

    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert title_controls is not None
    assert stoplights is not None
    assert title_controls.isVisible()
    assert stoplights.isHidden()

    window.close()
    context.close()


def test_reader_header_switch_icons_survive_hover_and_checked_modes(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "reader.cbz"
    image = tmp_path / "001.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")
    context = create_app_context()
    window = ReaderWindow(context, source)
    qtbot.addWidget(window)

    for button in (window.header.detail_button, window.header.bookmark_button, window.header.thumbnail_button):
        icon = button.icon()
        for mode in (QIcon.Mode.Normal, QIcon.Mode.Active, QIcon.Mode.Selected, QIcon.Mode.Disabled):
            assert not icon.pixmap(Theme.icon_size, Theme.icon_size, mode, QIcon.State.Off).isNull()
            assert not icon.pixmap(Theme.icon_size, Theme.icon_size, mode, QIcon.State.On).isNull()

    window.close()
    context.close()


def test_reader_topic_button_group_disables_unavailable_modes_for_direct_files(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "reader.cbz"
    image = tmp_path / "001.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")
    context = create_app_context()
    window = ReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    assert not window.header.detail_button.isEnabled()
    assert not window.header.bookmark_button.isEnabled()
    assert window.header.thumbnail_button.isEnabled()
    assert not window.header.detail_button.isCheckable()
    assert not window.header.bookmark_button.isCheckable()
    assert not window.header.thumbnail_button.isCheckable()
    assert window.header.topic_button_group.active_mode is None

    window.shell._show_topic_panel(ReaderTopicMode.BOOKMARKS)

    assert window.topic_panel.isHidden()
    assert window.header.topic_button_group.active_mode is None

    window.shell._show_topic_panel(ReaderTopicMode.THUMBNAILS)

    assert window.topic_panel.isVisible()
    assert window.header.topic_button_group.active_mode == ReaderTopicMode.THUMBNAILS
    assert window.header.thumbnail_button.property("topicActive") is True
    assert window.header.detail_button.property("topicActive") is False
    assert window.header.bookmark_button.property("topicActive") is False

    window.shell._hide_topic_panel()

    assert window.header.topic_button_group.active_mode is None

    window.close()
    context.close()


def test_reader_topic_contents_enables_for_archive_folders(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "reader-contents.cbz"
    image = tmp_path / "001.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "000.png")
        archive.write(image, "Chapter1/001.png")
    context = create_app_context()
    window = ReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    qtbot.waitUntil(lambda: window.shell.viewmodel.can_use_contents, timeout=5000)

    assert window.header.detail_button.isEnabled()
    assert [(item.label, item.page_index, item.depth) for item in window.shell.viewmodel.contents] == [
        ("Chapter1", 1, 0),
    ]

    window.shell._show_topic_panel(ReaderTopicMode.CONTENTS)
    assert window.topic_panel.isVisible()
    assert window.topic_panel.mode == ReaderTopicMode.CONTENTS

    window.close()
    context.close()


def test_reader_topic_panel_opens_centered_and_closes_with_escape(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "reader.cbz"
    image = tmp_path / "001.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")
    context = create_app_context()
    window = ReaderWindow(context, source, book=_reader_book(source))
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    window.shell._show_topic_panel(ReaderTopicMode.THUMBNAILS)

    assert window.topic_panel.isVisible()
    assert window.topic_panel.mode == ReaderTopicMode.THUMBNAILS
    assert window.header.topic_button_group.active_mode == ReaderTopicMode.THUMBNAILS
    assert window.topic_panel.geometry().getRect() == (
        (Theme.reader_width - Theme.reader_topic_panel_width) // 2,
        (Theme.reader_height - Theme.reader_topic_panel_height) // 2,
        Theme.reader_topic_panel_width,
        Theme.reader_topic_panel_height,
    )

    qtbot.keyClick(window, Qt.Key.Key_Escape)

    assert window.topic_panel.isHidden()
    assert window.header.topic_button_group.active_mode is None

    window.close()
    context.close()


def test_reader_topic_panel_closes_on_canvas_click(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "reader.cbz"
    image = tmp_path / "001.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")
    context = create_app_context()
    window = ReaderWindow(context, source, book=_reader_book(source))
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    window.shell._show_topic_panel(ReaderTopicMode.THUMBNAILS)
    qtbot.mouseClick(window.canvas, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))

    assert window.topic_panel.isHidden()
    assert window.header.topic_button_group.active_mode is None

    window.close()
    context.close()


def test_reader_topic_panel_keeps_one_visible_mode_and_independent_scrollbars(qtbot) -> None:
    context = create_app_context()
    panel = ReaderTopicPanel(context.resources)
    qtbot.addWidget(panel)
    panel.resize(Theme.reader_topic_panel_min_width, Theme.reader_topic_panel_min_height)
    panel.set_bookmarks(
        tuple(ReaderBookmarkItem(f"bookmark-{index}", f"Bookmark {index}", index) for index in range(60))
    )
    panel.reset_thumbnails(80)
    panel.show()
    panel.set_mode(ReaderTopicMode.THUMBNAILS)

    thumbnail_scroll = panel.findChild(QScrollArea, "ReaderTopicThumbnailsScrollArea")
    bookmark_scroll = panel.findChild(QScrollArea, "ReaderTopicBookmarksScrollArea")
    assert thumbnail_scroll is not None
    assert bookmark_scroll is not None
    assert thumbnail_scroll is not bookmark_scroll

    qtbot.waitUntil(lambda: thumbnail_scroll.verticalScrollBar().maximum() > 0, timeout=1000)
    thumbnail_scroll.verticalScrollBar().setValue(thumbnail_scroll.verticalScrollBar().maximum())
    thumbnail_value = thumbnail_scroll.verticalScrollBar().value()

    panel.set_mode(ReaderTopicMode.BOOKMARKS)

    assert panel.mode == ReaderTopicMode.BOOKMARKS
    assert panel._stack.currentWidget() is bookmark_scroll
    assert bookmark_scroll.verticalScrollBar().value() == 0

    qtbot.waitUntil(lambda: bookmark_scroll.verticalScrollBar().maximum() > 0, timeout=1000)
    bookmark_scroll.verticalScrollBar().setValue(bookmark_scroll.verticalScrollBar().maximum())
    panel.set_mode(ReaderTopicMode.THUMBNAILS)

    assert panel._stack.currentWidget() is thumbnail_scroll
    assert thumbnail_scroll.verticalScrollBar().value() == thumbnail_value

    context.close()


def test_reader_topic_contents_renders_one_based_page_number(qtbot) -> None:
    locale_service.load_language("English")
    context = create_app_context()
    panel = ReaderTopicPanel(context.resources)
    qtbot.addWidget(panel)
    panel.set_contents((ReaderContentsItem("Nested archive", 18, 0),))
    panel.set_mode(ReaderTopicMode.CONTENTS)
    panel.show()
    qtbot.wait(0)

    index_labels = [
        label
        for label in panel.findChildren(QLabel)
        if label.property("class") == "ReaderTopicItemIndex"
    ]

    assert [label.text() for label in index_labels] == ["page 19"]

    context.close()


def test_reader_topic_bookmark_row_elides_long_names_without_expanding_panel(qtbot) -> None:
    context = create_app_context()
    panel = ReaderTopicPanel(context.resources)
    qtbot.addWidget(panel)
    panel.resize(420, Theme.reader_topic_panel_min_height)
    long_name = "Manatsu no Inaka de Asedaku ni Natte Musaboriau Oyako _ Mother and Son Sweating " * 4
    panel.set_bookmarks((ReaderBookmarkItem("bookmark-long", long_name, 0),))
    panel.set_mode(ReaderTopicMode.BOOKMARKS)
    panel.show()
    qtbot.wait(0)

    bookmark_scroll = panel.findChild(QScrollArea, "ReaderTopicBookmarksScrollArea")
    assert bookmark_scroll is not None
    rows = [row for row in panel.findChildren(QFrame) if row.property("class") == "ReaderTopicItem"]
    labels = [label for label in panel.findChildren(ElidedLabel) if label.full_text == long_name]

    assert rows
    assert labels
    assert rows[0].width() <= bookmark_scroll.viewport().width()
    assert labels[0].text() != long_name
    assert labels[0].toolTip() == long_name

    context.close()


def test_reader_topic_panel_updates_interest_when_virtual_grid_scrolls(qtbot) -> None:
    context = create_app_context()
    panel = ReaderTopicPanel(context.resources)
    qtbot.addWidget(panel)
    interests: list[tuple[tuple[int, ...], tuple[int, ...], tuple[int, int]]] = []
    panel.thumbnail_interest_changed.connect(
        lambda visible, prefetch, size: interests.append((visible, prefetch, size))
    )
    panel.resize(Theme.reader_topic_panel_width, Theme.reader_topic_panel_height)
    panel.show()
    panel.set_mode(ReaderTopicMode.THUMBNAILS)
    panel.reset_thumbnails(100)

    qtbot.waitUntil(lambda: bool(interests), timeout=1000)
    assert interests[-1][0][0] == 0

    interests.clear()
    scrollbar = panel._thumbnails_scroll.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())

    qtbot.waitUntil(lambda: bool(interests), timeout=1000)
    visible, prefetch, size = interests[-1]
    assert visible[0] > 0
    assert prefetch
    assert size == (Theme.detail_thumbnail_width, Theme.detail_thumbnail_height)

    context.close()


def test_reader_settings_panel_matches_figma_side_panel_geometry(qtbot) -> None:
    context = create_app_context()
    panel = ReaderSettingsPanel(context.resources)
    qtbot.addWidget(panel)
    panel.resize(Theme.reader_settings_panel_width, Theme.reader_height)
    panel.set_settings(ReaderSettings())

    margins = panel.layout().contentsMargins()
    assert panel.width() == Theme.reader_settings_panel_width
    assert panel.layout().spacing() == Theme.reader_settings_gap
    assert margins.left() == Theme.reader_settings_panel_layout_margin
    assert margins.top() == Theme.reader_settings_panel_layout_margin
    assert margins.right() == Theme.reader_settings_panel_layout_margin
    assert margins.bottom() == Theme.reader_settings_panel_layout_margin

    sections = [child for child in panel.findChildren(QFrame) if child.property("class") == "ReaderSettingsSection"]
    rows = [child for child in panel.findChildren(QFrame) if child.property("class") == "ReaderSettingsRow"]
    switches = [
        child for child in panel.findChildren(QFrame) if child.property("class") == "ReaderSettingsSmallSwitch"
    ]
    controls = [
        child for child in panel.findChildren(QFrame) if child.property("class") == "ReaderSettingsSmallControl"
    ]

    assert len(sections) == 2
    assert {section.height() for section in sections} == {Theme.reader_settings_section_height}
    assert len(rows) == 7
    assert {row.height() for row in rows} == {Theme.reader_settings_row_height}
    assert len(switches) == 4
    assert {switch.size().width() for switch in switches} == {Theme.settings_switch_width}
    assert {switch.size().height() for switch in switches} == {Theme.settings_switch_height}
    assert len(controls) == 3
    assert {control.size().width() for control in controls} == {Theme.reader_settings_control_width}
    assert {control.size().height() for control in controls} == {Theme.reader_settings_option_height}

    context.close()


def test_reader_settings_panel_opens_as_right_full_height_panel(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "reader.cbz"
    image = tmp_path / "001.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")
    context = create_app_context()
    window = ReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    window.shell._toggle_settings_panel()

    assert window.settings_panel.isVisible()
    assert window.settings_panel.geometry().getRect() == (
        Theme.reader_width - Theme.reader_settings_panel_width,
        0,
        Theme.reader_settings_panel_width,
        Theme.reader_height,
    )

    window.close()
    context.close()


def test_reader_settings_panel_closes_on_outside_click_and_ignores_auto_hide(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "reader.cbz"
    image = tmp_path / "001.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")
    context = create_app_context()
    window = ReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    window.shell._toggle_settings_panel()
    window.shell._hide_inactive_controls()

    assert window.settings_panel.isVisible()

    qtbot.mouseClick(window.canvas, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))

    assert window.settings_panel.isHidden()

    window.close()
    context.close()


def test_reader_auto_hide_uses_direct_visibility_without_graphics_effects(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "reader.cbz"
    image = tmp_path / "001.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")
    context = create_app_context()
    window = ReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()

    controls = (window.header, window.footer, window.left_arrow, window.right_arrow)
    assert all(control.graphicsEffect() is None for control in controls)

    window.shell._control_interaction_active = lambda: False  # type: ignore[method-assign]
    window.shell._hide_inactive_controls()

    assert all(control.isHidden() for control in controls)

    window.shell._toggle_settings_panel()
    window.shell._show_controls((window.footer,), reset_timer=False)

    assert window.footer.isVisible()
    assert window.settings_panel.isVisible()
    assert window.header.isHidden()
    assert window.left_arrow.isHidden()
    assert window.right_arrow.isHidden()

    window.close()
    context.close()


def test_reader_open_hide_reveal_does_not_emit_qpainter_effect_warnings(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "reader.cbz"
    image = tmp_path / "001.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")
    context = create_app_context()
    messages: list[str] = []
    previous_handler = qInstallMessageHandler(lambda _mode, _context, message: messages.append(message))
    window: ReaderWindow | None = None
    try:
        window = ReaderWindow(context, source)
        qtbot.addWidget(window)
        window.resize(Theme.reader_width, Theme.reader_height)
        window.show()
        qtbot.wait(0)

        window.shell._control_interaction_active = lambda: False  # type: ignore[method-assign]
        window.shell._hide_inactive_controls()
        qtbot.wait(0)
        window.shell._show_controls(reset_timer=False)
        qtbot.wait(0)
    finally:
        qInstallMessageHandler(previous_handler)
        if window is not None:
            window.close()
        context.close()

    blocked_fragments = (
        "QPainter::begin",
        "Painter not active",
        "QWidgetEffectSourcePrivate::pixmap",
    )
    assert not [message for message in messages if any(fragment in message for fragment in blocked_fragments)]


def test_reader_settings_numeric_controls_clamp_and_revert_invalid_input(qtbot) -> None:
    context = create_app_context()
    panel = ReaderSettingsPanel(context.resources)
    qtbot.addWidget(panel)
    panel.set_settings(ReaderSettings(vertical_custom_enabled=True))

    panel.spacing_control._field.setText("-5")
    panel.spacing_control.commit_text()
    assert panel.spacing_control.value == 0
    assert panel.spacing_control._field.text() == "0px"

    panel.spacing_control._field.setText("999px")
    panel.spacing_control.commit_text()
    assert panel.spacing_control.value == 200
    assert panel.spacing_control._field.text() == "200px"

    panel.zoom_control._field.setText("2")
    panel.zoom_control.commit_text()
    assert panel.zoom_control.value == 25
    assert panel.zoom_control._field.text() == "25 %"

    panel.zoom_control._field.setText("abc")
    panel.zoom_control.commit_text()
    assert panel.zoom_control.value == 25
    assert panel.zoom_control._field.text() == "25 %"

    context.close()


def test_reader_settings_zoom_value_text_fits_three_digits(qtbot) -> None:
    context = create_app_context()
    panel = ReaderSettingsPanel(context.resources)
    qtbot.addWidget(panel)
    panel.set_settings(ReaderSettings(vertical_custom_enabled=True, vertical_zoom_percent=100))

    field = panel.zoom_control._field
    assert panel.zoom_control.size().width() == Theme.reader_settings_control_width
    assert panel.zoom_control.size().height() == Theme.reader_settings_option_height
    assert field.text() == "100 %"

    # The Figma sample uses a two-digit value, but JoyRead allows 200%.
    # Keep enough text room after Qt/QSS padding so right alignment cannot
    # clip the leading digit in values like "100 %" or "200 %".
    field.setText("200 %")
    available_width = field.width() - 4
    assert field.textMargins().left() == 0
    assert field.textMargins().right() == 0
    assert field.fontMetrics().horizontalAdvance("100 %") <= available_width
    assert field.fontMetrics().horizontalAdvance("200 %") <= available_width

    context.close()


def test_reader_settings_disabled_rows_use_half_opacity(qtbot) -> None:
    context = create_app_context()
    panel = ReaderSettingsPanel(context.resources)
    qtbot.addWidget(panel)

    panel.set_settings(ReaderSettings(custom_enabled=False, vertical_custom_enabled=False))

    assert panel.one_page_row.graphicsEffect().opacity() == pytest.approx(0.5)
    assert panel.fit_row.graphicsEffect().opacity() == pytest.approx(0.5)
    assert panel.vertical_fit_width_row.graphicsEffect().opacity() == pytest.approx(0.5)
    assert panel.spacing_row.graphicsEffect().opacity() == pytest.approx(0.5)
    assert panel.zoom_row.graphicsEffect().opacity() == pytest.approx(0.5)

    panel.set_settings(ReaderSettings(custom_enabled=True, vertical_custom_enabled=True))

    assert panel.one_page_row.graphicsEffect().opacity() == pytest.approx(1.0)
    assert panel.fit_row.graphicsEffect().opacity() == pytest.approx(1.0)
    assert panel.vertical_fit_width_row.graphicsEffect().opacity() == pytest.approx(1.0)
    assert panel.spacing_row.graphicsEffect().opacity() == pytest.approx(1.0)
    assert panel.zoom_row.graphicsEffect().opacity() == pytest.approx(1.0)

    panel.set_settings(
        ReaderSettings(
            custom_enabled=True,
            vertical_custom_enabled=True,
            vertical_fit_width=True,
        )
    )

    assert panel.vertical_fit_width_row.graphicsEffect().opacity() == pytest.approx(1.0)
    assert panel.spacing_row.graphicsEffect().opacity() == pytest.approx(1.0)
    assert panel.zoom_row.graphicsEffect().opacity() == pytest.approx(0.5)

    context.close()


def test_reader_settings_vertical_switch_is_independent_from_reading_direction(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "reader.cbz"
    image = tmp_path / "001.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")
    context = create_app_context()
    window = ReaderWindow(context, source)
    qtbot.addWidget(window)

    window.shell._set_reader_direction(ReaderDirection.LEFT_TO_RIGHT)
    window.viewmodel.set_vertical_custom_enabled(True)
    assert window.viewmodel.settings.direction == ReaderDirection.LEFT_TO_RIGHT
    assert window.viewmodel.settings.vertical_custom_enabled is True

    window.viewmodel.set_vertical_custom_enabled(False)
    assert window.viewmodel.settings.direction == ReaderDirection.LEFT_TO_RIGHT
    assert window.viewmodel.settings.vertical_custom_enabled is False

    window.close()
    context.close()


def test_shelf_reader_uses_embedded_mode_when_individual_window_disabled(qtbot, tmp_path: Path, monkeypatch) -> None:
    context = _context_with_imported_book(tmp_path, monkeypatch)
    launch_requests: list[object] = []
    window = MainWindow(context, standalone_reader_launcher=launch_requests.append)
    qtbot.addWidget(window)
    book = context.shelf_viewmodel.books[0]

    window.open_reader_for_book(book.uuid)

    assert window._embedded_reader is not None
    assert not window._embedded_reader.header.back_button.isHidden()
    assert launch_requests == []

    window.close()
    context.close()


def test_shelf_reader_uses_independent_mode_when_setting_enabled(qtbot, tmp_path: Path, monkeypatch) -> None:
    context = _context_with_imported_book(tmp_path, monkeypatch)
    context.settings_store.update(individual_read_window=True)
    manager = ApplicationWindowManager(context)
    window = manager.show_library()
    book = context.shelf_viewmodel.books[0]

    window.open_reader_for_book(book.uuid)

    assert window._embedded_reader is None
    assert len(manager.reader_windows) == 1
    reader = manager.reader_windows[0]
    assert reader.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    assert not reader.header.back_button.isVisible()

    reader.close()
    qtbot.wait(0)
    assert manager.reader_windows == ()
    window.close()
    qtbot.wait(0)
    context.close()


def test_shelf_reader_reloads_saved_per_book_settings(qtbot, tmp_path: Path, monkeypatch) -> None:
    context = _context_with_imported_book(tmp_path, monkeypatch)
    window = MainWindow(context)
    qtbot.addWidget(window)
    book = context.shelf_viewmodel.books[0]

    window.open_reader_for_book(book.uuid)
    assert window._embedded_reader is not None
    window._embedded_reader.viewmodel.set_direction(ReaderDirection.LEFT_TO_RIGHT)
    window._embedded_reader.viewmodel.set_vertical_custom_enabled(True)
    window._embedded_reader.viewmodel.set_vertical_fit_width(True)

    def settings_saved() -> bool:
        settings = context.library_service.get_reader_settings(book.uuid)
        return (
            settings is not None
            and settings.direction == ReaderDirection.LEFT_TO_RIGHT
            and settings.vertical_custom_enabled
            and settings.vertical_fit_width
        )

    qtbot.waitUntil(settings_saved, timeout=1000)
    window._close_embedded_reader()

    window.open_reader_for_book(book.uuid)

    assert window._embedded_reader is not None
    assert window._embedded_reader.viewmodel.settings.direction == ReaderDirection.LEFT_TO_RIGHT
    assert window._embedded_reader.viewmodel.settings.vertical_custom_enabled is True
    assert window._embedded_reader.viewmodel.settings.vertical_fit_width is True

    window.close()
    context.close()


def test_main_window_restored_book_opens_after_click(qtbot, tmp_path: Path, monkeypatch) -> None:
    # Patch B contract: a book whose file disappears and then comes
    # back must open on the next click without showing the missing
    # dialog. The shelf-click path runs the VM's
    # ``_refresh_book_state``, which flips the row back to healthy
    # the moment storage_path exists again.
    context = _context_with_imported_book(tmp_path, monkeypatch)
    window = MainWindow(context)
    qtbot.addWidget(window)
    book = context.shelf_viewmodel.books[0]
    file_path = Path(book.file_path)

    # Delete the file, click once → VM detects missing → dialog.
    backup = tmp_path / "backup.bytes"
    backup.write_bytes(file_path.read_bytes())
    file_path.unlink()
    context.shelf_viewmodel.open_book(book.uuid)
    qtbot.waitUntil(lambda: any(b.uuid == book.uuid and b.is_missing for b in context.shelf_viewmodel.books), timeout=2000)

    # Restore the file. Next click heals the row and opens the reader.
    file_path.write_bytes(backup.read_bytes())
    context.shelf_viewmodel.open_book(book.uuid)
    qtbot.waitUntil(lambda: any(b.uuid == book.uuid and not b.is_missing for b in context.shelf_viewmodel.books), timeout=2000)
    qtbot.waitUntil(lambda: window._embedded_reader is not None, timeout=2000)

    window.close()
    context.close()


def test_main_window_shelf_view_no_longer_re_emits_open_signals(qtbot, tmp_path: Path, monkeypatch) -> None:
    # The shelf view used to relay ``book_open_requested`` through
    # ``read_book_requested``. Patch B removed the relay; MainWindow
    # subscribes to the VM directly so the relay isn't even defined.
    context = _context_with_imported_book(tmp_path, monkeypatch)
    window = MainWindow(context)
    qtbot.addWidget(window)

    assert not hasattr(window.shelf_view, "read_book_requested")
    assert not hasattr(window.shelf_view, "read_book_at_requested")

    window.close()
    context.close()


def test_main_window_missing_book_dialog_delete_button_triggers_deletion(qtbot, tmp_path: Path, monkeypatch) -> None:
    context = _context_with_imported_book(tmp_path, monkeypatch)
    window = MainWindow(context)
    qtbot.addWidget(window)
    book = context.shelf_viewmodel.books[0]

    # Confirm path runs the destructive lambda.
    window._show_missing_book_dialog(book.uuid)
    accept = window.dialog_overlay._on_accept
    assert accept is not None
    accept()
    qtbot.waitUntil(lambda: not any(b.uuid == book.uuid for b in context.shelf_viewmodel.books), timeout=2000)

    window.close()
    context.close()


def test_main_window_missing_book_dialog_cancel_keeps_book(qtbot, tmp_path: Path, monkeypatch) -> None:
    context = _context_with_imported_book(tmp_path, monkeypatch)
    window = MainWindow(context)
    qtbot.addWidget(window)
    book = context.shelf_viewmodel.books[0]

    # Cancel path is wired to None — pressing Esc / clicking outside
    # must not delete the book.
    window._show_missing_book_dialog(book.uuid)
    assert window.dialog_overlay._on_reject is None
    # Dismiss without action; book stays in library.
    window.dialog_overlay.hide()
    assert any(b.uuid == book.uuid for b in context.shelf_viewmodel.books)

    window.close()
    context.close()


def test_main_window_close_takes_the_readers_it_opened(qtbot, tmp_path: Path, monkeypatch) -> None:
    """A Reader launched from the shelf belongs to that Library session."""

    context = _context_with_imported_book(tmp_path, monkeypatch)
    context.settings_store.update(individual_read_window=True)
    manager = ApplicationWindowManager(context)
    window = manager.show_library()
    book = context.shelf_viewmodel.books[0]

    window.open_reader_for_book(book.uuid)
    assert len(manager.reader_windows) == 1

    window.close()
    qtbot.wait(0)

    assert manager.main_window is None
    assert manager.reader_windows == ()
    context.close()


def test_main_window_close_keeps_readers_opened_by_the_operating_system(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    """An "Open With" Reader is a root window and must survive the Library."""

    context = _context_with_imported_book(tmp_path, monkeypatch)
    context.settings_store.update(individual_read_window=True)
    manager = ApplicationWindowManager(context)
    window = manager.show_library()
    book = context.shelf_viewmodel.books[0]

    readers = manager.open_files((book.file_path,))
    assert len(readers) == 1
    reader = readers[0]

    window.close()
    qtbot.wait(0)

    assert manager.main_window is None
    assert manager.reader_windows == (reader,)
    assert reader.isVisible()
    reader.close()
    qtbot.wait(0)
    context.close()


def test_rebuilt_main_drops_error_subscribers_from_deleted_window(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = _context_with_imported_book(tmp_path, monkeypatch)
    manager = ApplicationWindowManager(context)
    first = manager.show_library()

    first.close()
    qtbot.waitUntil(lambda: not shiboken6.isValid(first), timeout=1000)
    second = manager.show_library()

    context.shelf_viewmodel.delete_failed.emit("Delete failed after Main rebuild.")

    assert not second.dialog_overlay.isHidden()
    assert second.dialog_overlay.panel.title_text == "Delete Failed"

    second.close()
    qtbot.wait(0)
    context.close()


def _reader_book(source: Path) -> Book:
    now = datetime.now()
    return Book(
        uuid="reader-book",
        title="Reader Book",
        author=None,
        language_tag="en",
        language_name="English",
        book_type="Comic",
        file_format=source.suffix.lstrip(".").upper(),
        file_path=str(source),
        progress=0.0,
        cover_thumbnail_path=None,
        added_at=now,
        updated_at=now,
        last_read_at=None,
        is_favourite=False,
        original_file_name=source.name,
    )


def _context_with_imported_book(tmp_path: Path, monkeypatch) -> object:  # noqa: ANN001 - test helper returns AppContext.
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    source = tmp_path / "library-book.cbz"
    image = tmp_path / "library-page.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")
    context = create_app_context()
    context.import_service.import_files([source])
    return context


def test_reader_panel_paths_fill_body_without_overlapping_holes() -> None:
    rect = QRectF(0, 0, 240, Theme.reader_banner_height)
    header_path = _top_rounded_path(rect)
    footer_path = _bottom_rounded_path(rect)

    assert header_path.contains(QPointF(rect.center().x(), rect.center().y()))
    assert header_path.contains(QPointF(4, rect.bottom() - 4))
    assert not header_path.contains(QPointF(1, 1))
    assert footer_path.contains(QPointF(rect.center().x(), rect.center().y()))
    assert footer_path.contains(QPointF(4, rect.top() + 4))
    assert not footer_path.contains(QPointF(1, rect.bottom() - 1))


def test_reader_progress_slider_filled_track_mirrors_with_direction(qtbot) -> None:
    slider = ReaderProgressSlider()
    qtbot.addWidget(slider)
    slider.resize(240, Theme.reader_slider_height)
    slider.setMinimum(0)
    slider.setMaximum(10)
    slider.setValue(3)

    slider.set_reading_direction(ReaderDirection.LEFT_TO_RIGHT)
    track = slider._track_rect()
    left_to_right = slider._filled_track_rect()

    slider.set_reading_direction(ReaderDirection.RIGHT_TO_LEFT)
    right_to_left = slider._filled_track_rect()

    assert left_to_right.left() == pytest.approx(track.left())
    assert right_to_left.right() == pytest.approx(track.right())
    assert left_to_right.width() == pytest.approx(right_to_left.width())
    assert left_to_right.left() < right_to_left.left()


def test_reader_footer_updates_progress_slider_direction(qtbot, tmp_path: Path) -> None:
    source = tmp_path / "reader.cbz"
    image = tmp_path / "001.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")
    context = create_app_context()
    window = ReaderWindow(context, source)
    qtbot.addWidget(window)

    window.footer.set_page_state(2, 10, ReaderDirection.LEFT_TO_RIGHT)
    assert window.footer.slider.reading_direction == ReaderDirection.LEFT_TO_RIGHT
    assert not window.footer.slider.invertedAppearance()
    assert window.footer.page_indicator.text() == "3/10"

    window.footer.set_direction(ReaderDirection.RIGHT_TO_LEFT)
    assert window.footer.slider.reading_direction == ReaderDirection.RIGHT_TO_LEFT
    assert window.footer.slider.invertedAppearance()
    assert window.footer.page_indicator.text() == "3/10"

    window.close()
    context.close()


# ---------------------------------------------------------------------------
# Hidden Space launch lock overlay


def _initialised_hidden_space_context(tmp_path: Path, monkeypatch) -> object:
    # Mirrors ``_context_with_imported_book`` but also primes the Hidden
    # Space service so the next ``MainWindow`` construction has to gate
    # the shelf behind the lock overlay. The setup goes through the
    # SettingsViewModel so the in-memory ``show_hidden_collection`` /
    # ``hidden_space_initialized`` mirrors stay in sync with the
    # persisted state (which MainWindow now reads from the VM, not the
    # raw settings dataclass).
    context = _context_with_imported_book(tmp_path, monkeypatch)
    context.settings_viewmodel.initialize_hidden_space("Pass1234", "Pass1234", "remember the dog")
    context.settings = context.settings_store.load()
    return context


def test_main_window_shows_hidden_space_lock_when_show_collections_persisted(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    context = _initialised_hidden_space_context(tmp_path, monkeypatch)
    window = MainWindow(context)
    qtbot.addWidget(window)

    assert window._lock_overlay is not None
    # ``isVisible`` returns False until the parent window is shown, which
    # we deliberately skip in tests; ``isHidden`` is the visibility-state
    # check that doesn't require an actual window-server.
    assert window._lock_overlay.isHidden() is False

    window.close()
    context.close()


def test_main_window_lock_overlay_verifies_password_and_reveals_shelf(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    context = _initialised_hidden_space_context(tmp_path, monkeypatch)
    window = MainWindow(context)
    qtbot.addWidget(window)
    assert window._lock_overlay is not None

    # Wrong password leaves the overlay in place.
    window._lock_overlay._password.setText("WrongPass")
    window._lock_overlay._on_verify_clicked()
    assert window._lock_overlay is not None
    assert window._lock_overlay.isHidden() is False

    # Correct password tears the overlay down and keeps the toggle on.
    window._lock_overlay._password.setText("Pass1234")
    window._lock_overlay._on_verify_clicked()
    assert window._lock_overlay is None
    assert context.settings_store.load().show_hidden_collection is True

    window.close()
    context.close()


def test_main_window_lock_overlay_hide_button_disables_toggle_without_password(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    context = _initialised_hidden_space_context(tmp_path, monkeypatch)
    window = MainWindow(context)
    qtbot.addWidget(window)
    assert window._lock_overlay is not None

    window._lock_overlay._on_hide_clicked()

    assert window._lock_overlay is None
    # Hide flips the persisted toggle off; the password stays configured.
    settings = context.settings_store.load()
    assert settings.show_hidden_collection is False
    assert settings.hidden_space_password_hash is not None

    window.close()
    context.close()


def test_main_window_skips_lock_overlay_when_show_collections_is_off(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    context = _initialised_hidden_space_context(tmp_path, monkeypatch)
    # Flip the toggle off through the VM so the persisted state and the
    # VM mirror stay in sync (MainWindow reads the latter).
    context.settings_viewmodel.set_show_hidden_collection(False)
    context.settings = context.settings_store.load()
    window = MainWindow(context)
    qtbot.addWidget(window)

    assert window._lock_overlay is None

    window.close()
    context.close()


def test_reader_topic_thumbnail_selection_closes_the_panel(qtbot, tmp_path: Path) -> None:
    """Picking a page from the thumbnail grid is a "take me there", so the
    panel should stop covering the page the user just chose."""

    source = tmp_path / "reader-select.cbz"
    image = tmp_path / "page.png"
    Image.new("RGB", (20, 30), "#336699").save(image, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        for index in range(4):
            archive.write(image, f"{index:03d}.png")
    context = create_app_context()
    window = ReaderWindow(context, source)
    qtbot.addWidget(window)
    window.resize(Theme.reader_width, Theme.reader_height)
    window.show()
    qtbot.waitUntil(lambda: window.shell.viewmodel.page_count == 4, timeout=3000)

    window.shell._show_topic_panel(ReaderTopicMode.THUMBNAILS)  # noqa: SLF001
    assert window.topic_panel.isVisible()

    window.topic_panel.thumbnail_selected.emit(2)

    assert window.shell.viewmodel.current_index == 2, "the seek must still happen"
    assert window.topic_panel.isHidden(), "selecting a page must dismiss the panel"
    assert window.header.topic_button_group.active_mode is None

    window.close()
    context.close()
