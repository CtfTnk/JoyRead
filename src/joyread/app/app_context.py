"""Application dependency container."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from joyread.core.archive import ArchiveImageService, ArchiveOpenLimits
from joyread.core.archive.limits import GIB, MEGAPIXEL
from joyread.core.models.cache import ArchiveCacheStrategy, normalize_archive_cache_strategy
from joyread.core.repositories.book_repository import BookRepository
from joyread.core.repositories.sqlite_book_repository import SqliteBookRepository
from joyread.core.repositories.sqlite_tag_repository import SqliteTagRepository
from joyread.core.repositories.tag_repository import TagRepository
from joyread.core.reader import ReaderSessionService
from joyread.core.services.archive_extraction_pool import (
    ArchiveExtractionCache,
    ArchiveExtractionPool,
    HiddenImageExtractionPool,
)
from joyread.app.archive_pool_usage_bridge import ArchivePoolUsageBridge
from joyread.app.archive_warmup_coordinator import ArchiveWarmupCoordinator
from joyread.core.services.cache_service import CacheService
from joyread.core.services.export_service import ExportService
from joyread.core.services.hash_service import HashService
from joyread.core.services.hidden_space_service import HiddenSpaceService
from joyread.core.services.import_service import ImportService
from joyread.core.services.library_service import LibraryService
from joyread.core.services.library_maintenance_service import (
    LibraryMaintenanceCoordinator,
    LibraryMaintenanceLease,
    LibraryMaintenanceService,
)
from joyread.core.services.storage_migration_service import (
    StorageMigrationResult,
    StorageMigrationService,
)
from joyread.core.services.storage_recovery_service import RecoveryPrompt, StorageRecoveryService
from joyread.core.services.storage_validation_service import (
    StorageValidationResult,
    StorageValidationService,
)
from joyread.core.services.tag_service import TagService
from joyread.infrastructure.pdf_document_thread import shutdown_pdf_thread
from joyread.infrastructure.pdf_image_service import PdfImageService
from joyread.infrastructure.qt_task_service import TaskService
from joyread.infrastructure.reader_image_decoder import qimage_frame_bytes
from joyread.infrastructure.thumbnail_renderer import QtThumbnailRenderer
from joyread.core.services.thumbnail_service import ThumbnailService
from joyread.infrastructure.config.app_config import AppConfig
from joyread.infrastructure.config.settings_store import (
    AppSettings,
    SettingsStore,
    create_environment_settings_store,
)
from joyread.infrastructure.i18n import locale_service
from joyread.infrastructure.database import DatabaseInterpreter, DatabasePriority, apply_migrations
from joyread.infrastructure.filesystem.path_service import PathService, WritableLocation
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.main_window_viewmodel import MainWindowViewModel
from joyread.ui.viewmodels.settings_viewmodel import SettingsViewModel
from joyread.ui.viewmodels.shelf_viewmodel import ShelfViewModel
from joyread.ui.viewmodels.tag_management_viewmodel import TagManagementViewModel


logger = logging.getLogger(__name__)


@dataclass
class StorageTransition:
    """Outcome of a worker-side storage mutation awaiting UI-side rebuild.

    The coordinator lease intentionally remains held while the task result is
    queued back to Qt.  ``finish_storage_transition`` rebuilds services on the
    UI thread and releases it, preventing queued import/audit work from using
    the closed database interpreter in the gap.
    """

    operation: str
    lease: LibraryMaintenanceLease
    result: StorageMigrationResult | StorageValidationResult | None = None
    error: Exception | None = None
    reload_required: bool = False


@dataclass
class AppContext:
    """Runtime dependency graph for the application.

    Keep construction here instead of inside widgets. Views receive already
    wired ViewModels and services, which preserves the MVVM rule that UI code
    should not know how SQLite, cache directories, or import services are built.
    """

    config: AppConfig
    settings: AppSettings
    settings_store: SettingsStore
    paths: PathService
    resources: ResourceLoader
    database_interpreter: DatabaseInterpreter
    book_repository: BookRepository
    tag_repository: TagRepository
    archive_extraction_pool: ArchiveExtractionCache
    archive_image_service: ArchiveImageService
    reader_session_service: ReaderSessionService
    pdf_image_service: PdfImageService
    library_service: LibraryService
    task_service: TaskService
    cache_service: CacheService
    hash_service: HashService
    tag_service: TagService
    import_service: ImportService
    library_maintenance_coordinator: LibraryMaintenanceCoordinator
    library_maintenance_service: LibraryMaintenanceService
    export_service: ExportService
    storage_migration_service: StorageMigrationService
    storage_validation_service: StorageValidationService
    storage_recovery_service: StorageRecoveryService
    thumbnail_service: ThumbnailService
    hidden_space_service: HiddenSpaceService
    main_window_viewmodel: MainWindowViewModel
    shelf_viewmodel: ShelfViewModel
    settings_viewmodel: SettingsViewModel
    tag_management_viewmodel: TagManagementViewModel
    thumbnail_renderer: QtThumbnailRenderer | None = None
    archive_warmup_coordinator: ArchiveWarmupCoordinator | None = None
    #: Carries live pool usage from caching workers to the settings page.
    archive_pool_usage_bridge: ArchivePoolUsageBridge | None = None
    # Populated when startup recovery had to fall back to another library;
    # the main window shows it once on launch.
    storage_startup_notice: str | None = None
    library_maintenance_recovery_conflicts: bool = False
    #: Set once a storage transition passes its commit point, where terminal
    #: services are released. Every outcome after that has to rebuild, even one
    #: that never touched storage.
    storage_rebuild_required: bool = False

    def close(self) -> None:
        logger.info("AppContext shutting down: cancelling tasks then closing database")
        if self.archive_warmup_coordinator is not None:
            self.archive_warmup_coordinator.close()
        self.task_service.shutdown()
        if self.thumbnail_service is not None:
            self.thumbnail_service.close()
        # Task shutdown stops accepting new work first. PDF shutdown then seals
        # its own queue behind any render already accepted and joins when that
        # queue drains, without destroying a still-running QThread on timeout.
        shutdown_pdf_thread()
        self.database_interpreter.close()
        logger.info("AppContext shutdown complete")

    def quiesce_for_storage_transition(self) -> int:
        """Stop storage-dependent producers, reversibly.

        Same order as :meth:`close` and for the same reason: the warmup
        coordinator first, so its cancellation reaches an in-flight extractor
        before anything else changes underneath it, then the task service.

        Only reversible work happens here. ``ThumbnailService.close()`` is
        terminal -- a closed one is never usable again -- so it belongs to
        :meth:`commit_storage_transition`, past the point of no return. That
        split is what lets a drain that times out put the application back
        exactly as it was. The PDF thread is left alone for the same reason;
        it is stopped at commit and restarts itself on the next call.

        Returns the number of tasks still unwinding. This does not join -- see
        :meth:`TaskService.quiesce` -- so the caller must poll
        :meth:`storage_transition_pending_tasks` from the event loop and only
        migrate once it reaches zero.
        """

        logger.info("Quiescing for storage transition")
        if self.archive_warmup_coordinator is not None:
            self.archive_warmup_coordinator.close()
        return self.task_service.quiesce()

    def storage_transition_pending_tasks(self) -> int:
        return self.task_service.pending_task_count()

    def commit_storage_transition(self) -> None:
        """Release the services that must not outlive the retired storage.

        Called only once the drain is proven, immediately before the disk
        phase. Everything released here is reconstructed by
        :meth:`reload_storage_from_settings`, which every path out of a
        committed transition runs -- see :attr:`storage_rebuild_required`.

        The warmup coordinator is reset rather than merely closed. Its running
        task was cancelled during the drain, which suppresses the callback that
        would normally clear ``_active_key``; leaving it set would block every
        later warmup. Resetting is only safe now, because the drain proves the
        task is gone rather than still running.
        """

        self.storage_rebuild_required = True
        if self.archive_warmup_coordinator is not None:
            self.archive_warmup_coordinator.reset()
        if self.thumbnail_service is not None:
            self.thumbnail_service.close()
        shutdown_pdf_thread()

    def abandon_storage_transition(self) -> None:
        """Undo a quiesce that never committed.

        Reached when the drain times out. Nothing terminal has run and storage
        was never touched, so accepting work again is the whole of the repair.
        """

        self.task_service.resume()
        logger.warning("Storage transition abandoned before migrating")

    def resume_after_storage_transition(self) -> None:
        """Accept background work again, against the rebuilt storage stack."""

        self.task_service.resume()
        logger.info("Resumed after storage transition")

    def move_storage_to_parent(self, target_parent: Path) -> None:
        """Move the library into ``<target_parent>/JoyRead-Library`` and adopt it.

        Raises ``StorageMigrationError`` (with a user-facing message) if the
        destination already has a JoyRead-Library folder or the copy fails
        validation; the old storage is reopened and stays in use.
        """

        transition = self.begin_storage_move(target_parent)
        try:
            if transition.error is not None:
                raise transition.error
        finally:
            self.finish_storage_transition(transition)

    def select_storage(self, existing_root: Path) -> StorageValidationResult:
        """Switch to an existing JoyRead library without copying or deleting.

        Validates first; only on success is the database closed and settings
        re-pointed. On failure the current library keeps running.
        """

        transition = self.begin_storage_select(existing_root)
        try:
            if transition.error is not None:
                raise transition.error
            result = transition.result
            if not isinstance(result, StorageValidationResult):
                raise RuntimeError("Storage selection did not produce a validation result.")
            return result
        finally:
            self.finish_storage_transition(transition)

    def reset_storage(self) -> None:
        """Erase the current library and rebuild an empty one in place."""

        transition = self.begin_storage_reset()
        try:
            if transition.error is not None:
                raise transition.error
        finally:
            self.finish_storage_transition(transition)

    def begin_storage_move(self, target_parent: Path) -> StorageTransition:
        """Run the disk phase of Move while holding the maintenance gate.

        Call :meth:`finish_storage_transition` on the UI thread after the
        worker returns, even when ``error`` is populated.
        """

        lease = self.library_maintenance_coordinator.acquire("storage-move")
        old_root = Path(self.settings.storage_location)
        logger.info("Move storage requested: %s -> parent %s", old_root, target_parent)
        try:
            self.database_interpreter.close()
            result = self.storage_migration_service.move_to_parent(old_root, target_parent)
        except Exception as exc:
            return StorageTransition(
                "storage-move",
                lease,
                error=exc,
                reload_required=True,
            )
        return StorageTransition("storage-move", lease, result=result, reload_required=True)

    def begin_storage_select(self, existing_root: Path) -> StorageTransition:
        """Validate and adopt a library while excluding import/audit work."""

        lease = self.library_maintenance_coordinator.acquire("storage-select")
        existing_root = existing_root.expanduser().resolve()
        logger.info("Select existing library requested: %s", existing_root)
        try:
            result = self.storage_validation_service.validate_full(existing_root)
        except Exception as exc:
            lease.release()
            return StorageTransition("storage-select", lease, error=exc)
        if not result.ok:
            logger.warning("Select rejected (%s): %s", result.code, result.message)
            lease.release()
            return StorageTransition("storage-select", lease, result=result)
        try:
            self.database_interpreter.close()
            # Record the selected root as known-good, so startup recovery can
            # safely return to it if a future configured path is unavailable.
            self.settings_store.update(
                storage_location=str(existing_root),
                last_good_storage_location=str(existing_root),
            )
        except Exception as exc:
            return StorageTransition(
                "storage-select",
                lease,
                result=result,
                error=exc,
                reload_required=True,
            )
        return StorageTransition("storage-select", lease, result=result, reload_required=True)

    def begin_storage_reset(self) -> StorageTransition:
        """Run the destructive Reset phase while holding the maintenance gate."""

        lease = self.library_maintenance_coordinator.acquire("storage-reset")
        root = Path(self.settings.storage_location)
        logger.info("Reset storage requested at %s", root)
        try:
            self.database_interpreter.close()
            self.storage_migration_service.reset_library(root)
        except Exception as exc:
            return StorageTransition(
                "storage-reset",
                lease,
                error=exc,
                reload_required=True,
            )
        return StorageTransition("storage-reset", lease, reload_required=True)

    def finish_storage_transition(self, transition: StorageTransition) -> None:
        """Rebuild UI-facing services and release a worker-held storage lease.

        ``reload_required`` describes whether *storage* changed, which is not
        the same question as whether a rebuild is needed. A Select that is
        rejected by validation changes nothing on disk and reports
        ``reload_required=False``, but the commit phase has already closed the
        thumbnail service for good -- so the rebuild is driven by having
        committed, not by the outcome.
        """

        try:
            if transition.reload_required or self.storage_rebuild_required:
                self.reload_storage_from_settings()
        finally:
            self.storage_rebuild_required = False
            transition.lease.release()

    def apply_archive_depth_settings(self) -> None:
        """Compatibility entrypoint for older callers of depth-only settings."""

        self.apply_archive_open_limits()

    def apply_archive_open_limits(self) -> None:
        self.settings = self.settings_store.load()
        limits = _archive_open_limits_from_settings(self.settings)
        self.thumbnail_service.set_archive_open_limits(limits)
        self.import_service.set_archive_open_limits(limits)
        self.library_maintenance_service.set_archive_open_limits(limits)
        self.import_service.set_verify_imported_file_integrity(
            self.settings.verify_imported_file_integrity
        )
        if self.archive_warmup_coordinator is not None:
            self.archive_warmup_coordinator.invalidate()
        self.shelf_viewmodel.invalidate_detail_thumbnail_source()

    def reload_storage_from_settings(self) -> None:
        # Rebuild every storage-rooted service in the right order: settings →
        # path service → archive pool → archive reading stack → database. A
        # piecemeal swap risks dangling references to the previous storage
        # root, so the whole subtree is reconstructed atomically here.
        self.thumbnail_service.close()
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
        self.tag_repository = SqliteTagRepository(self.database_interpreter)
        self.tag_service = TagService(self.tag_repository)
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
            nested_archive_max_depth=self.settings.nested_archive_max_depth,
            archive_global_file_max_depth=self.settings.archive_global_file_max_depth,
            archive_limits=_archive_open_limits_from_settings(self.settings),
            thumbnail_renderer=self.thumbnail_renderer or QtThumbnailRenderer(),
        )
        self.import_service = ImportService(
            self.paths,
            self.database_interpreter,
            self.archive_image_service,
            self.hash_service,
            self.settings.hash_algorithm,
            tag_service=self.tag_service,
            archive_limits=_archive_open_limits_from_settings(self.settings),
            verify_imported_file_integrity=self.settings.verify_imported_file_integrity,
            maintenance_coordinator=self.library_maintenance_coordinator,
            pdf_service=self.pdf_image_service,
        )
        self.library_maintenance_service = _create_library_maintenance_service(
            self.paths,
            self.database_interpreter,
            self.hash_service,
            self.archive_image_service,
            self.thumbnail_service,
            self.archive_extraction_pool,
            _archive_open_limits_from_settings(self.settings),
            self.library_maintenance_coordinator,
            self.pdf_image_service,
        )
        self.export_service = ExportService(self.book_repository, self.hash_service)
        self.shelf_viewmodel.replace_services(self.library_service, self.thumbnail_service, self.tag_service)
        self.settings_viewmodel.set_hidden_space_service(self.hidden_space_service)
        self.settings_viewmodel.set_storage_location(self.settings.storage_location)
        self.settings_viewmodel.set_archive_pool_bytes_provider(lambda: self.archive_extraction_pool.current_bytes)
        # Tag VM holds the service reference and re-reads the DB on refresh,
        # so swap the underlying service to the rebuilt one.
        self.tag_management_viewmodel.replace_service(self.tag_service)
        self._refresh_settings_pool_usage()

    def apply_cache_settings(self) -> None:
        """Push the current settings into every cache that owns a live budget.

        Called when the user edits a value under the Cache group on the
        Settings page. ``CacheService.apply_cache_budgets`` covers the shared
        reader page cache, thumbnail cache, and disk extraction pool.
        """

        previous_strategy = normalize_archive_cache_strategy(self.settings.archive_cache_strategy)
        self.settings = self.settings_store.load()
        next_strategy = normalize_archive_cache_strategy(self.settings.archive_cache_strategy)
        logger.info(
            "Applying cache settings: strategy=%s->%s reader_mb=%s pool_gb=%s thumbnail_mb=%s",
            previous_strategy.value,
            next_strategy.value,
            self.settings.reader_page_cache_mb,
            self.settings.archive_extraction_pool_gb,
            self.settings.thumbnail_cache_mb,
        )
        if next_strategy != previous_strategy:
            # Strategy change isn't a resize — `ArchiveExtractionPool` and
            # `HiddenImageExtractionPool` use completely different on-disk
            # layouts. The old pool's bytes must be cleared and the dependent
            # archive/thumbnail/import services rebuilt against the new pool
            # so they don't keep a reference to the old object.
            self.thumbnail_service.close()
            self.archive_extraction_pool.clear()
            self.archive_extraction_pool = _create_archive_extraction_cache(self.paths, self.settings)
            self._rebuild_archive_reading_services()
            self.thumbnail_service = ThumbnailService(
                self.paths,
                self.archive_image_service,
                self.cache_service,
                self.reader_session_service,
                nested_archive_max_depth=self.settings.nested_archive_max_depth,
                archive_global_file_max_depth=self.settings.archive_global_file_max_depth,
                archive_limits=_archive_open_limits_from_settings(self.settings),
                thumbnail_renderer=self.thumbnail_renderer or QtThumbnailRenderer(),
            )
            self.import_service = ImportService(
                self.paths,
                self.database_interpreter,
                self.archive_image_service,
                self.hash_service,
                self.settings.hash_algorithm,
                tag_service=self.tag_service,
                archive_limits=_archive_open_limits_from_settings(self.settings),
                verify_imported_file_integrity=self.settings.verify_imported_file_integrity,
                maintenance_coordinator=self.library_maintenance_coordinator,
                pdf_service=self.pdf_image_service,
            )
            self.library_maintenance_service = _create_library_maintenance_service(
                self.paths,
                self.database_interpreter,
                self.hash_service,
                self.archive_image_service,
                self.thumbnail_service,
                self.archive_extraction_pool,
                _archive_open_limits_from_settings(self.settings),
                self.library_maintenance_coordinator,
                self.pdf_image_service,
            )
            self.settings_viewmodel.set_archive_pool_bytes_provider(lambda: self.archive_extraction_pool.current_bytes)
            self.shelf_viewmodel.replace_services(self.library_service, self.thumbnail_service, self.tag_service)
            self.cache_service.apply_cache_budgets(
                reader_page_cache_bytes=self.settings.reader_page_cache_mb * 1024 * 1024,
                thumbnail_cache_bytes=self.settings.thumbnail_cache_mb * 1024 * 1024,
            )
        else:
            self.cache_service.apply_cache_budgets(
                reader_page_cache_bytes=self.settings.reader_page_cache_mb * 1024 * 1024,
                thumbnail_cache_bytes=self.settings.thumbnail_cache_mb * 1024 * 1024,
                archive_extraction_pool_bytes=self.settings.archive_extraction_pool_gb * 1024 * 1024 * 1024,
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

    def attach_archive_pool_usage_bridge(self) -> None:
        """Point the usage bridge at whichever pool is currently live.

        Called wherever the pool is replaced, so the settings page follows the
        new one and stops hearing from the retired one.
        """

        bridge = self.archive_pool_usage_bridge
        if bridge is None:
            return
        bridge.attach(self.archive_extraction_pool)

    def _rebuild_archive_reading_services(self) -> None:
        logger.debug("Rebuilding archive reader services for cache=%s", type(self.archive_extraction_pool).__name__)
        self.attach_archive_pool_usage_bridge()
        self.archive_image_service = ArchiveImageService(extraction_pool=self.archive_extraction_pool)
        self.reader_session_service = ReaderSessionService(
            self.archive_image_service,
            self.pdf_image_service,
        )
        if self.archive_warmup_coordinator is not None:
            self.archive_warmup_coordinator.replace_session_service(self.reader_session_service)
        self.cache_service.archive_extraction_pool = self.archive_extraction_pool


def create_app_context(
    recovery_prompt: RecoveryPrompt | None = None,
    *,
    config: AppConfig | None = None,
    settings_store: SettingsStore | None = None,
) -> AppContext:
    logger.info("Creating AppContext")
    config = config or AppConfig()
    settings_store = settings_store or create_environment_settings_store(
        config.app_name,
        config.app_author,
    )
    # Resolve the storage root before anything is built: first-run init,
    # daily health check, and recovery all happen here so the rest of the
    # graph is wired against a known-usable library.
    storage_validation_service = StorageValidationService()
    storage_migration_service = StorageMigrationService(settings_store, storage_validation_service)
    storage_recovery_service = StorageRecoveryService(
        settings_store, storage_validation_service, storage_migration_service
    )
    startup = storage_recovery_service.prepare(recovery_prompt)
    settings = startup.settings
    paths = _create_path_service(config, settings_store, settings)
    paths.ensure_directories()
    resources = ResourceLoader()
    # Initialise the locale service before any UI is constructed.
    locale_service.init(
        bundled_dir=resources.locale_dir(),
        user_dir=settings_store.locales_dir if settings_store.locales_dir.exists() else None,
        language=settings.language,
    )
    database_interpreter = _create_database_interpreter(paths)
    book_repository: BookRepository = _create_sqlite_book_repository(database_interpreter, paths)
    tag_repository: TagRepository = SqliteTagRepository(database_interpreter)
    tag_service = TagService(tag_repository)
    archive_extraction_pool = _create_archive_extraction_cache(paths, settings)
    archive_image_service = ArchiveImageService(extraction_pool=archive_extraction_pool)
    pdf_image_service = PdfImageService()
    reader_session_service = ReaderSessionService(archive_image_service, pdf_image_service)
    library_service = LibraryService(book_repository)
    task_service = TaskService(config.max_background_workers)
    archive_warmup_coordinator = ArchiveWarmupCoordinator(reader_session_service, task_service)
    hash_service = HashService()
    library_maintenance_coordinator = LibraryMaintenanceCoordinator()
    cache_service = CacheService(
        archive_extraction_pool=archive_extraction_pool,
        reader_page_cache_max_bytes=settings.reader_page_cache_mb * 1024 * 1024,
        thumbnail_cache_max_bytes=settings.thumbnail_cache_mb * 1024 * 1024,
        cover_index_max_items=config.cover_index_max_items,
        reader_frame_sizer=qimage_frame_bytes,
    )
    thumbnail_renderer = QtThumbnailRenderer()
    import_service = ImportService(
        paths,
        database_interpreter,
        archive_image_service,
        hash_service,
        settings.hash_algorithm,
        tag_service=tag_service,
        archive_limits=_archive_open_limits_from_settings(settings),
        verify_imported_file_integrity=settings.verify_imported_file_integrity,
        maintenance_coordinator=library_maintenance_coordinator,
        pdf_service=pdf_image_service,
    )
    export_service = ExportService(book_repository, hash_service)
    thumbnail_service = ThumbnailService(
        paths,
        archive_image_service,
        cache_service,
        reader_session_service,
        nested_archive_max_depth=settings.nested_archive_max_depth,
        archive_global_file_max_depth=settings.archive_global_file_max_depth,
        archive_limits=_archive_open_limits_from_settings(settings),
        thumbnail_renderer=thumbnail_renderer,
    )
    library_maintenance_service = _create_library_maintenance_service(
        paths,
        database_interpreter,
        hash_service,
        archive_image_service,
        thumbnail_service,
        archive_extraction_pool,
        _archive_open_limits_from_settings(settings),
        library_maintenance_coordinator,
        pdf_image_service,
    )
    maintenance_recovery = library_maintenance_service.recover_pending_journal()
    hidden_space_service = HiddenSpaceService(settings_store, library_service)
    main_window_viewmodel = MainWindowViewModel()
    shelf_viewmodel = ShelfViewModel(
        library_service,
        thumbnail_service,
        task_service,
        cover_size=(Theme.detail_cover_width, Theme.detail_cover_height),
        settings=settings,
        settings_store=settings_store,
        tag_service=tag_service,
        archive_warmup_coordinator=archive_warmup_coordinator,
    )
    settings_viewmodel = SettingsViewModel(settings, settings_store, hidden_space_service)
    tag_management_viewmodel = TagManagementViewModel(tag_service)

    context = AppContext(
        config=config,
        settings=settings,
        settings_store=settings_store,
        paths=paths,
        resources=resources,
        database_interpreter=database_interpreter,
        book_repository=book_repository,
        tag_repository=tag_repository,
        archive_extraction_pool=archive_extraction_pool,
        archive_image_service=archive_image_service,
        reader_session_service=reader_session_service,
        pdf_image_service=pdf_image_service,
        archive_warmup_coordinator=archive_warmup_coordinator,
        library_service=library_service,
        task_service=task_service,
        cache_service=cache_service,
        hash_service=hash_service,
        tag_service=tag_service,
        import_service=import_service,
        library_maintenance_coordinator=library_maintenance_coordinator,
        library_maintenance_service=library_maintenance_service,
        export_service=export_service,
        storage_migration_service=storage_migration_service,
        storage_validation_service=storage_validation_service,
        storage_recovery_service=storage_recovery_service,
        thumbnail_service=thumbnail_service,
        thumbnail_renderer=thumbnail_renderer,
        hidden_space_service=hidden_space_service,
        main_window_viewmodel=main_window_viewmodel,
        shelf_viewmodel=shelf_viewmodel,
        settings_viewmodel=settings_viewmodel,
        tag_management_viewmodel=tag_management_viewmodel,
        storage_startup_notice=startup.notice,
        library_maintenance_recovery_conflicts=bool(maintenance_recovery.conflicts),
    )
    # The settings panel renders a live "used / budget" label for the disk
    # pool; provide it a thin lambda so the viewmodel can poll the current
    # strategy object even after a runtime cache-strategy switch.
    settings_viewmodel.set_archive_pool_bytes_provider(lambda: context.archive_extraction_pool.current_bytes)
    # ...and let the pool say when that value changed, so the label follows
    # caching live instead of only being correct just after a rebuild, a
    # settings edit, a manual clear, or a finished audit. The pool reports from
    # a worker thread; the bridge is what makes the hop to the GUI thread safe.
    context.archive_pool_usage_bridge = ArchivePoolUsageBridge()
    context.archive_pool_usage_bridge.usage_changed.connect(
        lambda _usage: settings_viewmodel.refresh_archive_pool_usage()
    )
    context.attach_archive_pool_usage_bridge()
    # Hook user-driven cache actions back into the live services. Owning the
    # connection in AppContext keeps the viewmodel UI-only and makes the side
    # effects (resize/clear) easy to find from one place.
    settings_viewmodel.cache_budgets_changed.connect(context.apply_cache_settings)
    settings_viewmodel.archive_open_limits_changed.connect(context.apply_archive_open_limits)
    settings_viewmodel.clear_archive_pool_requested.connect(context.clear_archive_extraction_pool)
    settings_viewmodel.import_integrity_changed.connect(
        lambda: context.import_service.set_verify_imported_file_integrity(
            context.settings_store.load().verify_imported_file_integrity
        )
    )
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
    max_bytes = settings.archive_extraction_pool_gb * 1024 * 1024 * 1024
    logger.debug("Creating archive extraction cache strategy=%s max_bytes=%d", strategy.value, max_bytes)
    if strategy == ArchiveCacheStrategy.HIDDEN_IMAGE_FILES:
        return HiddenImageExtractionPool(
            paths.resolve(WritableLocation.CACHE, ".archive_image_pages"), max_bytes=max_bytes
        )
    return ArchiveExtractionPool(
        paths.resolve(WritableLocation.CACHE, ".archive_zip_bundles"), max_bytes=max_bytes
    )


def _create_database_interpreter(paths: PathService) -> DatabaseInterpreter:
    logger.debug("Creating database interpreter at %s", paths.paths.database / "joyread.sqlite3")
    database = DatabaseInterpreter(paths.paths.database / "joyread.sqlite3")
    database.execute(apply_migrations, DatabasePriority.CRITICAL)
    return database


def _create_sqlite_book_repository(database: DatabaseInterpreter, paths: PathService) -> SqliteBookRepository:
    return SqliteBookRepository(
        database,
        resolver=paths.resolver,
        managed_books_root=paths.paths.books,
        thumbnails_root=paths.paths.thumbnails,
    )


def _create_library_maintenance_service(
    paths: PathService,
    database: DatabaseInterpreter,
    hash_service: HashService,
    archive_service: ArchiveImageService,
    thumbnail_service: ThumbnailService,
    extraction_cache: ArchiveExtractionCache,
    archive_limits: ArchiveOpenLimits,
    coordinator: LibraryMaintenanceCoordinator,
    pdf_service: PdfImageService,
) -> LibraryMaintenanceService:
    return LibraryMaintenanceService(
        paths,
        database,
        hash_service,
        archive_service,
        archive_limits=archive_limits,
        extraction_cache=extraction_cache,
        invalidate_file_cache=thumbnail_service.invalidate_file_cache,
        coordinator=coordinator,
        pdf_service=pdf_service,
    )


def _archive_open_limits_from_settings(settings: AppSettings) -> ArchiveOpenLimits:
    """Translate persisted settings once at the application composition root."""

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
            if settings.archive_max_source_size_enabled
            else None
        ),
        max_extracted_item_bytes=resource_limit(settings.archive_max_extracted_item_gb, GIB),
        max_operation_bytes=resource_limit(settings.archive_max_operation_data_gb, GIB),
        max_image_pixels=resource_limit(settings.archive_max_image_megapixels, MEGAPIXEL),
        external_command_timeout_seconds=resource_limit(
            settings.archive_external_command_timeout_seconds,
            1,
        ),
    )
