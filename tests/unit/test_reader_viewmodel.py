from __future__ import annotations

from pathlib import Path

from PIL import Image

from joyread.core.reader import ReaderDirection, ReaderDisplayMode, ReaderPageImage
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

    def open_archive(self, _path: Path, password=None):  # noqa: ANN001
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
    assert viewmodel.current_index == 2
    assert viewmodel.current_display_indices == (2, 3)
    viewmodel.handle_horizontal_key("right")
    assert viewmodel.current_index == 1
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


def test_reader_viewmodel_backward_navigation_uses_previous_page_as_primary(tmp_path: Path) -> None:
    viewmodel = _viewmodel(tmp_path)

    viewmodel.open_path(tmp_path / "book.cbz")
    viewmodel.set_viewport_size(1600, 900)
    viewmodel.go_next()
    viewmodel.go_previous()

    assert viewmodel.current_index == 1
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

    assert viewmodel.layout_result is not None
    assert viewmodel.layout_result.mode == ReaderDisplayMode.SINGLE
    assert viewmodel.current_display_indices == (4, 3)

    viewmodel.go_previous()

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
    viewmodel.seek(2)

    assert library.progress_calls[-1] == ("book-1", 2, 50.0)
    assert progress_events[-1] == ("book-1", 2, 50.0)


def test_reader_viewmodel_pans_wide_page_before_turning_page(tmp_path: Path) -> None:
    viewmodel = _viewmodel(tmp_path, dimensions=(2000, 1000))

    viewmodel.open_path(tmp_path / "wide.cbz")
    viewmodel.set_viewport_size(1000, 800)
    before_index = viewmodel.current_index
    before_pan = viewmodel.pan_x

    assert viewmodel.layout_result is not None
    assert viewmodel.layout_result.mode == ReaderDisplayMode.WIDE_PAN

    viewmodel.handle_horizontal_key("left")

    assert viewmodel.current_index == before_index
    assert viewmodel.pan_x < before_pan


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


def _viewmodel(
    tmp_path: Path,
    dimensions: tuple[int, int] = (600, 900),
    library_service=None,  # noqa: ANN001
) -> ReaderViewModel:
    source = tmp_path / "book.cbz"
    source.write_bytes(b"fake")
    return ReaderViewModel(
        _FakeSessionService(dimensions),  # type: ignore[arg-type]
        _SyncTaskService(),  # type: ignore[arg-type]
        CacheService(16, 16),
        library_service,  # type: ignore[arg-type]
        book_uuid="book-1" if library_service is not None else None,
        title="Book",
    )


def _png_bytes() -> bytes:
    from io import BytesIO

    output = BytesIO()
    Image.new("RGB", (8, 12), "#336699").save(output, format="PNG")
    return output.getvalue()
