"""Temporary typed accessors while callers migrate to Reader/LibraryRuntime."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from joyread.app.archive_warmup_coordinator import ArchiveWarmupCoordinator
    from joyread.core.archive import ArchiveImageService
    from joyread.core.reader import ReaderSessionService
    from joyread.core.repositories.book_repository import BookRepository
    from joyread.core.repositories.tag_repository import TagRepository
    from joyread.core.services.archive_extraction_pool import ArchiveExtractionCache
    from joyread.core.services.cache_service import CacheService
    from joyread.core.services.export_service import ExportService
    from joyread.core.services.hash_service import HashService
    from joyread.core.services.hidden_space_service import HiddenSpaceService
    from joyread.core.services.import_service import ImportService
    from joyread.core.services.library_maintenance_service import LibraryMaintenanceCoordinator, LibraryMaintenanceService
    from joyread.core.services.library_service import LibraryService
    from joyread.core.services.path_issue_service import PathIssueService
    from joyread.core.services.tag_service import TagService
    from joyread.core.services.thumbnail_service import ThumbnailService
    from joyread.infrastructure.database import DatabaseInterpreter
    from joyread.infrastructure.pdf_image_service import PdfImageService
    from joyread.infrastructure.qt_task_service import TaskService
    from joyread.infrastructure.resources.resource_loader import ResourceLoader
    from joyread.infrastructure.thumbnail_renderer import QtThumbnailRenderer
    from joyread.ui.viewmodels.main_window_viewmodel import MainWindowViewModel
    from joyread.ui.viewmodels.path_issue_viewmodel import PathIssueViewModel
    from joyread.ui.viewmodels.settings_viewmodel import SettingsViewModel
    from joyread.ui.viewmodels.shelf_viewmodel import ShelfViewModel
    from joyread.ui.viewmodels.tag_management_viewmodel import TagManagementViewModel


class _OwnedField:
    """Forward a legacy AppContext attribute to its sole runtime owner."""

    def __init__(self, owner: str, name: str) -> None:
        self.owner = owner
        self.name = name

    def __get__(self, instance: Any, _owner: type | None = None) -> Any:
        if instance is None:
            return self
        return getattr(getattr(instance, self.owner), self.name)

    def __set__(self, instance: Any, value: Any) -> None:
        setattr(getattr(instance, self.owner), self.name, value)


class RuntimeAccess:
    """Compatibility surface; new code should use ``reader_runtime``/``library_runtime``."""

    resources: ResourceLoader = _OwnedField("reader_runtime", "resources")  # type: ignore[assignment]
    path_issue_service: PathIssueService = _OwnedField("reader_runtime", "path_issue_service")  # type: ignore[assignment]
    path_issue_viewmodel: PathIssueViewModel = _OwnedField("reader_runtime", "path_issue_viewmodel")  # type: ignore[assignment]
    archive_extraction_pool: ArchiveExtractionCache = _OwnedField("reader_runtime", "archive_extraction_pool")  # type: ignore[assignment]
    archive_image_service: ArchiveImageService = _OwnedField("reader_runtime", "archive_image_service")  # type: ignore[assignment]
    reader_session_service: ReaderSessionService = _OwnedField("reader_runtime", "reader_session_service")  # type: ignore[assignment]
    pdf_image_service: PdfImageService = _OwnedField("reader_runtime", "pdf_image_service")  # type: ignore[assignment]
    task_service: TaskService = _OwnedField("reader_runtime", "task_service")  # type: ignore[assignment]
    hash_service: HashService = _OwnedField("reader_runtime", "hash_service")  # type: ignore[assignment]
    archive_warmup_coordinator: ArchiveWarmupCoordinator = _OwnedField("reader_runtime", "archive_warmup_coordinator")  # type: ignore[assignment]
    thumbnail_renderer: QtThumbnailRenderer = _OwnedField("reader_runtime", "thumbnail_renderer")  # type: ignore[assignment]
    database_interpreter: DatabaseInterpreter = _OwnedField("library_runtime", "database_interpreter")  # type: ignore[assignment]
    book_repository: BookRepository = _OwnedField("library_runtime", "book_repository")  # type: ignore[assignment]
    tag_repository: TagRepository = _OwnedField("library_runtime", "tag_repository")  # type: ignore[assignment]
    library_service: LibraryService = _OwnedField("library_runtime", "library_service")  # type: ignore[assignment]
    tag_service: TagService = _OwnedField("library_runtime", "tag_service")  # type: ignore[assignment]
    import_service: ImportService = _OwnedField("library_runtime", "import_service")  # type: ignore[assignment]
    export_service: ExportService = _OwnedField("library_runtime", "export_service")  # type: ignore[assignment]
    thumbnail_service: ThumbnailService = _OwnedField("library_runtime", "thumbnail_service")  # type: ignore[assignment]
    library_maintenance_coordinator: LibraryMaintenanceCoordinator = _OwnedField("library_runtime", "library_maintenance_coordinator")  # type: ignore[assignment]
    library_maintenance_service: LibraryMaintenanceService = _OwnedField("library_runtime", "library_maintenance_service")  # type: ignore[assignment]
    hidden_space_service: HiddenSpaceService = _OwnedField("library_runtime", "hidden_space_service")  # type: ignore[assignment]
    shelf_viewmodel: ShelfViewModel = _OwnedField("library_runtime", "shelf_viewmodel")  # type: ignore[assignment]
    tag_management_viewmodel: TagManagementViewModel = _OwnedField("library_runtime", "tag_management_viewmodel")  # type: ignore[assignment]
    settings_viewmodel: SettingsViewModel = _OwnedField("library_runtime", "settings_viewmodel")  # type: ignore[assignment]
    main_window_viewmodel: MainWindowViewModel = _OwnedField("library_runtime", "main_window_viewmodel")  # type: ignore[assignment]
    cache_service: CacheService = _OwnedField("library_runtime", "cache_service")  # type: ignore[assignment]
