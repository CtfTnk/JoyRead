"""Application dependency container."""

from __future__ import annotations

from dataclasses import dataclass
from os import environ
from pathlib import Path

from joyread.core.repositories.book_repository import BookRepository
from joyread.core.repositories.mock_book_repository import MockBookRepository
from joyread.core.services.cache_service import CacheService
from joyread.core.services.library_service import LibraryService
from joyread.core.services.task_service import TaskService
from joyread.core.services.thumbnail_service import ThumbnailService
from joyread.infrastructure.config.app_config import AppConfig
from joyread.infrastructure.filesystem.path_service import PathService
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.viewmodels.main_window_viewmodel import MainWindowViewModel
from joyread.ui.viewmodels.settings_viewmodel import SettingsViewModel
from joyread.ui.viewmodels.shelf_viewmodel import ShelfViewModel


@dataclass
class AppContext:
    config: AppConfig
    paths: PathService
    resources: ResourceLoader
    book_repository: BookRepository
    library_service: LibraryService
    task_service: TaskService
    cache_service: CacheService
    thumbnail_service: ThumbnailService
    main_window_viewmodel: MainWindowViewModel
    shelf_viewmodel: ShelfViewModel
    settings_viewmodel: SettingsViewModel


def create_app_context() -> AppContext:
    config = AppConfig()
    runtime_override = environ.get("JOYREAD_RUNTIME_DIR")
    base_dir = Path(runtime_override) if runtime_override else None
    paths = PathService(config.app_name, config.app_author, base_dir=base_dir)
    resources = ResourceLoader()
    book_repository = MockBookRepository()
    library_service = LibraryService(book_repository)
    task_service = TaskService(config.max_background_workers)
    cache_service = CacheService(
        thumbnail_limit_mb=config.thumbnail_cache_memory_limit_mb,
        page_limit_mb=config.page_cache_memory_limit_mb,
    )
    thumbnail_service = ThumbnailService(cache_service)
    main_window_viewmodel = MainWindowViewModel()
    shelf_viewmodel = ShelfViewModel(library_service)
    settings_viewmodel = SettingsViewModel()

    return AppContext(
        config=config,
        paths=paths,
        resources=resources,
        book_repository=book_repository,
        library_service=library_service,
        task_service=task_service,
        cache_service=cache_service,
        thumbnail_service=thumbnail_service,
        main_window_viewmodel=main_window_viewmodel,
        shelf_viewmodel=shelf_viewmodel,
        settings_viewmodel=settings_viewmodel,
    )
