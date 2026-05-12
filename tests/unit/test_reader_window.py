from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from PIL import Image
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, qInstallMessageHandler
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFrame

from joyread.app.app_context import create_app_context
from joyread.core.reader import ReaderDirection, ReaderSettings
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.views.main_window import MainWindow
from joyread.ui.views.reader_window import ReaderWindow
from joyread.ui.widgets.reader_controls import ReaderProgressSlider, _bottom_rounded_path, _top_rounded_path
from joyread.ui.widgets.reader_settings_panel import ReaderSettingsPanel


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
    assert len(rows) == 6
    assert {row.height() for row in rows} == {Theme.reader_settings_row_height}
    assert len(switches) == 3
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
    assert panel.spacing_row.graphicsEffect().opacity() == pytest.approx(0.5)
    assert panel.zoom_row.graphicsEffect().opacity() == pytest.approx(0.5)

    panel.set_settings(ReaderSettings(custom_enabled=True, vertical_custom_enabled=True))

    assert panel.one_page_row.graphicsEffect().opacity() == pytest.approx(1.0)
    assert panel.fit_row.graphicsEffect().opacity() == pytest.approx(1.0)
    assert panel.spacing_row.graphicsEffect().opacity() == pytest.approx(1.0)
    assert panel.zoom_row.graphicsEffect().opacity() == pytest.approx(1.0)

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
    window = MainWindow(context)
    qtbot.addWidget(window)
    book = context.shelf_viewmodel.books[0]

    window.open_reader_for_book(book.uuid)

    assert window._embedded_reader is not None
    assert not window._embedded_reader.header.back_button.isHidden()
    assert not window._reader_windows

    window.close()
    context.close()


def test_shelf_reader_uses_independent_mode_when_setting_enabled(qtbot, tmp_path: Path, monkeypatch) -> None:
    context = _context_with_imported_book(tmp_path, monkeypatch)
    context.settings_store.update(individual_read_window=True)
    window = MainWindow(context)
    qtbot.addWidget(window)
    book = context.shelf_viewmodel.books[0]

    window.open_reader_for_book(book.uuid)

    assert window._embedded_reader is None
    assert len(window._reader_windows) == 1
    reader = window._reader_windows[0]
    assert reader.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    assert not reader.header.back_button.isVisible()

    reader.close()
    qtbot.wait(0)
    assert window._reader_windows == []
    window.close()
    context.close()


def test_main_window_close_closes_independent_readers(qtbot, tmp_path: Path, monkeypatch) -> None:
    context = _context_with_imported_book(tmp_path, monkeypatch)
    context.settings_store.update(individual_read_window=True)
    window = MainWindow(context)
    qtbot.addWidget(window)
    book = context.shelf_viewmodel.books[0]

    window.open_reader_for_book(book.uuid)
    assert len(window._reader_windows) == 1

    window.close()
    qtbot.wait(0)

    assert window._reader_windows == []
    context.close()


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
