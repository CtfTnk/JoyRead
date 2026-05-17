"""Application dependency container."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from os import environ
from pathlib import Path

from joyread.core.archive import ArchiveImageService
from joyread.core.models.cache import ArchiveCacheStrategy, normalize_archive_cache_strategy
from joyread.core.repositories.book_repository import BookRepository
from joyread.core.repositories.sqlite_book_repository import SqliteBookRepository
from joyread.core.reader import ReaderSessionService
from joyread.core.services.archive_extraction_pool import (
    ArchiveExtractionCache,
    ArchiveExtractionPool,
    HiddenImageExtractionPool,
)
from joyread.core.services.cache_service import CacheService
from joyread.core.services.export_service import ExportService
from joyread.core.services.hash_service import HashService
from joyread.core.services.hidden_space_service import HiddenSpaceService
from joyread.core.services.import_service import ImportService
from joyread.core.services.library_service import LibraryService
from joyread.core.services.storage_migration_service import StorageMigrationService
from joyread.core.services.task_service import TaskService
from joyread.core.services.thumbnail_service import ThumbnailService
from joyread.infrastructure.config.app_config import AppConfig
from joyread.infrastructure.config.settings_store import AppSettings, SettingsStore
from joyread.infrastructure.database import DatabaseInterpreter, DatabasePriority, apply_migrations
from joyread.infrastructure.filesystem.path_service import PathService
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.main_window_viewmodel import MainWindowViewModel
from joyread.ui.viewmodels.settings_viewmodel import SettingsViewModel
from joyread.ui.viewmodels.shelf_viewmodel import ShelfViewModel


logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    config: AppConfig
    settings: AppSettings
    settings_store: SettingsStore
    paths: PathService
    resources: ResourceLoader
    database_interpreter: DatabaseInterpreter
    book_repository: BookRepository
    archive_extraction_pool: ArchiveExtractionCache
    archive_image_service: ArchiveImageService
    reader_session_service: ReaderSessionService
    library_service: LibraryService
    task_service: TaskService
    cache_service: CacheService
    hash_service: HashService
    import_service: ImportService
    export_service: ExportService
    storage_migration_service: StorageMigrationService
    thumbnail_service: ThumbnailService
    hidden_space_service: HiddenSpaceService
    main_window_viewmodel: MainWindowViewModel
    shelf_viewmodel: ShelfViewModel
    settings_viewmodel: SettingsViewModel

    def close(self) -> None:
        logger.info("AppContext shutting down: cancelling tasks then closing database")
        self.task_service.shutdown()
        self.database_interpreter.close()
        logger.info("AppContext shutdown complete")

    def reconfigure_storage(self, new_root: Path) -> None:
        old_root = Path(self.settings.storage_location)
        logger.info("Reconfiguring storage: %s -> %s", old_root, new_root)
        self.database_interpreter.close()
        self.storage_migration_service.move_storage_location(old_root, new_root)
        self.reload_storage_from_settings()

    def reload_storage_from_settings(self) -> None:
        # Rebuild every storage-rooted service in the right order: settings →
        # path service → archive pool → archive reading stack → database. A
        # piecemeal swap risks dangling references to the previous storage
        # root, so the whole subtree is reconstructed atomically here.
        self.settings = self.settings_store.load()
        logger.info("Reloading storage from settings root=%s", self.settings.storage_location)
        self.paths = _create_path_service(self.config, self.settings_store, self.settings)
        self.paths.ensure_directories()
        # The cache directory follows the storage root, so changing storage
        # rebuilds the pool against the new location. Bytes left behind under
        # the old root are harmless: they sit inside the old extraction
        # directory and will simply not be referenced again.
        self.archive_extraction_pool = _create_archive_extraction_cache(self.paths, self.settings)
        self._rebuild_archive_reading_services()
        self.database_interpreter = _create_database_interpreter(self.paths)
        self.book_repository = _create_sqlite_book_repository(self.database_interpreter, self.paths)
        self.library_service = LibraryService(self.book_repository)
        # Hidden Space caches the library service reference, so it has to
        # follow the storage rebuild — otherwise its operations would still
        # hit the old (closed) database.
        self.hidden_space_service = HiddenSpaceService(self.settings_store, self.library_service)
        self.thumbnail_service = ThumbnailService(
            self.paths,
            self.archive_image_service,
            self.cache_service,
            self.reader_session_service,
        )
        self.import_service = ImportService(
            self.paths,
            self.database_interpreter,
            self.archive_image_service,
            self.hash_service,
            self.settings.hash_algorithm,
        )
        self.export_service = ExportService(self.book_repository, self.hash_service)
        self.shelf_viewmodel.replace_services(self.library_service, self.thumbnail_service)
        self.settings_viewmodel.set_hidden_space_service(self.hidden_space_service)
        self.settings_viewmodel.set_storage_location(self.settings.storage_location)
        self.settings_viewmodel.set_archive_pool_bytes_provider(lambda: self.archive_extraction_pool.current_bytes)
        self._refresh_settings_pool_usage()

    def apply_cache_settings(self) -> None:
        """Push the current settings into every cache that owns a live budget.

        Called when the user edits a value under the Cache group on the
        Settings page. ``CacheService.apply_cache_budgets`` covers the shared
        reader page cache and the disk pool; the detail-thumbnail cache lives
        on ``ShelfViewModel`` so we resize it explicitly here.
        """

        previous_strategy = normalize_archive_cache_strategy(self.settings.archive_cache_strategy)
        self.settings = self.settings_store.load()
        next_strategy = normalize_archive_cache_strategy(self.settings.archive_cache_strategy)
        logger.info(
            "Applying cache settings: strategy=%s->%s reader_mb=%s pool_mb=%s detail_mb=%s",
            previous_strategy.value,
            next_strategy.value,
            self.settings.reader_page_cache_mb,
            self.settings.archive_extraction_pool_mb,
            self.settings.detail_thumbnail_cache_mb,
        )
        if next_strategy != previous_strategy:
            # Strategy change isn't a resize — `ArchiveExtractionPool` and
            # `HiddenImageExtractionPool` use completely different on-disk
            # layouts. The old pool's bytes must be cleared and the dependent
            # archive/thumbnail/import services rebuilt against the new pool
            # so they don't keep a reference to the old object.
            self.archive_extraction_pool.clear()
            self.archive_extraction_pool = _create_archive_extraction_cache(self.paths, self.settings)
            self._rebuild_archive_reading_services()
            self.thumbnail_service = ThumbnailService(
                self.paths,
                self.archive_image_service,
                self.cache_service,
                self.reader_session_service,
            )
            self.import_service = ImportService(
                self.paths,
                self.database_interpreter,
                self.archive_image_service,
                self.hash_service,
                self.settings.hash_algorithm,
            )
            self.settings_viewmodel.set_archive_pool_bytes_provider(lambda: self.archive_extraction_pool.current_bytes)
            self.shelf_viewmodel.replace_services(self.library_service, self.thumbnail_service)
            self.cache_service.apply_cache_budgets(
                reader_page_cache_bytes=self.settings.reader_page_cache_mb * 1024 * 1024,
            )
        else:
            self.cache_service.apply_cache_budgets(
                reader_page_cache_bytes=self.settings.reader_page_cache_mb * 1024 * 1024,
                archive_extraction_pool_bytes=self.settings.archive_extraction_pool_mb * 1024 * 1024,
            )
        self.shelf_viewmodel.resize_detail_thumbnail_cache(
            self.settings.detail_thumbnail_cache_mb * 1024 * 1024,
        )
        self._refresh_settings_pool_usage()

    def clear_archive_extraction_pool(self) -> None:
        """User-triggered "Clear archive cache" button hook."""

        bytes_before = self.archive_extraction_pool.current_bytes
        self.archive_extraction_pool.clear()
        logger.info("Cleared archive extraction pool: freed %d bytes", bytes_before)
        self._refresh_settings_pool_usage()

    def _refresh_settings_pool_usage(self) -> None:
        self.settings_viewmodel.refresh_archive_pool_usage()

    def _rebuild_archive_reading_services(self) -> None:
        self.archive_image_service = ArchiveImageService(extraction_pool=self.archive_extraction_pool)
        self.reader_session_service = ReaderSessionService(self.archive_image_service)
        self.cache_service.archive_extraction_pool = self.archive_extraction_pool


def create_app_context() -> AppContext:
    logger.info("Creating AppContext")
    config = AppConfig()
    runtime_override = environ.get("JOYREAD_RUNTIME_DIR")
    support_root = Path(runtime_override) / ".joyread_support" if runtime_override else None
    default_storage_root = Path(runtime_override) / ".joyread_storage" if runtime_override else None
    settings_store = SettingsStore(
        config.app_name,
        config.app_author,
        support_root=support_root,
        default_storage_root=default_storage_root,
    )
    settings = settings_store.load()
    paths = _create_path_service(config, settings_store, settings)
    paths.ensure_directories()
    resources = ResourceLoader()
    database_interpreter = _create_database_interpreter(paths)
    book_repository: BookRepository = _create_sqlite_book_repository(database_interpreter, paths)
    archive_extraction_pool = _create_archive_extraction_cache(paths, settings)
    archive_image_service = ArchiveImageService(extraction_pool=archive_extraction_pool)
    reader_session_service = ReaderSessionService(archive_image_service)
    library_service = LibraryService(book_repository)
    task_service = TaskService(config.max_background_workers)
    hash_service = HashService()
    cache_service = CacheService(
        archive_extraction_pool=archive_extraction_pool,
        reader_page_cache_max_bytes=settings.reader_page_cache_mb * 1024 * 1024,
        cover_index_max_items=config.cover_index_max_items,
    )
    import_service = ImportService(
        paths,
        database_interpreter,
        archive_image_service,
        hash_service,
        settings.hash_algorithm,
    )
    export_service = ExportService(book_repository, hash_service)
    storage_migration_service = StorageMigrationService(settings_store)
    thumbnail_service = ThumbnailService(paths, archive_image_service, cache_service, reader_session_service)
    hidden_space_service = HiddenSpaceService(settings_store, library_service)
    main_window_viewmodel = MainWindowViewModel()
    shelf_viewmodel = ShelfViewModel(
        library_service,
        thumbnail_service,
        task_service,
        cover_size=(Theme.detail_cover_width, Theme.detail_cover_height),
        settings=settings,
        settings_store=settings_store,
    )
    settings_viewmodel = SettingsViewModel(settings, settings_store, hidden_space_service)

    context = AppContext(
        config=config,
        settings=settings,
        settings_store=settings_store,
        paths=paths,
        resources=resources,
        database_interpreter=database_interpreter,
        book_repository=book_repository,
        archive_extraction_pool=archive_extraction_pool,
        archive_image_service=archive_image_service,
        reader_session_service=reader_session_service,
        library_service=library_service,
        task_service=task_service,
        cache_service=cache_service,
        hash_service=hash_service,
        import_service=import_service,
        export_service=export_service,
        storage_migration_service=storage_migration_service,
        thumbnail_service=thumbnail_service,
        hidden_space_service=hidden_space_service,
        main_window_viewmodel=main_window_viewmodel,
        shelf_viewmodel=shelf_viewmodel,
        settings_viewmodel=settings_viewmodel,
    )
    # The settings panel renders a live "used / budget" label for the disk
    # pool; provide it a thin lambda so the viewmodel can poll the current
    # strategy object even after a runtime cache-strategy switch.
    settings_viewmodel.set_archive_pool_bytes_provider(lambda: context.archive_extraction_pool.current_bytes)
    # Hook user-driven cache actions back into the live services. Owning the
    # connection in AppContext keeps the viewmodel UI-only and makes the side
    # effects (resize/clear) easy to find from one place.
    settings_viewmodel.cache_budgets_changed.connect(context.apply_cache_settings)
    settings_viewmodel.clear_archive_pool_requested.connect(context.clear_archive_extraction_pool)
    logger.info(
        "AppContext ready (storage=%s, workers=%d, archive_cache_strategy=%s)",
        settings.storage_location,
        config.max_background_workers,
        settings.archive_cache_strategy,
    )
    return context


def _create_path_service(config: AppConfig, settings_store: SettingsStore, settings: AppSettings) -> PathService:
    return PathService(
        config.app_name,
        config.app_author,
        storage_root=Path(settings.storage_location),
        support_root=settings_store.support_root,
    )


def _create_archive_extraction_cache(paths: PathService, settings: AppSettings) -> ArchiveExtractionCache:
    strategy = normalize_archive_cache_strategy(settings.archive_cache_strategy)
    max_bytes = settings.archive_extraction_pool_mb * 1024 * 1024
    if strategy == ArchiveCacheStrategy.HIDDEN_IMAGE_FILES:
        return HiddenImageExtractionPool(paths.paths.cache / ".archive_image_pages", max_bytes=max_bytes)
    return ArchiveExtractionPool(paths.paths.cache / ".archive_zip_bundles", max_bytes=max_bytes)


def _create_database_interpreter(paths: PathService) -> DatabaseInterpreter:
    database = DatabaseInterpreter(paths.paths.database / "joyread.sqlite3")
    database.execute(apply_migrations, DatabasePriority.CRITICAL)
    return database


def _create_sqlite_book_repository(database: DatabaseInterpreter, paths: PathService) -> SqliteBookRepository:
    return SqliteBookRepository(
        database,
        managed_books_root=paths.paths.books,
        thumbnails_root=paths.paths.thumbnails,
    )
