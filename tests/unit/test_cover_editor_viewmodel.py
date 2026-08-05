from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from joyread.app.cover_editor import PreparedCoverSource
from joyread.app.tasking import TaskHandle, TaskStatus
from joyread.core.services.cache_service import SharedThumbnailCache
from joyread.core.services.thumbnail_service import CoverCropState
from joyread.ui.viewmodels.cover_editor_viewmodel import CoverEditorThumbnailViewModel
from tests.support.in_memory_book_repository import InMemoryBookRepository


class _SyncTaskService:
    def submit(
        self,
        name,
        callback,
        *,
        on_success=None,
        on_failure=None,
        on_discard=None,
        priority=0,
    ):  # noqa: ANN001
        del on_discard, priority
        handle = TaskHandle(task_id=name, status=TaskStatus.RUNNING)
        try:
            result = callback()
        except Exception as exc:  # pragma: no cover - helper failure path.
            handle.status = TaskStatus.FAILED
            if on_failure is not None:
                on_failure(exc)
            return handle
        handle.status = TaskStatus.COMPLETED
        handle.result = result
        if on_success is not None:
            on_success(result)
        return handle


class _PreviewRenderer:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, tuple[int, int], str]] = []

    def prepare_preview(self, image_bytes, size, source_token):  # noqa: ANN001
        self.calls.append((image_bytes, size, source_token))
        return PreparedCoverSource(source_token, "bounded-frame", (2400, 3600))


class _ThumbnailService:
    def __init__(self, target_path: Path) -> None:
        self.target_path = target_path
        self.source_bytes = b"full-quality-source"
        self.saved: list[tuple[object, bytes, CoverCropState, tuple[int, int]]] = []
        self.cache = SharedThumbnailCache(1024 * 1024)

    def issue_thumbnail_cache_client(self, client_id: str):  # noqa: ANN201
        return self.cache.issue_client(client_id)

    def open_thumbnail_source(self, _book):  # noqa: ANN001
        service = self

        class _Source:
            source_id = "file:test"
            page_count = 1

            @staticmethod
            def preferred_batch_size(_page_index: int) -> int:
                return 1

            @staticmethod
            def read_page(_page_index: int):  # noqa: ANN205
                return SimpleNamespace(image_bytes=service.source_bytes)

            @staticmethod
            def close() -> None:
                return None

        return _Source()

    def load_cover_source_page(self, _book, _page_index: int) -> bytes:  # noqa: ANN001
        return self.source_bytes

    def save_edited_cover(self, book, source_bytes, crop_state, size):  # noqa: ANN001
        self.saved.append((book, source_bytes, crop_state, size))
        return self.target_path


def test_cover_editor_viewmodel_retains_original_bytes_while_view_gets_prepared_frame(
    tmp_path: Path,
) -> None:
    target = tmp_path / "cover.png"
    service = _ThumbnailService(target)
    renderer = _PreviewRenderer()
    viewmodel = CoverEditorThumbnailViewModel(
        service,  # type: ignore[arg-type]
        _SyncTaskService(),  # type: ignore[arg-type]
        renderer,  # type: ignore[arg-type]
    )
    book = InMemoryBookRepository().list_books()[0]
    previews: list[PreparedCoverSource[object]] = []
    saved: list[tuple[str, Path]] = []
    viewmodel.preview_ready.connect(
        lambda _book_uuid, prepared, _opening: previews.append(prepared)
    )
    viewmodel.cover_saved.connect(lambda book_uuid, path: saved.append((book_uuid, path)))

    viewmodel.set_book(book, (100, 142))
    viewmodel.load_page_source(0, (360, 310), opening=True)

    assert renderer.calls == [(service.source_bytes, (360, 310), "page:1")]
    assert previews[0].frame == "bounded-frame"
    crop = CoverCropState("page:1", 100, 0.25, -0.5, (170, 241))
    viewmodel.save_cover(crop, (170, 241))

    assert service.saved[0][1] == service.source_bytes
    assert service.saved[0][2] == crop
    assert saved == [(book.uuid, target)]


def test_cover_editor_viewmodel_reads_imported_source_on_worker_task(tmp_path: Path) -> None:
    image_path = tmp_path / "cover-source.png"
    image_path.write_bytes(b"imported-original")
    service = _ThumbnailService(tmp_path / "cover.png")
    renderer = _PreviewRenderer()
    viewmodel = CoverEditorThumbnailViewModel(
        service,  # type: ignore[arg-type]
        _SyncTaskService(),  # type: ignore[arg-type]
        renderer,  # type: ignore[arg-type]
    )
    viewmodel.set_book(InMemoryBookRepository().list_books()[0], (100, 142))

    viewmodel.load_import_source(image_path, (720, 620))

    assert renderer.calls == [(b"imported-original", (720, 620), "import:cover-source.png")]
