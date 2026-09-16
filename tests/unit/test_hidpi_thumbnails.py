from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from joyread.core.services.library_service import LibraryService
from joyread.core.services.thumbnail_service import CoverCropState
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.viewmodels.shelf_viewmodel import ShelfViewModel
from joyread.ui.views.shelf_view import ShelfView
from tests.support.in_memory_book_repository import InMemoryBookRepository
from tests.unit.test_shelf_viewmodel import FakeThumbnailService, RecordingTaskService
from tests.unit.test_thumbnail_service import _thumbnail_service, _synthetic_book, _png_bytes


class DensityThumbnails(FakeThumbnailService):
    def __init__(self):
        super().__init__()
        self.cover_sizes = []
        self.page_sizes = []

    def generate_cover(self, book, size):
        self.cover_sizes.append(size)
        return Path(f"/tmp/{book.uuid}-{size[0]}x{size[1]}.png")

    def stream_thumbnails(self, source, indices, size, emit_item):
        self.page_sizes.append(size)
        super().stream_thumbnails(source, indices, size, emit_item)


def model():
    service, tasks = DensityThumbnails(), RecordingTaskService()
    vm = ShelfViewModel(LibraryService(InMemoryBookRepository()), service, tasks, cover_size=(200, 284))
    vm.load_books()
    return vm, service, tasks


@pytest.mark.parametrize('ratio,cover,page', [
    (1, (200,284), (100,142)), (1.25, (250,355), (125,178)),
    (1.5, (300,426), (150,213)), (2, (400,568), (200,284)), (3, (600,852), (300,426)),
])
def test_window_density_sets_physical_generation_dimensions(ratio, cover, page):
    vm, service, tasks = model()
    vm.set_thumbnail_device_pixel_ratio(ratio)
    assert vm.thumbnail_render_size((100,142)) == page
    vm.request_covers_for_books(('mock-book-01',))
    tasks.complete()
    assert service.cover_sizes == [cover]


def test_old_density_worker_cannot_replace_current_request():
    vm, service, tasks = model()
    vm.request_covers_for_books(('mock-book-01',))
    vm.set_thumbnail_device_pixel_ratio(2)
    vm.request_covers_for_books(('mock-book-01',))
    tasks.complete(0)
    assert 'mock-book-01' not in vm.cover_paths
    assert 'mock-book-01' in vm._pending_cover_ids
    tasks.complete(1)
    assert service.cover_sizes == [(200,284),(400,568)]
    assert str(vm.cover_paths['mock-book-01']).endswith('400x568.png')


def test_pending_generated_cover_does_not_overwrite_new_custom_cover():
    vm, _, tasks = model()
    vm.request_covers_for_books(('mock-book-01',))
    custom = Path('/tmp/chosen-crop.png')
    vm._handle_book_cover_success('mock-book-01', custom)
    tasks.complete(0)
    assert vm.cover_paths['mock-book-01'] == custom


def test_detail_density_change_uses_latest_size_and_reuses_open_source():
    vm, service, tasks = model()
    key = 'mock-book-15'
    vm.show_detail(key)
    counts = []
    vm.detail_thumbnail_source_ready.connect(lambda *args: counts.append(args))
    vm.set_detail_thumbnail_interest(key, (0,), (), (100,142))
    vm.set_detail_thumbnail_interest(key, (0,), (), (200,284))
    tasks.complete(0)  # Opening source uses latest request, not captured old size.
    handle = vm._detail_source_handle
    tasks.complete()
    assert service.page_sizes == [(200,284)]
    vm.set_detail_thumbnail_interest(key, (0,), (), (100,142))
    tasks.complete()
    assert vm._detail_source_handle is handle
    assert not getattr(handle, 'closed', False)
    assert service.page_sizes[-1] == (100,142)
    assert len(counts) == 1  # A density change does not clear existing widgets.
    assert sum(name.startswith('detail-thumbnail-source') for name in tasks.submitted) == 1


def test_generated_density_variants_coexist_and_larger_cover_is_reused(tmp_path):
    service = _thumbnail_service(tmp_path)
    book = _synthetic_book(tmp_path)
    low = service.generate_cover(book, (200,284))
    high = service.generate_cover(book, (400,568))
    assert low.exists() and high.exists()
    with Image.open(high) as image:
        assert image.size == (400,568)
    assert service.existing_cover_path(book, (300,426)) == high
    assert service.existing_cover_path(book, (200,284)) == low
    unavailable = replace(book, file_path=str(tmp_path/'gone.cbz'))
    assert service.existing_cover_path(unavailable, (600,852)) == high
    service.close()


def test_custom_crop_keeps_priority_and_new_save_can_be_high_resolution(tmp_path):
    service = _thumbnail_service(tmp_path)
    book = _synthetic_book(tmp_path)
    source = _png_bytes((800,1000), '#aa2244')
    crop = CoverCropState('custom', 100, .25, -.5, (170,241))
    path = service.save_edited_cover(book, source, crop, (170,241))
    custom_book = replace(book, cover_thumbnail_path=str(path))
    assert service.existing_cover_path(custom_book, (400,568)) == path
    saved = service.save_edited_cover(custom_book, source, crop, (400,568))
    with Image.open(saved) as image:
        assert image.size == (400,568)
    assert service.existing_cover_path(custom_book, (200,284)) == saved
    service.close()


def test_larger_cover_lookup_scans_directory_once_for_multiple_books(tmp_path, monkeypatch):
    service = _thumbnail_service(tmp_path)
    base = _synthetic_book(tmp_path)
    books = [replace(base, uuid=f'book-{i}') for i in range(3)]
    directory = service._paths.paths.thumbnails / 'covers'
    directory.mkdir(parents=True, exist_ok=True)
    for book in books:
        (directory / f'{book.uuid}-generated-400x568.png').write_bytes(b'cached cover')
    scans = []
    glob = Path.glob
    def counted(path, pattern):
        if path == directory:
            scans.append(pattern)
        return glob(path, pattern)
    monkeypatch.setattr(Path, 'glob', counted)
    for book in books:
        assert service.existing_cover_path(book, (300,426)).name.endswith('400x568.png')
    assert len(scans) == 1
    service.close()


def test_shelf_dpr_event_preserves_card_geometry_and_selection(qtbot, monkeypatch):
    vm = ShelfViewModel(LibraryService(InMemoryBookRepository()), cover_size=(200,284))
    vm.load_books()
    view = ShelfView(vm, ResourceLoader());qtbot.addWidget(view)
    monkeypatch.setattr(view, 'devicePixelRatioF', lambda: 1.0)
    view.resize(1000,800);view.show();view.render()
    qtbot.wait(10)
    key = next(iter(view.grid.book_controls))
    vm.set_selection({key})
    control = view.grid.book_controls[key]
    old_size = control.size()
    monkeypatch.setattr(view, 'devicePixelRatioF', lambda: 2.0)
    QApplication.sendEvent(view, QEvent(QEvent.Type.DevicePixelRatioChange))
    qtbot.waitUntil(lambda: vm._cover_size == (400,568))
    assert vm.selected_book_ids == {key}
    assert view.grid.book_controls[key] is control and control.size() == old_size
