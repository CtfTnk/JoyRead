"""Application dependency container."""

from __future__ import annotations

from dataclasses import dataclass
from os import environ
from pathlib import Path

from joyread.core.archive import ArchiveImageService
from joyread.core.repositories.book_repository import BookRepository
from joyread.core.repositories.mock_book_repository import MockBookRepository
from joyread.core.repositories.sqlite_book_repository import SqliteBookRepository
from joyread.core.reader import ReaderSessionService
from joyread.core.services.cache_service import CacheService
from joyread.core.services.export_service import ExportService
from joyread.core.services.hash_service import HashService
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


@dataclass
class AppContext:
    config: AppConfig
    settings: AppSettings
    settings_store: SettingsStore
    paths: PathService
    resources: ResourceLoader
    database_interpreter: DatabaseInterpreter
    book_repository: BookRepository
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
    main_window_viewmodel: MainWindowViewModel
    shelf_viewmodel: ShelfViewModel
    settings_viewmodel: SettingsViewModel

    def close(self) -> None:
        self.database_interpreter.close()

    def reconfigure_storage(self, new_root: Path) -> None:
        old_root = Path(self.settings.storage_location)
        self.database_interpreter.close()
        self.storage_migration_service.move_storage_location(old_root, new_root)
        self.reload_storage_from_settings()

    def reload_storage_from_settings(self) -> None:
        self.settings = self.settings_store.load()
        self.paths = _create_path_service(self.config, self.settings_store, self.settings)
        self.paths.ensure_directories()
        self.database_interpreter = _create_database_interpreter(self.paths)
        self.book_repository = _create_sqlite_book_repository(self.database_interpreter, self.paths)
        self.library_service = LibraryService(self.book_repository)
        self.thumbnail_service = ThumbnailService(self.paths, self.archive_image_service, self.cache_service)
        self.import_service = ImportService(
            self.paths,
            self.database_interpreter,
            self.archive_image_service,
            self.hash_service,
            self.settings.hash_algorithm,
        )
        self.export_service = ExportService(self.book_repository, self.hash_service)
        self.shelf_viewmodel.replace_services(self.library_service, self.thumbnail_service)
        self.settings_viewmodel.set_storage_location(self.settings.storage_location)


def create_app_context() -> AppContext:
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
    use_mock = environ.get("JOYREAD_USE_MOCK_REPOSITORY") == "1"
    book_repository: BookRepository = (
        MockBookRepository() if use_mock else _create_sqlite_book_repository(database_interpreter, paths)
    )
    archive_image_service = ArchiveImageService()
    reader_session_service = ReaderSessionService(archive_image_service)
    library_service = LibraryService(book_repository)
    task_service = TaskService(config.max_background_workers)
    hash_service = HashService()
    cache_service = CacheService(
        thumbnail_limit_mb=config.thumbnail_cache_memory_limit_mb,
        page_limit_mb=config.page_cache_memory_limit_mb,
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
    thumbnail_service = ThumbnailService(paths, archive_image_service, cache_service)
    main_window_viewmodel = MainWindowViewModel()
    shelf_viewmodel = ShelfViewModel(
        library_service,
        thumbnail_service,
        task_service,
        cover_size=(Theme.detail_cover_width, Theme.detail_cover_height),
        settings=settings,
        settings_store=settings_store,
    )
    settings_viewmodel = SettingsViewModel(settings, settings_store)

    return AppContext(
        config=config,
        settings=settings,
        settings_store=settings_store,
        paths=paths,
        resources=resources,
        database_interpreter=database_interpreter,
        book_repository=book_repository,
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
        main_window_viewmodel=main_window_viewmodel,
        shelf_viewmodel=shelf_viewmodel,
        settings_viewmodel=settings_viewmodel,
    )


def _create_path_service(config: AppConfig, settings_store: SettingsStore, settings: AppSettings) -> PathService:
    return PathService(
        config.app_name,
        config.app_author,
        storage_root=Path(settings.storage_location),
        support_root=settings_store.support_root,
    )


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
