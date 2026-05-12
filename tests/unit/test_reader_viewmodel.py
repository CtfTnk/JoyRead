from __future__ import annotations

from pathlib import Path

from PIL import Image

from joyread.core.reader import ReaderDirection, ReaderDisplayMode, ReaderPageImage
from joyread.core.services.archive_extraction_pool import ArchiveExtractionPool
from joyread.core.services.cache_service import CacheService
from joyread.core.services.task_service import TaskHandle, TaskStatus
from joyread.ui.viewmodels.reader_viewmodel import ReaderViewModel


class _SyncTaskService:
    def submit(self, name, callback, *, on_success=None, on_failure=None):  # noqa: ANN001
        handle = TaskHandle(task_id=name)
        handle.status = TaskStatus.RUNNING
        try:
            result = callback()
        except Exception as exc:  # pragma: no cover - test helper only.
            handle.status = TaskStatus.FAILED
            handle.error = exc
            if on_failure is not None:
                on_failure(exc)
            return handle
        handle.status = TaskStatus.COMPLETED
        handle.result = result
        if on_success is not None:
            on_success(result)
        return handle


class _ManualPageTaskService:
    def __init__(self) -> None:
        self.page_tasks = []

    def submit(self, name, callback, *, on_success=None, on_failure=None):  # noqa: ANN001
        handle = TaskHandle(task_id=name)
        handle.status = TaskStatus.RUNNING
        if name.startswith("reader-pages"):
            self.page_tasks.append((handle, callback, on_success, on_failure))
            return handle
        try:
            result = callback()
        except Exception as exc:  # pragma: no cover - test helper only.
            handle.status = TaskStatus.FAILED
            handle.error = exc
            if on_failure is not None:
                on_failure(exc)
            return handle
        handle.status = TaskStatus.COMPLETED
        handle.result = result
        if on_success is not None:
            on_success(result)
        return handle

    def run_next_page_task(self) -> None:
        handle, callback, on_success, on_failure = self.page_tasks.pop(0)
        if handle.status == TaskStatus.CANCELLED:
            return
        try:
            result = callback()
        except Exception as exc:  # pragma: no cover - test helper only.
            handle.status = TaskStatus.FAILED
            handle.error = exc
            if on_failure is not None:
                on_failure(exc)
            return
        handle.status = TaskStatus.COMPLETED
        handle.result = result
        if on_success is not None:
            on_success(result)


class _FakeSession:
    page_count = 5

    def __init__(self, dimensions: tuple[int, int] = (600, 900)) -> None:
        self._dimensions = dimensions

    def get_image(self, index: int) -> bytes | None:
        if not 0 <= index < self.page_count:
            return None
        return _png_bytes()

    def get_dimensions(self, index: int) -> tuple[int, int] | None:
        if not 0 <= index < self.page_count:
            return None
        return self._dimensions


class _FakeSessionService:
    def __init__(self, dimensions: tuple[int, int] = (600, 900)) -> None:
        self._session = _FakeSession(dimensions)

    def open_document(self, path: Path, password=None, *, archive_internal_max_depth=2):  # noqa: ANN001
        return self.open_archive(path, password=password, archive_internal_max_depth=archive_internal_max_depth)

    def open_archive(self, _path: Path, password=None, *, archive_internal_max_depth=2):  # noqa: ANN001
        del archive_internal_max_depth
        return self._session

    def load_page(self, session: _FakeSession, page_index: int) -> ReaderPageImage | None:
        image = session.get_image(page_index)
        dimensions = session.get_dimensions(page_index)
        if image is None or dimensions is None:
            return None
        return ReaderPageImage(page_index, image, dimensions)

    def load_pages(self, session: _FakeSession, page_indices: tuple[int, ...]) -> dict[int, ReaderPageImage]:
        loaded: dict[int, ReaderPageImage] = {}
        for page_index in page_indices:
            page = self.load_page(session, page_index)
            if page is not None:
                loaded[page_index] = page
        return loaded


class _FakeLibraryService:
    def __init__(self) -> None:
        self.progress_calls: list[tuple[str, int, float]] = []

    def set_progress(self, book_uuid: str, page_index: int, progress_percent: float) -> None:
        self.progress_calls.append((book_uuid, page_index, progress_percent))

    def save_reader_settings(self, _book_uuid, _settings):  # noqa: ANN001
        return None


def test_reader_viewmodel_uses_rtl_navigation_and_shifted_spreads(tmp_path: Path) -> None:
    viewmodel = _viewmodel(tmp_path)

    viewmodel.open_path(tmp_path / "book.cbz")
    viewmodel.set_viewport_size(1600, 900)

    assert viewmodel.layout_result is not None
    assert viewmodel.layout_result.mode == ReaderDisplayMode.DOUBLE
    assert viewmodel.current_display_indices == (0, 1)
    viewmodel.handle_horizontal_key("left")
    # Forward step: smallest displayed index = 2 (matches the new indicator).
    assert viewmodel.current_index == 2
    assert viewmodel.current_display_indices == (2, 3)
    viewmodel.handle_horizontal_key("right")
    # Backward step: layout anchor is `(1, 0)` but the indicator follows the
    # smallest displayed index = 0.
    assert viewmodel.current_index == 0
    assert viewmodel.current_display_indices == (1, 0)

    viewmodel.seek(2)
    viewmodel.toggle_spread_offset()

    assert viewmodel.current_index == 1
    assert viewmodel.current_display_indices == (1, 2)


def test_reader_viewmodel_waits_for_spread_before_layout(tmp_path: Path) -> None:
    viewmodel = _viewmodel(tmp_path)
    layout_modes: list[ReaderDisplayMode] = []
    viewmodel.layout_changed.connect(lambda result: layout_modes.append(result.mode))

    viewmodel.open_path(tmp_path / "book.cbz")
    viewmodel.set_viewport_size(1600, 900)

    assert layout_modes
    assert ReaderDisplayMode.SINGLE not in layout_modes
    assert layout_modes[-1] == ReaderDisplayMode.DOUBLE


def test_reader_viewmodel_clears_stale_layout_while_step_navigation_target_loads(tmp_path: Path) -> None:
    task_service = _ManualPageTaskService()
    viewmodel = _viewmodel(tmp_path, task_service=task_service, prefetch_before=0, prefetch_after=0)

    viewmodel.open_path(tmp_path / "book.cbz")
    viewmodel.set_viewport_size(1600, 900)
    task_service.run_next_page_task()

    assert viewmodel.layout_result is not None
    assert [draw.page_index for draw in viewmodel.layout_result.page_draws] == [1, 0]

    viewmodel.go_next()

    assert viewmodel.current_index == 2
    assert viewmodel.current_display_indices == (2, 3)
    assert viewmodel.layout_result is None
    assert viewmodel.loading_page_index == 2

    viewmodel.go_next()

    # Step navigation is ignored while the target spread is still unresolved.
    assert viewmodel.current_display_indices == (2, 3)
    assert viewmodel.loading_page_index == 2

    task_service.run_next_page_task()

    assert viewmodel.loading_page_index is None
    assert viewmodel.layout_result is not None
    assert [draw.page_index for draw in viewmodel.layout_result.page_draws] == [3, 2]


def test_reader_viewmodel_progress_seek_overrides_pending_step_navigation(tmp_path: Path) -> None:
    task_service = _ManualPageTaskService()
    viewmodel = _viewmodel(tmp_path, task_service=task_service, prefetch_before=0, prefetch_after=0)

    viewmodel.open_path(tmp_path / "book.cbz")
    viewmodel.set_viewport_size(1600, 900)
    task_service.run_next_page_task()
    viewmodel.go_next()

    assert viewmodel.loading_page_index == 2

    viewmodel.seek(4)

    assert viewmodel.current_display_indices == (4,)
    assert viewmodel.loading_page_index == 4

    task_service.run_next_page_task()
    assert viewmodel.loading_page_index == 4
    assert viewmodel.layout_result is None

    task_service.run_next_page_task()
    assert viewmodel.loading_page_index is None
    assert viewmodel.layout_result is not None
    assert [draw.page_index for draw in viewmodel.layout_result.page_draws] == [4]


def test_reader_viewmodel_backward_navigation_uses_smallest_as_current_index(tmp_path: Path) -> None:
    viewmodel = _viewmodel(tmp_path)

    viewmodel.open_path(tmp_path / "book.cbz")
    viewmodel.set_viewport_size(1600, 900)
    viewmodel.go_next()
    viewmodel.go_previous()

    # Backward step plants the layout anchor on the larger page (`1`) but the
    # user-facing indicator follows the smallest displayed index (`0`).
    assert viewmodel.current_index == 0
    assert viewmodel.current_display_indices == (1, 0)


def test_reader_viewmodel_does_not_skip_hidden_companion_in_single_mode(tmp_path: Path) -> None:
    viewmodel = _viewmodel(tmp_path)

    viewmodel.open_path(tmp_path / "book.cbz")
    viewmodel.set_viewport_size(700, 900)

    assert viewmodel.layout_result is not None
    assert viewmodel.layout_result.mode == ReaderDisplayMode.SINGLE
    assert viewmodel.current_display_indices == (0, 1)

    viewmodel.go_next()

    assert viewmodel.current_index == 1
    assert viewmodel.layout_result is not None
    assert [draw.page_index for draw in viewmodel.layout_result.page_draws] == [1]


def test_reader_viewmodel_previous_does_not_skip_hidden_companion_in_single_mode(tmp_path: Path) -> None:
    viewmodel = _viewmodel(tmp_path)

    viewmodel.open_path(tmp_path / "book.cbz")
    viewmodel.set_viewport_size(700, 900)
    viewmodel.seek(4)

    # Seeking to the last index is treated as an explicit "single page" intent:
    # only the last page is in the spread, indicator reads `N/N`.
    assert viewmodel.layout_result is not None
    assert viewmodel.layout_result.mode == ReaderDisplayMode.SINGLE
    assert viewmodel.current_display_indices == (4,)

    viewmodel.go_previous()

    # Going back from the single last-page view still steps by exactly one
    # archive index — it must not skip page 3 to land on page 2.
    assert viewmodel.current_index == 3
    assert viewmodel.layout_result is not None
    assert [draw.page_index for draw in viewmodel.layout_result.page_draws] == [3]


def test_reader_viewmodel_shift_button_advances_archive_index(tmp_path: Path) -> None:
    viewmodel = _viewmodel(tmp_path)

    viewmodel.open_path(tmp_path / "book.cbz")
    viewmodel.set_viewport_size(1600, 900)
    viewmodel.set_direction(viewmodel.settings.direction)

    viewmodel.shift_to_next_index()

    assert viewmodel.current_index == 1


def test_reader_viewmodel_emits_progress_after_persist(tmp_path: Path) -> None:
    library = _FakeLibraryService()
    progress_events: list[tuple[str, int, float]] = []
    viewmodel = _viewmodel(tmp_path, library_service=library)
    viewmodel.progress_changed.connect(lambda book_uuid, page, percent: progress_events.append((book_uuid, page, percent)))

    viewmodel.open_path(tmp_path / "book.cbz")
    viewmodel.set_viewport_size(1600, 900)
    viewmodel.seek(2)

    # The viewport is wide enough that the spread `(2, 3)` is rendered as
    # DOUBLE. The resume index is the smallest displayed page (2), while the
    # progress percent uses the largest displayed page (3 of 5 -> 75%).
    assert library.progress_calls[-1] == ("book-1", 2, 75.0)
    assert progress_events[-1] == ("book-1", 2, 75.0)


def test_reader_viewmodel_pans_wide_page_before_turning_page(tmp_path: Path) -> None:
    viewmodel = _viewmodel(tmp_path, dimensions=(2000, 1000))

    viewmodel.open_path(tmp_path / "wide.cbz")
    viewmodel.set_viewport_size(1000, 800)
    before_index = viewmodel.current_index
    before_pan = viewmodel.pan_x

    assert viewmodel.layout_result is not None
    assert viewmodel.layout_result.mode == ReaderDisplayMode.WIDE_PAN

    assert viewmodel.pan_x == viewmodel.layout_result.pan_min_x

    viewmodel.handle_horizontal_key("left")

    assert viewmodel.current_index == before_index
    assert viewmodel.pan_x > before_pan


def test_reader_viewmodel_ltr_wide_page_starts_left_and_pans_right(tmp_path: Path) -> None:
    viewmodel = _viewmodel(tmp_path, dimensions=(2000, 1000))

    viewmodel.set_direction(ReaderDirection.LEFT_TO_RIGHT)
    viewmodel.open_path(tmp_path / "wide.cbz")
    viewmodel.set_viewport_size(1000, 800)
    before_index = viewmodel.current_index
    before_pan = viewmodel.pan_x

    assert viewmodel.layout_result is not None
    assert viewmodel.layout_result.mode == ReaderDisplayMode.WIDE_PAN
    assert before_pan == viewmodel.layout_result.pan_max_x

    viewmodel.handle_horizontal_key("right")

    assert viewmodel.current_index == before_index
    assert viewmodel.pan_x < before_pan


def test_reader_viewmodel_wide_buttons_pan_before_navigation(tmp_path: Path) -> None:
    viewmodel = _viewmodel(tmp_path, dimensions=(2000, 1000))

    viewmodel.open_path(tmp_path / "wide.cbz")
    viewmodel.set_viewport_size(1000, 800)
    before_index = viewmodel.current_index
    before_pan = viewmodel.pan_x

    viewmodel.activate_left_side()

    assert viewmodel.current_index == before_index
    assert viewmodel.pan_x > before_pan


def test_reader_viewmodel_vertical_mode_scrolls_continuously_and_snaps_by_page(tmp_path: Path) -> None:
    viewmodel = _viewmodel(tmp_path)

    viewmodel.set_direction(ReaderDirection.TOP_TO_BOTTOM)
    viewmodel.open_path(tmp_path / "book.cbz")
    viewmodel.set_viewport_size(1000, 800)

    assert viewmodel.layout_result is not None
    assert [draw.page_index for draw in viewmodel.layout_result.page_draws] == [0, 1, 2]
    assert viewmodel.layout_result.page_draws[0].rect.height == 800

    assert viewmodel.handle_vertical_scroll(-801) is True
    assert viewmodel.current_index == 1
    assert viewmodel.layout_result is not None
    assert any(draw.page_index == 1 and draw.rect.height == 800 for draw in viewmodel.layout_result.page_draws)

    viewmodel.go_next()
    assert viewmodel.current_index == 2


def test_reader_viewmodel_vertical_custom_settings_do_not_change_direction(tmp_path: Path) -> None:
    viewmodel = _viewmodel(tmp_path)

    viewmodel.set_direction(ReaderDirection.LEFT_TO_RIGHT)
    viewmodel.set_vertical_custom_enabled(True)
    viewmodel.set_vertical_zoom_percent(500)

    assert viewmodel.settings.direction == ReaderDirection.LEFT_TO_RIGHT
    assert viewmodel.settings.vertical_custom_enabled is True
    assert viewmodel.settings.vertical_zoom_percent == 200


def test_reader_viewmodel_progress_uses_largest_for_percent_and_smallest_for_resume(
    tmp_path: Path,
) -> None:
    library = _FakeLibraryService()
    viewmodel = _viewmodel(tmp_path, library_service=library)

    viewmodel.open_path(tmp_path / "book.cbz")
    viewmodel.set_viewport_size(1600, 900)
    viewmodel.seek(2)

    # DOUBLE spread `(2, 3)` of 5 pages: resume index = smallest (2),
    # percent = largest (3) / (5 - 1) * 100 = 75%.
    assert library.progress_calls[-1] == ("book-1", 2, 75.0)

    # Narrow viewport forces SINGLE layout, so the displayed indices collapse
    # to one page and smallest == largest -> resume and progress agree.
    viewmodel.set_viewport_size(700, 900)
    viewmodel.seek(2)
    assert library.progress_calls[-1] == ("book-1", 2, 50.0)


def test_reader_viewmodel_shift_to_next_index_advances_smallest_after_backward(
    tmp_path: Path,
) -> None:
    viewmodel = _viewmodel(tmp_path)

    viewmodel.open_path(tmp_path / "book.cbz")
    viewmodel.set_viewport_size(1600, 900)
    viewmodel.go_next()
    viewmodel.go_previous()

    # After backward, layout draws are `[1, 0]`, smallest = 0. Shift must
    # advance the smallest archive index by one, landing on `(1, 2)` rather
    # than re-running `seek(primary + 1)` which would have produced `(2, 3)`.
    assert viewmodel.current_index == 0
    viewmodel.shift_to_next_index()
    assert viewmodel.current_index == 1
    assert viewmodel.current_display_indices == (1, 2)


def test_reader_viewmodel_jump_to_end_displays_last_page_alone(tmp_path: Path) -> None:
    library = _FakeLibraryService()
    viewmodel = _viewmodel(tmp_path, library_service=library)

    viewmodel.open_path(tmp_path / "book.cbz")
    viewmodel.set_viewport_size(1600, 900)
    viewmodel.jump_to_end()

    last_index = viewmodel.page_count - 1
    assert viewmodel.current_index == last_index
    assert viewmodel.current_display_indices == (last_index,)
    assert library.progress_calls[-1] == ("book-1", last_index, 100.0)


def test_reader_viewmodel_shift_to_next_index_collapses_to_single_at_last_page(
    tmp_path: Path,
) -> None:
    viewmodel = _viewmodel(tmp_path)

    viewmodel.open_path(tmp_path / "book.cbz")
    viewmodel.set_viewport_size(1600, 900)
    viewmodel.seek(3)

    # Spread `(3, 4)` with `page_count == 5`. Shifting once pushes the smallest
    # index to `4` (the last index); the spread collapses to a single-page
    # display so the indicator can read `5/5` unambiguously.
    assert viewmodel.current_display_indices == (3, 4)
    viewmodel.shift_to_next_index()
    assert viewmodel.current_index == viewmodel.page_count - 1
    assert viewmodel.current_display_indices == (viewmodel.page_count - 1,)


def test_reader_viewmodel_reports_password_cancel_as_undecrypted_archive(tmp_path: Path) -> None:
    viewmodel = _viewmodel(tmp_path)
    errors: list[str | None] = []
    viewmodel.error_changed.connect(errors.append)

    viewmodel.cancel_password_request()

    assert errors == ["Could not load images because the archive is encrypted and no password was provided."]
    assert viewmodel.error_message == errors[0]
    assert viewmodel.is_loading is False


def _cache_service(tmp_path: Path) -> CacheService:
    pool = ArchiveExtractionPool(tmp_path / "pool", max_bytes=4 * 1024 * 1024)
    return CacheService(archive_extraction_pool=pool, reader_page_cache_max_bytes=4 * 1024 * 1024)


def _viewmodel(
    tmp_path: Path,
    dimensions: tuple[int, int] = (600, 900),
    library_service=None,  # noqa: ANN001
    *,
    cache_service: CacheService | None = None,
    task_service=None,  # noqa: ANN001
    prefetch_before: int = 1,
    prefetch_after: int = 1,
) -> ReaderViewModel:
    source = tmp_path / "book.cbz"
    source.write_bytes(b"fake")
    cache = cache_service or _cache_service(tmp_path)
    return ReaderViewModel(
        _FakeSessionService(dimensions),  # type: ignore[arg-type]
        task_service or _SyncTaskService(),  # type: ignore[arg-type]
        cache.issue_reader_namespace(),
        library_service,  # type: ignore[arg-type]
        book_uuid="book-1" if library_service is not None else None,
        title="Book",
        prefetch_before=prefetch_before,
        prefetch_after=prefetch_after,
    )


def test_reader_viewmodel_cancel_clears_only_its_namespace_in_shared_cache(tmp_path: Path) -> None:
    cache_service = _cache_service(tmp_path)
    vm_a = _viewmodel(tmp_path, cache_service=cache_service)
    vm_b = _viewmodel(tmp_path, cache_service=cache_service)

    vm_a.open_path(tmp_path / "a.cbz")
    vm_b.open_path(tmp_path / "b.cbz")
    vm_a.set_viewport_size(1600, 900)
    vm_b.set_viewport_size(1600, 900)
    bytes_before = cache_service.reader_page_cache.current_bytes
    assert bytes_before > 0

    vm_a.cancel()

    # Reader B's pages survive — only A's slice of the shared cache is freed.
    bytes_after = cache_service.reader_page_cache.current_bytes
    assert bytes_after > 0
    assert bytes_after < bytes_before
    assert vm_a._session is None
    assert vm_a._pages == {}
    assert vm_a._layout_result is None
    assert vm_a.page_count == 0


def test_reader_viewmodel_multi_open_respects_shared_byte_budget(tmp_path: Path) -> None:
    # A tight shared budget guarantees that opening two readers can never
    # multiply memory: the cache evicts oldest pages from idle readers first.
    pool = ArchiveExtractionPool(tmp_path / "pool", max_bytes=1024)
    cache_service = CacheService(archive_extraction_pool=pool, reader_page_cache_max_bytes=2048)
    vm_a = _viewmodel(tmp_path, cache_service=cache_service)
    vm_b = _viewmodel(tmp_path, cache_service=cache_service)

    vm_a.open_path(tmp_path / "a.cbz")
    vm_a.set_viewport_size(1600, 900)
    vm_b.open_path(tmp_path / "b.cbz")
    vm_b.set_viewport_size(1600, 900)

    # Walk through both books to load several pages and exercise eviction.
    for _ in range(3):
        vm_a.go_next()
        vm_b.go_next()

    assert cache_service.reader_page_cache.current_bytes <= 2048


def test_reader_viewmodel_ignores_late_page_results_after_cancel(tmp_path: Path) -> None:
    task_service = _ManualPageTaskService()
    vm = _viewmodel(tmp_path, task_service=task_service)
    ready: list[ReaderPageImage] = []
    vm.page_ready.connect(ready.append)

    vm.open_path(tmp_path / "book.cbz")
    vm.set_viewport_size(700, 900)
    assert task_service.page_tasks
    _handle, _callback, on_success, _on_failure = task_service.page_tasks[0]

    vm.cancel()
    assert on_success is not None
    on_success({0: ReaderPageImage(0, _png_bytes(), (8, 12))})

    assert ready == []
    assert vm._pages == {}
    assert vm._page_handles == {}


def test_reader_viewmodel_keeps_prefetch_pages_out_of_resident_pages(tmp_path: Path) -> None:
    cache_service = _cache_service(tmp_path)
    vm = _viewmodel(
        tmp_path,
        cache_service=cache_service,
        prefetch_before=0,
        prefetch_after=4,
    )
    vm.set_direction(ReaderDirection.LEFT_TO_RIGHT)
    vm.open_path(tmp_path / "book.cbz")
    vm.set_viewport_size(700, 900)

    for _ in range(3):
        vm.go_next()

    resident = set(vm._pages)
    assert resident
    assert resident <= vm._resident_page_indices()
    assert len(resident) <= len(vm.current_display_indices)
    cached = [index for index in range(vm.page_count) if vm._page_cache.get(index) is not None]
    assert len(cached) > len(resident)


def test_reader_viewmodel_prefetch_window_uses_configured_after_count(tmp_path: Path) -> None:
    cache_service_low = _cache_service(tmp_path)
    vm_low = _viewmodel(
        tmp_path,
        cache_service=cache_service_low,
        prefetch_before=0,
        prefetch_after=1,
    )
    vm_low.set_direction(ReaderDirection.LEFT_TO_RIGHT)
    vm_low.open_path(tmp_path / "low.cbz")
    vm_low.set_viewport_size(700, 900)

    cache_service_high = _cache_service(tmp_path)
    vm_high = _viewmodel(
        tmp_path,
        cache_service=cache_service_high,
        prefetch_before=0,
        prefetch_after=3,
    )
    vm_high.set_direction(ReaderDirection.LEFT_TO_RIGHT)
    vm_high.open_path(tmp_path / "high.cbz")
    vm_high.set_viewport_size(700, 900)

    # AppConfig.page_prefetch_after must translate into how many pages the
    # reader has on hand ahead of the current spread. With the same starting
    # state, a higher ``after`` value must keep strictly more pages cached.
    low_namespace = vm_low._page_cache
    high_namespace = vm_high._page_cache
    low_cached = [i for i in range(vm_low.page_count) if low_namespace.get(i) is not None]
    high_cached = [i for i in range(vm_high.page_count) if high_namespace.get(i) is not None]
    assert len(high_cached) > len(low_cached)
    # And in LTR the prefetched pages live ahead of the current index.
    assert all(index >= vm_high.current_index for index in high_cached)


def test_reader_viewmodel_rtl_swaps_prefetch_window(tmp_path: Path) -> None:
    cache_service = _cache_service(tmp_path)
    vm = _viewmodel(
        tmp_path,
        cache_service=cache_service,
        prefetch_before=0,
        prefetch_after=3,
    )
    # Default direction is RTL but be explicit so the intent is obvious.
    vm.set_direction(ReaderDirection.RIGHT_TO_LEFT)
    vm.open_path(tmp_path / "book.cbz")
    vm.set_viewport_size(700, 900)
    vm.seek(3)

    namespace = vm._page_cache
    # RTL swaps the window so the user-facing "forward" prefetch (decreasing
    # archive indices) is covered by the configured ``after`` count. With
    # prefetch_after=3 the cache should hold pages 0..3 (page 3 from the
    # spread, pages 0..2 from the prefetch).
    for index in range(0, 4):
        assert namespace.get(index) is not None, f"page {index} should be prefetched"


def _png_bytes() -> bytes:
    from io import BytesIO

    output = BytesIO()
    Image.new("RGB", (8, 12), "#336699").save(output, format="PNG")
    return output.getvalue()
