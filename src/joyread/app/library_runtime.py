"""Library-owned services and ViewModels, built against a ready Reader runtime."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from joyread.app.reader_runtime import ReaderRuntime, archive_open_limits_from_settings
from joyread.core.models.import_policy import normalize_canonical_import_policy
from joyread.core.repositories.book_repository import BookRepository
from joyread.core.repositories.sqlite_book_repository import SqliteBookRepository
from joyread.core.repositories.sqlite_tag_repository import SqliteTagRepository
from joyread.core.repositories.tag_repository import TagRepository
from joyread.core.services.export_service import ExportService
from joyread.core.services.cache_service import CacheService, LibraryCacheService
from joyread.core.services.hidden_space_service import HiddenSpaceService
from joyread.core.services.import_service import ImportService
from joyread.core.services.library_maintenance_service import (
    LibraryMaintenanceCoordinator,
    LibraryMaintenanceService,
)
from joyread.core.services.library_service import LibraryService
from joyread.core.services.tag_service import TagService
from joyread.core.services.thumbnail_service import ThumbnailService
from joyread.infrastructure.config.app_config import AppConfig
from joyread.infrastructure.config.settings_store import AppSettings, SettingsStore
from joyread.infrastructure.database import DatabaseInterpreter, DatabasePriority, apply_migrations
from joyread.infrastructure.filesystem.path_service import PathService
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.main_window_viewmodel import MainWindowViewModel
from joyread.ui.viewmodels.settings_viewmodel import SettingsViewModel
from joyread.ui.viewmodels.shelf_viewmodel import ShelfViewModel
from joyread.ui.viewmodels.tag_management_viewmodel import TagManagementViewModel


logger = logging.getLogger(__name__)


@dataclass
class LibraryRuntime:
    """Everything that requires the Library database or managed thumbnails."""

    paths: PathService
    database_interpreter: DatabaseInterpreter
    book_repository: BookRepository
    tag_repository: TagRepository
    library_service: LibraryService
    tag_service: TagService
    import_service: ImportService
    export_service: ExportService
    thumbnail_service: ThumbnailService
    cache_service: CacheService
    library_caches: LibraryCacheService
    library_maintenance_coordinator: LibraryMaintenanceCoordinator
    library_maintenance_service: LibraryMaintenanceService
    hidden_space_service: HiddenSpaceService
    shelf_viewmodel: ShelfViewModel
    tag_management_viewmodel: TagManagementViewModel
    settings_viewmodel: SettingsViewModel
    main_window_viewmodel: MainWindowViewModel
    maintenance_recovery_conflicts: bool = False

    def close_frontend(self) -> None:
        self.thumbnail_service.close()

    def close_database(self) -> None:
        self.database_interpreter.close()

    def close(self) -> None:
        """Release Library-owned frontend work before its database."""

        try:
            self.close_frontend()
        finally:
            self.close_database()


def create_library_runtime(
    reader: ReaderRuntime,
    config: AppConfig,
    settings: AppSettings,
    settings_store: SettingsStore,
    *,
    settings_viewmodel: SettingsViewModel | None = None,
) -> LibraryRuntime:
    """Create a complete Library or release every partially built resource."""

    paths = reader.paths
    database: DatabaseInterpreter | None = None
    thumbnails: ThumbnailService | None = None
    try:
        database = DatabaseInterpreter(paths.paths.database / "joyread.sqlite3")
        database.execute(apply_migrations, DatabasePriority.CRITICAL)
        book_repository: BookRepository = SqliteBookRepository(
            database,
            resolver=paths.resolver,
            managed_books_root=paths.paths.books,
            thumbnails_root=paths.paths.thumbnails,
        )
        tag_repository: TagRepository = SqliteTagRepository(database)
        tag_service = TagService(tag_repository)
        library_service = LibraryService(book_repository)
        coordinator = LibraryMaintenanceCoordinator()
        limits = archive_open_limits_from_settings(settings)
        import_service = ImportService(
            paths,
            database,
            reader.archive_image_service,
            reader.hash_service,
            settings.hash_algorithm,
            path_issue_service=reader.path_issue_service,
            tag_service=tag_service,
            archive_limits=limits,
            verify_imported_file_integrity=settings.verify_imported_file_integrity,
            maintenance_coordinator=coordinator,
            pdf_service=reader.pdf_image_service,
            canonical_import_policy=normalize_canonical_import_policy(
                settings.canonical_import_policy
            ),
        )
        export_service = ExportService(book_repository, reader.hash_service)
        library_caches = LibraryCacheService(config.cover_index_max_items)
        cache_service = CacheService(
            reader.archive_extraction_pool,
            settings.reader_page_cache_mb * 1024 * 1024,
            settings.thumbnail_cache_mb * 1024 * 1024,
            config.cover_index_max_items,
            reader_caches=reader.cache_service,
            library_caches=library_caches,
        )
        thumbnails = ThumbnailService(
            paths,
            reader.archive_image_service,
            cache_service,
            reader.reader_session_service,
            nested_archive_max_depth=settings.nested_archive_max_depth,
            archive_global_file_max_depth=settings.archive_global_file_max_depth,
            archive_limits=limits,
            thumbnail_renderer=reader.thumbnail_renderer,
        )
        maintenance = LibraryMaintenanceService(
            paths,
            database,
            reader.hash_service,
            reader.archive_image_service,
            archive_limits=limits,
            extraction_cache=reader.archive_extraction_pool,
            invalidate_file_cache=thumbnails.invalidate_file_cache,
            coordinator=coordinator,
            pdf_service=reader.pdf_image_service,
        )
        recovery = maintenance.recover_pending_journal()
        hidden_space = HiddenSpaceService(settings_store, library_service)
        shelf = ShelfViewModel(
            library_service,
            thumbnails,
            reader.task_service,
            cover_size=(Theme.detail_cover_width, Theme.detail_cover_height),
            settings=settings,
            settings_store=settings_store,
            tag_service=tag_service,
            archive_warmup_coordinator=reader.archive_warmup_coordinator,
        )
        settings_viewmodel = settings_viewmodel or SettingsViewModel(settings, settings_store, hidden_space)
        return LibraryRuntime(
            paths=paths,
            database_interpreter=database,
            book_repository=book_repository,
            tag_repository=tag_repository,
            library_service=library_service,
            tag_service=tag_service,
            import_service=import_service,
            export_service=export_service,
            thumbnail_service=thumbnails,
            cache_service=cache_service,
            library_caches=library_caches,
            library_maintenance_coordinator=coordinator,
            library_maintenance_service=maintenance,
            hidden_space_service=hidden_space,
            shelf_viewmodel=shelf,
            tag_management_viewmodel=TagManagementViewModel(tag_service),
            settings_viewmodel=settings_viewmodel,
            main_window_viewmodel=MainWindowViewModel(),
            maintenance_recovery_conflicts=bool(recovery.conflicts),
        )
    except BaseException:
        for name, resource in (("thumbnails", thumbnails), ("database", database)):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception:
                logger.error("Partial Library %s cleanup failed", name, exc_info=True)
        raise
