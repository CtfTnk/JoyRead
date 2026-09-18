import json
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from PIL import Image
from PySide6.QtCore import QRect, QPoint
from PySide6.QtWidgets import QApplication

from joyread.app.app_context import create_app_context
from joyread.core.reader import ReaderDirection, ReaderSettings
from joyread.infrastructure.config.settings_store import SettingsStore
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.infrastructure.i18n import locale_service
from joyread.ui.viewmodels.settings_viewmodel import SettingsViewModel
from joyread.ui.viewmodels.reader_viewmodel import ReaderViewModel
from joyread.ui.views.reader_window import ReaderWindow
from joyread.ui.widgets.reader_settings_panel import ReaderSettingsPanel
from tests.unit.test_reader_viewmodel import _viewmodel, _ManualPageTaskService


def test_global_prefetch_defaults_bounds_and_restart(tmp_path):
    store = SettingsStore(support_root=tmp_path, default_storage_root=tmp_path / 'library')
    settings = store.load()
    assert (settings.page_prefetch_before, settings.page_prefetch_after) == (4, 8)
    vm = SettingsViewModel(settings, store)
    vm.set_page_prefetch_before(50)
    vm.set_page_prefetch_after(50)
    assert (store.load().page_prefetch_before, store.load().page_prefetch_after) == (10, 20)
    vm.set_page_prefetch_before(-1)
    vm.set_page_prefetch_after(0)
    restored = SettingsViewModel(store.load(), store)
    assert (restored.page_prefetch_before, restored.page_prefetch_after) == (0, 0)
    raw = json.loads(store.settings_path.read_text())
    raw['page_prefetch_before'] = 'bad'
    del raw['page_prefetch_after']
    store.settings_path.write_text(json.dumps(raw))
    assert (store.load().page_prefetch_before, store.load().page_prefetch_after) == (4, 8)


@pytest.mark.parametrize('direction', list(ReaderDirection))
@pytest.mark.parametrize('pages', [(10,), (10, 11), (11, 10), (0,), (49,)])
def test_prefetch_tracks_displayed_bounds_not_layout_direction(direction, pages):
    vm = object.__new__(ReaderViewModel)
    vm.settings = ReaderSettings(direction=direction)
    vm._page_count = 50
    vm._primary_index = pages[0]
    vm._companion_index = pages[1] if len(pages) > 1 else None
    vm._layout_result = SimpleNamespace(page_draws=tuple(SimpleNamespace(page_index=p) for p in pages))
    vm._prefetch_before, vm._prefetch_after = 4, 8
    requests = []
    vm._request_pages = lambda indices: requests.append(set(indices))
    vm._preload_nearby_pages()
    expected = set(vm.current_display_indices)
    # Vertical reading has one progress anchor, plus separate viewport demand.
    bounds = (pages[0],) if direction == ReaderDirection.TOP_TO_BOTTOM else pages
    expected.update(range(max(0, min(bounds) - 4), min(bounds)))
    expected.update(range(max(bounds) + 1, min(50, max(bounds) + 9)))
    assert requests == [expected]


def test_changing_prefetch_while_loading_retains_visible_demand(tmp_path):
    tasks = _ManualPageTaskService()
    vm = _viewmodel(tmp_path, task_service=tasks, prefetch_before=0, prefetch_after=0)
    vm.open_path(tmp_path / 'book.cbz')
    vm.set_viewport_size(1600, 900)
    tasks.run_next_page_task()
    vm.go_next()
    wanted = vm.current_display_indices
    document = vm._document
    vm.set_prefetch_window(4, 8)
    vm.set_prefetch_window(0, 0)
    for _ in range(12):
        if vm.loading_page_index is None:
            break
        tasks.run_next_page_task()
    assert vm.loading_page_index is None
    assert vm.current_display_indices == wanted and vm._document is document


def test_prefetch_change_during_document_open_does_not_invalidate_open(tmp_path):
    vm = _viewmodel(tmp_path)
    cancelled = []
    vm._page_pipeline.cancel_pending_pages = lambda: cancelled.append(True)
    vm.set_prefetch_window(10, 20)
    assert cancelled == []
    vm.open_path(tmp_path / 'book.cbz')
    assert vm._document is not None and vm.page_count > 0
    assert (vm._prefetch_before, vm._prefetch_after) == (10, 20)


def test_two_readers_sync_prefetch_without_reopening_documents(qtbot, tmp_path):
    image = tmp_path / 'page.png'
    Image.new('RGB', (20, 30), 'white').save(image)
    source = tmp_path / 'book.cbz'
    with ZipFile(source, 'w') as archive:
        archive.write(image, 'page.png')
    context = create_app_context()
    original = (context.settings_viewmodel.page_prefetch_before, context.settings_viewmodel.page_prefetch_after)
    windows = [ReaderWindow(context, source), ReaderWindow(context, source)]
    try:
        for window in windows:
            qtbot.addWidget(window)
            window.show()
        qtbot.waitUntil(lambda: all(w.shell.viewmodel._document is not None for w in windows))
        documents = [w.shell.viewmodel._document for w in windows]
        windows[0].shell.settings_panel.prefetch_before_control.set_value(7)
        windows[0].shell.settings_panel.prefetch_after_control.set_value(15)
        for index, window in enumerate(windows):
            vm = window.shell.viewmodel
            assert (vm._prefetch_before, vm._prefetch_after) == (7, 15)
            assert window.shell.settings_panel.prefetch_after_control.value == 15
            assert vm._document is documents[index]
        windows[0].shell.cancel()
        context.settings_viewmodel.set_page_prefetch_after(0)
        assert windows[1].shell.viewmodel._prefetch_after == 0
    finally:
        for window in windows:
            window.close()
        context.settings_viewmodel.set_page_prefetch_before(original[0])
        context.settings_viewmodel.set_page_prefetch_after(original[1])
        context.close()


@pytest.mark.parametrize('language', ['English', 'Chinese', 'Japanese'])
def test_prefetch_controls_fit_and_ignore_custom_layout_toggle(qtbot, language):
    locale_service.load_language(language)
    QApplication.instance().setStyleSheet(ResourceLoader().load_stylesheet())
    panel = ReaderSettingsPanel(ResourceLoader())
    qtbot.addWidget(panel)
    panel.resize(panel.width(), 600)
    panel.set_settings(ReaderSettings())
    panel.show()
    qtbot.wait(10)
    for control in (panel.prefetch_before_control, panel.prefetch_after_control):
        assert panel.rect().contains(QRect(control.mapTo(panel, QPoint()), control.size()))
        assert control.isEnabled()
    panel.prefetch_before_control.set_value(100)
    panel.prefetch_after_control.set_value(100)
    assert panel.prefetch_before_control.value == 10
    assert panel.prefetch_after_control.value == 20
    locale_service.load_language('English')
