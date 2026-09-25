"""Reader-owned services, constructible without Library repositories or SQLite.

The factory has no dependency on AppContext. External Readers share its
application services across Library loads and storage transitions.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from joyread.app.archive_warmup_coordinator import ArchiveWarmupCoordinator
from joyread.app.event_hook import EventHook
from joyread.core.archive import ArchiveImageService, ArchiveOpenLimits
from joyread.core.archive.limits import GIB, MEGAPIXEL
from joyread.core.models.cache import ArchiveCacheStrategy, normalize_archive_cache_strategy
from joyread.core.models.reader_prefetch import (
    PREFETCH_AFTER_DEFAULT,
    PREFETCH_AFTER_MAX,
    PREFETCH_BEFORE_DEFAULT,
    PREFETCH_BEFORE_MAX,
    prefetch_count,
)
from joyread.core.reader import ReaderSessionService
from joyread.core.services.archive_extraction_pool import (
    ArchiveExtractionCache,
    ArchiveExtractionPool,
    HiddenImageExtractionPool,
    managed_document_cache_key,
)
from joyread.core.services.cache_service import ReaderCacheService
from joyread.core.services.hash_service import HashService
from joyread.core.services.path_issue_service import PathIssueService
from joyread.infrastructure.config.app_config import AppConfig
from joyread.infrastructure.config.settings_store import AppSettings, SettingsStore
from joyread.infrastructure.filesystem.path_service import PathService, WritableLocation
from joyread.infrastructure.filesystem.windows_long_paths import WindowsLongPathCapability
from joyread.infrastructure.pdf_image_service import PdfImageService
from joyread.infrastructure.pdf_document_thread import shutdown_pdf_thread
from joyread.infrastructure.qt_task_service import TaskScope, TaskService
from joyread.infrastructure.reader_image_decoder import qimage_frame_bytes
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.infrastructure.thumbnail_renderer import QtThumbnailRenderer
from joyread.ui.viewmodels.path_issue_viewmodel import PathIssueViewModel


class ReaderPreferences:
    """Live reader settings without retaining the full Settings ViewModel."""

    def __init__(self, settings: AppSettings, store: SettingsStore | None = None) -> None:
        self._settings = settings
        self._store = store
        self.prefetch_changed: EventHook[None] = EventHook()
        self.window_sizes_reset: EventHook[None] = EventHook()

    @property
    def settings(self) -> AppSettings:
        return self._settings

    @property
    def page_prefetch_before(self) -> int:
        return prefetch_count(
            self._settings.page_prefetch_before,
            default=PREFETCH_BEFORE_DEFAULT,
            maximum=PREFETCH_BEFORE_MAX,
        )

    @property
    def page_prefetch_after(self) -> int:
        return prefetch_count(
            self._settings.page_prefetch_after,
            default=PREFETCH_AFTER_DEFAULT,
            maximum=PREFETCH_AFTER_MAX,
        )

    @property
    def archive_open_limits(self) -> ArchiveOpenLimits:
        return archive_open_limits_from_settings(self._settings)

    def refresh(self) -> AppSettings:
        if self._store is not None:
            self.apply(self._store.load())
        return self._settings

    def apply(self, settings: AppSettings) -> None:
        old_window = (self.page_prefetch_before, self.page_prefetch_after)
        self._settings = settings
        if old_window != (self.page_prefetch_before, self.page_prefetch_after):
            self.prefetch_changed.emit()

    def window_size(self, kind: str) -> tuple[int, int] | None:
        self.refresh()
        return (self._settings.library_window_size if kind == "library"
                else self._settings.reader_window_size)

    def remember_window_size(self, kind: str, size: tuple[int, int]) -> None:
        field = "library_window_size" if kind == "library" else "reader_window_size"
        settings = (
            self._store.update(**{field: size})
            if self._store is not None else replace(self._settings, **{field: size})
        )
        self.apply(settings)

    def notify_window_sizes_reset(self) -> None:
        self.refresh()
        self.window_sizes_reset.emit()

    def set_page_prefetch_before(self, value: int) -> None:
        self._set_prefetch("page_prefetch_before", value, PREFETCH_BEFORE_DEFAULT, PREFETCH_BEFORE_MAX)

    def set_page_prefetch_after(self, value: int) -> None:
        self._set_prefetch("page_prefetch_after", value, PREFETCH_AFTER_DEFAULT, PREFETCH_AFTER_MAX)

    def _set_prefetch(self, field: str, value: int, default: int, maximum: int) -> None:
        normalized = prefetch_count(value, default=default, maximum=maximum)
        if normalized == getattr(self._settings, field):
            return
        settings = (
            self._store.update(**{field: normalized})
            if self._store is not None
            else replace(self._settings, **{field: normalized})
        )
        self.apply(settings)


@dataclass
class ReaderRuntime:
    """Reader dependencies with no database or Library ViewModel fields."""

    settings_store: SettingsStore
    preferences: ReaderPreferences
    paths: PathService
    resources: ResourceLoader
    path_issue_service: PathIssueService
    path_issue_viewmodel: PathIssueViewModel
    task_service: TaskService
    library_task_service: TaskScope
    archive_extraction_pool: ArchiveExtractionCache
    archive_image_service: ArchiveImageService
    pdf_image_service: PdfImageService
    reader_session_service: ReaderSessionService
    cache_service: ReaderCacheService
    hash_service: HashService
    archive_warmup_coordinator: ArchiveWarmupCoordinator
    thumbnail_renderer: QtThumbnailRenderer

    def reload_settings(self) -> AppSettings:
        return self.preferences.refresh()

    def managed_cache_key(self, file_id: str) -> str:
        return managed_document_cache_key(file_id, self.paths.storage_root)

    def uses_library_storage(self, source_path: Path, *, managed: bool) -> bool:
        """A managed port or a file under the Library participates in its drain."""

        return managed or source_path.resolve().is_relative_to(self.paths.storage_root)

    def close(self) -> None:
        """Release a standalone Reader runtime in producer-to-engine order."""

        try:
            self.archive_warmup_coordinator.close()
        finally:
            try:
                self.task_service.shutdown()
            finally:
                shutdown_pdf_thread()


def create_reader_runtime(
    config: AppConfig,
    settings: AppSettings,
    settings_store: SettingsStore,
    *,
    paths: PathService | None = None,
    resources: ResourceLoader | None = None,
    path_issue_service: PathIssueService | None = None,
) -> ReaderRuntime:
    """Build Reader services without constructing a Library database.

    Only settings and the application cache are needed for an external file.
    The selected Library path is recorded but never created by this factory.
    """

    paths = paths or PathService(
        config.app_name,
        config.app_author,
        storage_root=Path(settings.storage_location),
        support_root=settings_store.support_root,
        cache_root=settings_store.cache_root,
    )
    resources = resources or ResourceLoader()
    path_issue_service = path_issue_service or PathIssueService(WindowsLongPathCapability())
    archive_pool = create_archive_extraction_cache(paths, settings, path_issue_service)
    archive_service = ArchiveImageService(
        extraction_pool=archive_pool, session_temp_root=paths.session_temp_root
    )
    pdf_service = PdfImageService()
    session_service = ReaderSessionService(
        archive_service, pdf_service, path_issue_service=path_issue_service
    )
    task_service = TaskService(config.max_background_workers)
    try:
        cache_service = ReaderCacheService(
            archive_extraction_pool=archive_pool,
            reader_page_cache_max_bytes=settings.reader_page_cache_mb * 1024 * 1024,
            thumbnail_cache_max_bytes=settings.thumbnail_cache_mb * 1024 * 1024,
            reader_frame_sizer=qimage_frame_bytes,
        )
        return ReaderRuntime(
            settings_store=settings_store,
            preferences=ReaderPreferences(settings, settings_store),
            paths=paths,
            resources=resources,
            path_issue_service=path_issue_service,
            path_issue_viewmodel=PathIssueViewModel(),
            task_service=task_service,
            library_task_service=task_service.create_scope(),
            archive_extraction_pool=archive_pool,
            archive_image_service=archive_service,
            pdf_image_service=pdf_service,
            reader_session_service=session_service,
            cache_service=cache_service,
            hash_service=HashService(),
            archive_warmup_coordinator=ArchiveWarmupCoordinator(session_service, task_service),
            thumbnail_renderer=QtThumbnailRenderer(),
        )
    except BaseException:
        try:
            task_service.shutdown()
        finally:
            shutdown_pdf_thread()
        raise


def create_archive_extraction_cache(
    paths: PathService, settings: AppSettings, path_issue_service: PathIssueService
) -> ArchiveExtractionCache:
    strategy = normalize_archive_cache_strategy(settings.archive_cache_strategy)
    max_bytes = settings.archive_extraction_pool_gb * 1024 * 1024 * 1024
    if strategy is ArchiveCacheStrategy.HIDDEN_IMAGE_FILES:
        return HiddenImageExtractionPool(
            paths.resolve(WritableLocation.CACHE, ".archive_image_pages"),
            max_bytes=max_bytes,
            path_issue_service=path_issue_service,
        )
    return ArchiveExtractionPool(
        paths.resolve(WritableLocation.CACHE, ".archive_zip_bundles"),
        max_bytes=max_bytes,
        path_issue_service=path_issue_service,
    )


def archive_open_limits_from_settings(settings: AppSettings) -> ArchiveOpenLimits:
    guardrails_enabled = bool(settings.archive_resource_guardrails_enabled)

    def resource_limit(value: int, multiplier: int) -> int | None:
        if not guardrails_enabled or int(value) == -1:
            return None
        return int(value) * multiplier

    return ArchiveOpenLimits(
        nested_archive_max_depth=(
            None if settings.nested_archive_max_depth == -1 else settings.nested_archive_max_depth
        ),
        global_file_max_depth=(
            None if settings.archive_global_file_max_depth == -1 else settings.archive_global_file_max_depth
        ),
        max_source_bytes=(
            settings.archive_max_source_size_gb * GIB
            if settings.archive_max_source_size_enabled else None
        ),
        max_extracted_item_bytes=resource_limit(settings.archive_max_extracted_item_gb, GIB),
        max_operation_bytes=resource_limit(settings.archive_max_operation_data_gb, GIB),
        max_image_pixels=resource_limit(settings.archive_max_image_megapixels, MEGAPIXEL),
        external_command_timeout_seconds=resource_limit(
            settings.archive_external_command_timeout_seconds, 1
        ),
    )
