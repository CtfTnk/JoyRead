import json

import pytest
from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QApplication, QBoxLayout, QMainWindow

from joyread.infrastructure.config.settings_store import SettingsStore
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.infrastructure.i18n import locale_service
from joyread.ui.viewmodels.settings_viewmodel import SettingsViewModel
from joyread.ui.widgets.window_geometry import WindowGeometryController, fitted_window_rect
from joyread.ui.widgets.settings_page import SettingsDropdownItem, SettingsPageWidget, SettingsOptionItem
from joyread.ui.widgets.top_toolbar import TopToolbarWidget
from joyread.ui.resources.styles.theme import Theme


def test_fit_small_screen_keeps_titlebar_and_minimum():
    available = QRect(-900, 30, 800, 600)
    assert fitted_window_rect(QSize(1200, 860), QSize(738, 600), available) == available
    reader = fitted_window_rect(QSize(1200, 860), QSize(500, 706), available)
    assert reader == QRect(-900, 30, 800, 706)
    large = QRect(0, 30, 1800, 1100)
    assert fitted_window_rect(QSize(1200, 860), QSize(738, 600), large).center() == large.center()


def test_window_preferences_roundtrip_and_malformed_fallback(tmp_path):
    store = SettingsStore(support_root=tmp_path, default_storage_root=tmp_path / "library")
    vm = SettingsViewModel(store.load(), store)
    assert vm.window_size("library") is None
    vm.remember_window_size("library", (1000, 700))
    vm.remember_window_size("reader", (900, 750))
    restored = SettingsViewModel(store.load(), store)
    assert restored.window_size("library") == (1000, 700)
    assert restored.window_size("reader") == (900, 750)
    restored.reset_window_sizes()
    assert store.load().library_window_size is None
    raw = json.loads(store.settings_path.read_text())
    raw['library_window_size'] = [False, -4]
    raw['reader_window_size'] = 'bad'
    store.settings_path.write_text(json.dumps(raw))
    assert store.load().reader_window_size is None and store.load().library_window_size is None


def test_controller_remembers_normal_resize_but_not_fit_or_maximize(qtbot):
    vm = SettingsViewModel()
    window = QMainWindow()
    qtbot.addWidget(window)
    window.setMinimumSize(300, 200)
    controller = WindowGeometryController(window, vm, "library")
    controller.place()
    window.show()
    qtbot.waitExposed(window)
    assert vm.window_size("library") is None

    window.resize(800, 610)
    qtbot.waitUntil(lambda: vm.window_size("library") == (800, 610))
    window.showMaximized()
    qtbot.wait(450)
    assert vm.window_size("library") == (800, 610)
    vm.reset_window_sizes()
    qtbot.wait(450)
    assert vm.window_size("library") is None
    assert not window.isMaximized()
    window.close()
    assert vm.window_size("library") is None


def test_close_flushes_pending_size_and_reader_memory_is_independent(qtbot):
    vm = SettingsViewModel()
    windows = []
    for kind, size in (("library", (810, 610)), ("reader", (720, 710))):
        window = QMainWindow()
        qtbot.addWidget(window)
        WindowGeometryController(window, vm, kind)
        window.show()
        qtbot.waitExposed(window)
        window.resize(*size)
        window.close()  # Before the debounce fires.
        assert vm.window_size(kind) == size
        windows.append(window)
    assert vm.window_size("library") == (810, 610)
    assert vm.window_size("reader") == (720, 710)


def test_real_window_uses_visible_available_origin_on_small_screen(qtbot):
    vm = SettingsViewModel()
    window = QMainWindow()
    window.setWindowFlag(Qt.WindowType.FramelessWindowHint)
    qtbot.addWidget(window)
    window.setMinimumSize(300, 200)
    controller = WindowGeometryController(window, vm, "library")
    controller.place()
    window.show()
    qtbot.waitExposed(window)
    qtbot.wait(20)
    available = window.screen().availableGeometry()
    expected = fitted_window_rect(QSize(1200, 860), window.minimumSize(), available)
    assert window.geometry() == expected


def test_search_shrinks_without_overlap_or_losing_input(qtbot):
    QApplication.instance().setStyleSheet(ResourceLoader().load_stylesheet())
    bar = TopToolbarWidget(ResourceLoader())
    qtbot.addWidget(bar)
    bar.resize(900, Theme.toolbar_height)
    bar.show()
    bar._search_panel.set_expanded(True)
    bar._search_panel._input.setText('keep this query')
    bar.resize(490, Theme.toolbar_height)
    qtbot.wait(10)
    controls = [bar._title, bar._search_panel, bar._filter_dropdown, bar._tag_filter_button]
    for i, control in enumerate(controls):
        assert bar.rect().contains(control.geometry())
        for other in controls[i+1:]:
            assert not control.geometry().intersects(other.geometry())
    assert bar._search_panel.width() < Theme.search_panel_width
    assert bar._filter_dropdown.isVisible() and bar._search_panel.query == 'keep this query'
    bar.resize(1000, Theme.toolbar_height)
    qtbot.wait(10)
    assert bar._search_panel.width() == Theme.search_panel_width


def test_settings_control_stacks_then_returns_to_same_row(qtbot):
    QApplication.instance().setStyleSheet(ResourceLoader().load_stylesheet())
    row = SettingsDropdownItem('Conversion policy for imported archives', 'Keep the original format',
        ('Keep the original format', 'Convert expensive and nested formats'), ResourceLoader())
    qtbot.addWidget(row)
    row.resize(350, 28)
    row.show()
    qtbot.wait(20)
    assert row._row_layout.direction() == QBoxLayout.Direction.TopToBottom
    assert row._option_frame.y() >= row._name_cell.geometry().bottom()
    assert row.rect().contains(row._option_frame.geometry())
    row.dropdown.set_value('Convert expensive and nested formats')
    row.resize(1000, row.height())
    qtbot.wait(20)
    assert row._row_layout.direction() == QBoxLayout.Direction.LeftToRight
    assert row.height() == Theme.settings_item_height
    assert row.dropdown.value == 'Convert expensive and nested formats'


@pytest.mark.parametrize("language", ["English", "Chinese", "Japanese"])
def test_settings_minimum_width_keeps_all_controls_inside(qtbot, language):
    QApplication.instance().setStyleSheet(ResourceLoader().load_stylesheet())
    locale_service.load_language(language)
    page = SettingsPageWidget(SettingsViewModel(), ResourceLoader())
    qtbot.addWidget(page)
    page.resize(Theme.settings_panel_min_width, Theme.settings_panel_min_height)
    page.show()
    qtbot.wait(30)
    for row in page.findChildren(SettingsOptionItem):
        assert row.rect().contains(row._option_frame.geometry())
        assert row._option_frame.rect().contains(row._option.geometry())
    locale_service.load_language("English")
