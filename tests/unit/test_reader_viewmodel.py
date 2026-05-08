from __future__ import annotations

from pathlib import Path

from PIL import Image

from joyread.core.reader import ReaderDisplayMode, ReaderPageImage
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


def test_reader_viewmodel_uses_rtl_navigation_and_shifted_spreads(tmp_path: Path) -> None:
    viewmodel = _viewmodel(tmp_path)

    viewmodel.open_path(tmp_path / "book.cbz")
    viewmodel.set_viewport_size(1600, 900)

    assert viewmodel.layout_result is not None
    assert viewmodel.layout_result.mode == ReaderDisplayMode.DOUBLE
    viewmodel.handle_horizontal_key("left")
    assert viewmodel.current_index == 2
    viewmodel.handle_horizontal_key("right")
    assert viewmodel.current_index == 0

    viewmodel.seek(2)
    viewmodel.toggle_spread_offset()

    assert viewmodel.current_index == 1
    assert viewmodel.current_display_indices == (1, 2)


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


def _viewmodel(tmp_path: Path, dimensions: tuple[int, int] = (600, 900)) -> ReaderViewModel:
    source = tmp_path / "book.cbz"
    source.write_bytes(b"fake")
    return ReaderViewModel(
        _FakeSessionService(dimensions),  # type: ignore[arg-type]
        _SyncTaskService(),  # type: ignore[arg-type]
        CacheService(16, 16),
        title="Book",
    )


def _png_bytes() -> bytes:
    from io import BytesIO

    output = BytesIO()
    Image.new("RGB", (8, 12), "#336699").save(output, format="PNG")
    return output.getvalue()
