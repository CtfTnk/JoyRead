import json
from datetime import datetime

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QMouseEvent, QColor
from PySide6.QtWidgets import QApplication

from joyread.core.models.collection import Collection
from joyread.core.services.library_service import LibraryService
from joyread.infrastructure.config.settings_store import SettingsStore
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.infrastructure.i18n import locale_service
from joyread.ui.viewmodels.shelf_viewmodel import ShelfViewModel
from joyread.ui.widgets.sidebar import SidebarWidget
from joyread.ui.widgets.section_banner import SectionBanner
from joyread.ui.resources.styles.theme import Theme
from tests.support.in_memory_book_repository import InMemoryBookRepository


def bound_sidebar(qtbot, settings=None, store=None):
    vm = ShelfViewModel(LibraryService(InMemoryBookRepository()), settings=settings, settings_store=store)
    widget = SidebarWidget(ResourceLoader())
    qtbot.addWidget(widget)
    widget.section_expansion_requested.connect(vm.set_sidebar_section_expanded)
    vm.sidebar_sections_changed.connect(lambda: widget.set_collapsed_sections(vm.sidebar_collapsed_sections))
    widget.set_collapsed_sections(vm.sidebar_collapsed_sections)
    widget.resize(widget.width(), 600)
    widget.show()
    return widget, vm


def test_sections_toggle_independently_without_navigation_or_selection_changes(qtbot):
    sidebar, vm = bound_sidebar(qtbot)
    navigation = []
    sidebar.navigation_requested.connect(navigation.append)
    vm.selected_book_ids = {'chosen'}
    original = dict(sidebar._buttons)
    books, collections = sidebar._sections.values()
    qtbot.mouseClick(books.banner, Qt.MouseButton.LeftButton)
    assert not books.body.isVisible() and collections.body.isVisible()
    assert sidebar._buttons['settings'].isVisible()
    assert books.banner.toolTip().startswith('Expand')
    qtbot.mouseClick(collections.banner, Qt.MouseButton.LeftButton)
    assert not collections.body.isVisible()
    qtbot.keyClick(books.banner, Qt.Key.Key_Space)
    assert books.body.isVisible() and not collections.body.isVisible()
    assert sidebar._buttons == original and vm.selected_book_ids == {'chosen'} and navigation == []


def test_hidden_space_and_collection_refresh_preserve_collapsed_state(qtbot):
    sidebar, vm = bound_sidebar(qtbot)
    vm.set_sidebar_section_expanded('bookshelf', False)
    sidebar.set_hidden_visible(True)
    assert not sidebar._hidden_item.isVisibleTo(sidebar)
    vm.set_sidebar_section_expanded('bookshelf', True)
    assert sidebar._hidden_item.isVisibleTo(sidebar)
    sidebar.set_hidden_visible(False)
    vm.set_sidebar_section_expanded('bookshelf', False)
    vm.set_sidebar_section_expanded('bookshelf', True)
    assert not sidebar._hidden_item.isVisibleTo(sidebar)
    vm.set_sidebar_section_expanded('collections', False)
    now = datetime(2026, 1, 1)
    sidebar.set_active('collection:one')
    sidebar.set_collections([Collection('one', 'Queue', False, now, now)])
    assert not sidebar._sections['collections'].body.isVisible()
    vm.set_sidebar_section_expanded('collections', True)
    assert sidebar._buttons['collection:one'].property('selected') == 'true'
    assert sidebar._buttons['new_collection'].isVisible()


def test_persistence_old_config_unknown_section_and_noop(qtbot, tmp_path):
    store = SettingsStore(support_root=tmp_path, default_storage_root=tmp_path / 'library')
    sidebar, vm = bound_sidebar(qtbot, store.load(), store)
    assert all(section.expanded for section in sidebar._sections.values())
    changes = []
    vm.sidebar_sections_changed.connect(lambda: changes.append(True))
    vm.set_sidebar_section_expanded('bookshelf', False)
    vm.set_sidebar_section_expanded('bookshelf', False)
    vm.set_sidebar_section_expanded('future-library', False)
    assert len(changes) == 2
    restored, restored_vm = bound_sidebar(qtbot, store.load(), store)
    assert not restored._sections['bookshelf'].expanded
    restored_vm.set_sidebar_section_expanded('bookshelf', True)
    assert store.load().sidebar_collapsed_sections == ('future-library',)
    raw = json.loads(store.settings_path.read_text())
    raw['sidebar_collapsed_sections'] = 'bad'
    store.settings_path.write_text(json.dumps(raw))
    assert store.load().sidebar_collapsed_sections == ()


def test_localization_and_release_outside_do_not_toggle(qtbot):
    sidebar, vm = bound_sidebar(qtbot)
    banner = sidebar._book_shelf_banner
    qtbot.mousePress(banner, Qt.MouseButton.LeftButton)
    qtbot.mouseRelease(banner, Qt.MouseButton.LeftButton, pos=QPoint(-1, -1))
    assert sidebar._sections['bookshelf'].expanded
    qtbot.keyClick(banner, Qt.Key.Key_Return)
    for language in ('Chinese', 'Japanese', 'English'):
        locale_service.load_language(language)
        sidebar.refresh_labels()
        assert not sidebar._sections['bookshelf'].expanded
        assert banner.accessibleName() == banner._label.text()
        assert banner.toolTip() == banner.accessibleDescription()
    ordinary = SectionBanner('Settings group', ResourceLoader())
    qtbot.addWidget(ordinary)
    assert ordinary.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_header_press_feedback_clears_when_pointer_leaves(qtbot):
    sidebar, vm = bound_sidebar(qtbot)
    header = sidebar._book_shelf_banner
    qtbot.waitExposed(sidebar)
    point = QPoint(5, header.height() // 2)
    def move(position, buttons):
        QApplication.sendEvent(header, QMouseEvent(QEvent.Type.MouseMove, position,
            header.mapToGlobal(position), Qt.MouseButton.NoButton, buttons,
            Qt.KeyboardModifier.NoModifier))
    def color():
        pixmap = header.grab()
        ratio = pixmap.devicePixelRatioF()
        return pixmap.toImage().pixelColor(round(point.x() * ratio), round(point.y() * ratio))
    move(point, Qt.MouseButton.NoButton)
    assert color() == QColor(Theme.color_sidebar_item_hover)
    qtbot.mousePress(header, Qt.MouseButton.LeftButton, pos=point)
    assert color() == QColor(Theme.color_selected)
    move(QPoint(-10, -10), Qt.MouseButton.LeftButton)
    assert color() not in (QColor(Theme.color_selected), QColor(Theme.color_sidebar_item_hover))
    qtbot.mouseRelease(header, Qt.MouseButton.LeftButton, pos=QPoint(-10, -10))
    assert sidebar._sections['bookshelf'].expanded
    qtbot.mouseClick(header, Qt.MouseButton.LeftButton, pos=point)
    move(QPoint(-10, -10), Qt.MouseButton.NoButton)
    assert not sidebar._sections['bookshelf'].expanded
    assert color() not in (QColor(Theme.color_selected), QColor(Theme.color_sidebar_item_hover))


def test_unchanged_section_does_not_replace_arrow_or_request_relayout(qtbot, monkeypatch):
    sidebar, vm = bound_sidebar(qtbot)
    collections = sidebar._sections['collections']
    def unexpected(*args):
        raise AssertionError('Unchanged section should not update its content or arrow')
    monkeypatch.setattr(collections.body, 'setVisible', unexpected)
    monkeypatch.setattr(collections.banner._arrow, 'setPixmap', unexpected)
    vm.set_sidebar_section_expanded('bookshelf', False)
    vm.set_sidebar_section_expanded('bookshelf', True)
