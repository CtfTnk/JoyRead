"""Application dependency container."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from time import perf_counter

from joyread.core.archive import ArchiveImageService, ArchiveOpenLimits
from joyread.core.models.cache import normalize_archive_cache_strategy
from joyread.core.models.import_policy import normalize_canonical_import_policy
from joyread.core.reader import ReaderSessionService
from joyread.core.services.archive_extraction_pool import ArchiveExtractionCache
from joyread.app.archive_pool_usage_bridge import ArchivePoolUsageBridge
from joyread.app.reader_runtime import (
    ReaderRuntime,
    archive_open_limits_from_settings as _archive_open_limits_from_settings,
    create_archive_extraction_cache as _create_archive_extraction_cache,
    create_reader_runtime,
)
from joyread.app.library_runtime import LibraryRuntime, create_library_runtime
from joyread.app.runtime_access import RuntimeAccess
from joyread.app.path_issue_bridge import PathIssueBridge
from joyread.core.services.cache_service import CacheService
from joyread.core.services.hash_service import HashService
from joyread.core.services.import_service import ImportService
from joyread.core.services.path_issue_service import PathIssueService
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
from joyread.infrastructure.pdf_document_thread import shutdown_pdf_thread
from joyread.infrastructure.pdf_image_service import PdfImageService
from joyread.infrastructure.thumbnail_renderer import QtThumbnailRenderer
from joyread.core.services.thumbnail_service import ThumbnailService
from joyread.infrastructure.config.app_config import AppConfig
from joyread.infrastructure.config.settings_store import (
    AppSettings,
    SettingsStore,
    create_environment_settings_store,
)
from joyread.infrastructure.i18n import locale_service
from joyread.infrastructure.database import DatabaseInterpreter
from joyread.infrastructure.filesystem.path_service import PathService
from joyread.infrastructure.filesystem.windows_long_paths import WindowsLongPathCapability
from joyread.infrastructure.logging import log_event, operation_scope
from joyread.infrastructure.resources.resource_loader import ResourceLoader


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
class AppContext(RuntimeAccess):
    """Application lifecycle and storage-transition coordinator.

    ReaderRuntime and LibraryRuntime own their services. The inherited accessors
    keep existing callers working while they move to the narrow runtime ports.
    """

    config: AppConfig
    settings: AppSettings
    settings_store: SettingsStore
    paths: PathService
    reader_runtime: ReaderRuntime
    library_runtime: LibraryRuntime
    storage_migration_service: StorageMigrationService
    storage_validation_service: StorageValidationService
    storage_recovery_service: StorageRecoveryService
    #: Carries live pool usage from caching workers to the settings page.
    archive_pool_usage_bridge: ArchivePoolUsageBridge | None = None
    #: Queues worker-side path diagnostics onto the GUI thread.
    path_issue_bridge: PathIssueBridge | None = None
    # Populated when startup recovery had to fall back to another library;
    # the main window shows it once on launch.
    storage_startup_notice: str | None = None
    library_maintenance_recovery_conflicts: bool = False
    #: Set once a storage transition passes its commit point, where terminal
    #: services are released. Every outcome after that has to rebuild, even one
    #: that never touched storage.
    storage_rebuild_required: bool = False

    def close(self) -> None:
        started = perf_counter()
        failures: list[tuple[str, Exception]] = []
        log_event(
            logger,
            logging.INFO,
            "app_context.close.started",
            "Application services shutdown started",
            category="shutdown",
            status="started",
        )

        def close_component(name: str, callback) -> None:  # noqa: ANN001
            try:
                callback()
            except Exception as exc:  # Finish the remaining teardown stages.
                failures.append((name, exc))
                log_event(
                    logger,
                    logging.ERROR,
                    "app_context.component_close.failed",
                    "Application service failed during shutdown",
                    category="shutdown",
                    status="failed",
                    component=name,
                    error_type=type(exc).__name__,
                    reason=str(exc),
                    exc_info=True,
                )

        if self.archive_warmup_coordinator is not None:
            close_component("archive_warmup", self.archive_warmup_coordinator.close)
        if self.path_issue_bridge is not None:
            close_component("path_issue_bridge", self.path_issue_bridge.detach)
        close_component("task_service", self.task_service.shutdown)
        if self.thumbnail_service is not None:
            close_component("thumbnail_service", self.thumbnail_service.close)
        # Task shutdown stops accepting new work first. PDF shutdown then seals
        # its own queue behind any render already accepted and joins when that
        # queue drains, without destroying a still-running QThread on timeout.
        close_component("pdf_thread", shutdown_pdf_thread)
        close_component("database", self.database_interpreter.close)
        log_event(
            logger,
            logging.WARNING if failures else logging.INFO,
            "app_context.close.finished",
            "Application services shutdown finished",
            category="shutdown",
            status="completed_with_errors" if failures else "finished",
            duration_ms=round((perf_counter() - started) * 1000.0, 3),
            failed_count=len(failures),
        )

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

        Reached when the drain times out. Storage was never touched and nothing
        terminal has run, so the application goes back to what it was -- but
        "what it was" includes the warmup coordinator, which the quiesce left
        holding a cancelled task that will never report. Resetting it here for
        the same reason :meth:`commit_storage_transition` does: without it,
        ``_active_key`` stays set and no later warmup can ever start.
        """

        if self.archive_warmup_coordinator is not None:
            self.archive_warmup_coordinator.reset()
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

    def reload_settings(self) -> AppSettings:
        """Refresh the shared settings snapshot and return it.

        Views used to do ``context.settings = context.settings_store.load()``
        inline whenever they needed current values. That put the assignment in
        three places with no single point to hook, so any future refresh a
        dependent service needs would have to be remembered at each one.

        This only refreshes the snapshot. Applying a *changed* setting to live
        services stays with :meth:`apply_cache_settings` and
        :meth:`apply_archive_open_limits`, which the settings page reaches
        through its own signals -- keeping the two concerns apart is what stops
        a routine read from tearing down and rebuilding caches.
        """

        self.settings = self.settings_store.load()
        return self.settings

    def apply_archive_depth_settings(self) -> None:
        """Compatibility entrypoint for older callers of depth-only settings."""

        self.apply_archive_open_limits()

    def apply_archive_open_limits(self) -> None:
        with operation_scope(logger, "settings.archive_limits.apply", category="settings"):
            self.settings = self.settings_store.load()
            limits = _archive_open_limits_from_settings(self.settings)
            self.thumbnail_service.set_archive_open_limits(limits)
            self.import_service.set_archive_open_limits(limits)
            self.library_maintenance_service.set_archive_open_limits(limits)
            self.import_service.set_verify_imported_file_integrity(
                self.settings.verify_imported_file_integrity
            )
            self.import_service.set_canonical_import_policy(
                normalize_canonical_import_policy(self.settings.canonical_import_policy)
            )
            if self.archive_warmup_coordinator is not None:
                self.archive_warmup_coordinator.invalidate()
            self.shelf_viewmodel.invalidate_detail_thumbnail_source()

    def reload_storage_from_settings(self) -> None:
        with operation_scope(logger, "storage.services.reload", category="storage"):
            self._reload_storage_from_settings_bound()

    def _reload_storage_from_settings_bound(self) -> None:
        # Stage the entire graph before publishing any new service. A failed
        # database migration or maintenance replay must release its partial
        # graph and leave the existing runtime identities intact.
        settings = self.settings_store.load()
        log_event(
            logger,
            logging.INFO,
            "storage.services.rebuild",
            "Rebuilding storage-rooted services",
            category="storage",
            status="started",
        )
        paths = _create_path_service(self.config, self.settings_store, settings)
        paths.ensure_directories()
        pool = _create_archive_extraction_cache(paths, settings, self.path_issue_service)
        archive_service = ArchiveImageService(extraction_pool=pool)
        session_service = ReaderSessionService(
            archive_service, self.pdf_image_service, path_issue_service=self.path_issue_service
        )
        reader = replace(
            self.reader_runtime,
            paths=paths,
            archive_extraction_pool=pool,
            archive_image_service=archive_service,
            reader_session_service=session_service,
            cache_service=self.reader_runtime.cache_service.with_archive_pool(pool),
        )
        library = create_library_runtime(reader, self.config, settings, self.settings_store)

        old_library = self.library_runtime
        shelf = old_library.shelf_viewmodel
        settings_vm = old_library.settings_viewmodel
        tag_vm = old_library.tag_management_viewmodel
        main_vm = old_library.main_window_viewmodel
        try:
            shelf.replace_services(library.library_service, library.thumbnail_service, library.tag_service)
            settings_vm.set_hidden_space_service(library.hidden_space_service)
            tag_vm.replace_service(library.tag_service)
        except BaseException:
            library.close()
            raise
        library.shelf_viewmodel = shelf
        library.settings_viewmodel = settings_vm
        library.tag_management_viewmodel = tag_vm
        library.main_window_viewmodel = main_vm

        self.reader_runtime = reader
        self.library_runtime = library
        self.settings = settings
        self.paths = paths
        self.archive_warmup_coordinator.replace_session_service(session_service)
        self.cache_service.apply_cache_budgets(
            reader_page_cache_bytes=settings.reader_page_cache_mb * 1024 * 1024,
            thumbnail_cache_bytes=settings.thumbnail_cache_mb * 1024 * 1024,
        )
        self.attach_archive_pool_usage_bridge()
        settings_vm.set_storage_location(settings.storage_location)
        settings_vm.set_archive_pool_bytes_provider(lambda: self.archive_extraction_pool.current_bytes)
        self.library_maintenance_recovery_conflicts = library.maintenance_recovery_conflicts
        self._refresh_settings_pool_usage()
        self._sync_runtime_ownership()
        old_library.close()

    def apply_cache_settings(self) -> None:
        """Push the current settings into every cache that owns a live budget.

        Called when the user edits a value under the Cache group on the
        Settings page. ``CacheService.apply_cache_budgets`` covers the shared
        reader page cache, thumbnail cache, and disk extraction pool.
        """

        with operation_scope(logger, "settings.cache.apply", category="settings"):
            self._apply_cache_settings_bound()

    def _apply_cache_settings_bound(self) -> None:
        """Apply cache settings while the public operation is already bound."""

        previous_strategy = normalize_archive_cache_strategy(self.settings.archive_cache_strategy)
        settings = self.settings_store.load()
        next_strategy = normalize_archive_cache_strategy(settings.archive_cache_strategy)
        logger.info(
            "Applying cache settings: strategy=%s->%s reader_mb=%s pool_gb=%s thumbnail_mb=%s",
            previous_strategy.value,
            next_strategy.value,
            settings.reader_page_cache_mb,
            settings.archive_extraction_pool_gb,
            settings.thumbnail_cache_mb,
        )
        if next_strategy != previous_strategy:
            # Stage the new strategy's dependent services before retiring the
            # old pool. A failure leaves the current Reader and Library usable.
            pool = _create_archive_extraction_cache(self.paths, settings, self.path_issue_service)
            archive = ArchiveImageService(extraction_pool=pool)
            session = ReaderSessionService(
                archive, self.pdf_image_service, path_issue_service=self.path_issue_service
            )
            reader_cache = self.reader_runtime.cache_service.with_archive_pool(pool)
            cache = CacheService(
                pool,
                settings.reader_page_cache_mb * 1024 * 1024,
                settings.thumbnail_cache_mb * 1024 * 1024,
                self.config.cover_index_max_items,
                reader_caches=reader_cache,
                library_caches=self.library_runtime.library_caches,
            )
            old_thumbnail = self.thumbnail_service
            thumbnail = None
            shelf_rebind_attempted = False
            try:
                thumbnail = ThumbnailService(
                    self.paths,
                    archive,
                    cache,
                    session,
                    nested_archive_max_depth=settings.nested_archive_max_depth,
                    archive_global_file_max_depth=settings.archive_global_file_max_depth,
                    archive_limits=_archive_open_limits_from_settings(settings),
                    thumbnail_renderer=self.thumbnail_renderer or QtThumbnailRenderer(),
                )
                importer = ImportService(
                    self.paths,
                    self.database_interpreter,
                    archive,
                    self.hash_service,
                    settings.hash_algorithm,
                    path_issue_service=self.path_issue_service,
                    tag_service=self.tag_service,
                    archive_limits=_archive_open_limits_from_settings(settings),
                    verify_imported_file_integrity=settings.verify_imported_file_integrity,
                    maintenance_coordinator=self.library_maintenance_coordinator,
                    pdf_service=self.pdf_image_service,
                    canonical_import_policy=normalize_canonical_import_policy(
                        settings.canonical_import_policy
                    ),
                )
                maintenance = _create_library_maintenance_service(
                    self.paths,
                    self.database_interpreter,
                    self.hash_service,
                    archive,
                    thumbnail,
                    pool,
                    _archive_open_limits_from_settings(settings),
                    self.library_maintenance_coordinator,
                    self.pdf_image_service,
                )
                shelf_rebind_attempted = True
                self.shelf_viewmodel.replace_services(self.library_service, thumbnail, self.tag_service)
            except BaseException:
                if shelf_rebind_attempted:
                    try:
                        self.shelf_viewmodel.replace_services(
                            self.library_service, old_thumbnail, self.tag_service
                        )
                    except Exception:
                        logger.error("Shelf rollback after cache rebuild failure failed", exc_info=True)
                if thumbnail is not None:
                    thumbnail.close()
                raise

            old_pool = self.archive_extraction_pool
            self.archive_extraction_pool = pool
            self.archive_image_service = archive
            self.reader_session_service = session
            self.reader_runtime.cache_service = reader_cache
            self.cache_service = cache
            self.thumbnail_service = thumbnail
            self.import_service = importer
            self.library_maintenance_service = maintenance
            self.archive_warmup_coordinator.replace_session_service(session)
            self.attach_archive_pool_usage_bridge()
            self.settings_viewmodel.set_archive_pool_bytes_provider(lambda: self.archive_extraction_pool.current_bytes)
            cache.apply_cache_budgets(
                reader_page_cache_bytes=settings.reader_page_cache_mb * 1024 * 1024,
                thumbnail_cache_bytes=settings.thumbnail_cache_mb * 1024 * 1024,
            )
            try:
                old_thumbnail.close()
                old_pool.clear()
            except Exception:
                logger.warning("Retired archive cache cleanup failed", exc_info=True)
        else:
            self.cache_service.apply_cache_budgets(
                reader_page_cache_bytes=settings.reader_page_cache_mb * 1024 * 1024,
                thumbnail_cache_bytes=settings.thumbnail_cache_mb * 1024 * 1024,
                archive_extraction_pool_bytes=settings.archive_extraction_pool_gb * 1024 * 1024 * 1024,
            )
        self.settings = settings
        self._sync_runtime_ownership()
        self._refresh_settings_pool_usage()

    def clear_archive_extraction_pool(self) -> None:
        """User-triggered "Clear archive cache" button hook."""

        bytes_before = self.archive_extraction_pool.current_bytes
        with operation_scope(
            logger,
            "cache.archive_pool.clear",
            category="cache",
            fields={"bytes": bytes_before},
        ):
            self.archive_extraction_pool.clear()
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

    def _sync_runtime_ownership(self) -> None:
        """Refresh app-level snapshots after a storage or cache transition.

        Service assignments already target their runtime owner through
        RuntimeAccess. Only the app-level path and preference snapshots need
        publication here.
        """

        self.reader_runtime.paths = self.paths
        self.library_runtime.paths = self.paths
        self.reader_runtime.preferences.apply(self.settings)


def create_app_context(
    recovery_prompt: RecoveryPrompt | None = None,
    *,
    config: AppConfig | None = None,
    settings_store: SettingsStore | None = None,
) -> AppContext:
    config = config or AppConfig()
    settings_store = settings_store or create_environment_settings_store(
        config.app_name,
        config.app_author,
    )
    with operation_scope(
        logger,
        "app_context.create",
        category="startup",
        fields={"worker_count": config.max_background_workers},
    ):
        return _create_app_context_bound(config, settings_store, recovery_prompt)


def _create_app_context_bound(
    config: AppConfig,
    settings_store: SettingsStore,
    recovery_prompt: RecoveryPrompt | None,
) -> AppContext:
    # Resolve the storage root before anything is built: first-run init,
    # daily health check, and recovery all happen here so the rest of the
    # graph is wired against a known-usable library.
    path_issue_service = PathIssueService(WindowsLongPathCapability())
    storage_validation_service = StorageValidationService(path_issue_service=path_issue_service)
    storage_migration_service = StorageMigrationService(settings_store, storage_validation_service)
    storage_recovery_service = StorageRecoveryService(
        settings_store, storage_validation_service, storage_migration_service
    )
    # Recovery can display a dialog before the paths/database exist. This
    # read-only preference lookup preserves the service's first-run sentinel.
    resources = ResourceLoader()
    locale_service.init(
        resources.locale_dir(), settings_store.locales_dir,
        settings_store.read_language_preference(),
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
    reader_runtime = create_reader_runtime(
        config,
        settings,
        settings_store,
        paths=paths,
        resources=resources,
        path_issue_service=path_issue_service,
    )
    try:
        library_runtime = create_library_runtime(reader_runtime, config, settings, settings_store)
    except BaseException:
        try:
            reader_runtime.close()
        except Exception:
            logger.error("Reader cleanup after Library construction failure failed", exc_info=True)
        raise
    settings_viewmodel = library_runtime.settings_viewmodel
    settings_viewmodel.prefetch_window_changed.connect(reader_runtime.preferences.refresh)
    settings_viewmodel.archive_open_limits_changed.connect(reader_runtime.preferences.refresh)
    reader_runtime.preferences.prefetch_changed.connect(
        lambda: settings_viewmodel.sync_page_prefetch(
            reader_runtime.preferences.page_prefetch_before,
            reader_runtime.preferences.page_prefetch_after,
        )
    )

    context = AppContext(
        config=config,
        settings=settings,
        settings_store=settings_store,
        paths=paths,
        reader_runtime=reader_runtime,
        library_runtime=library_runtime,
        storage_migration_service=storage_migration_service,
        storage_validation_service=storage_validation_service,
        storage_recovery_service=storage_recovery_service,
        storage_startup_notice=startup.notice,
        library_maintenance_recovery_conflicts=library_runtime.maintenance_recovery_conflicts,
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
    context.path_issue_bridge = PathIssueBridge()
    context.path_issue_bridge.issue_detected.connect(reader_runtime.path_issue_viewmodel.present)
    context.path_issue_bridge.attach(path_issue_service)
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
    settings_viewmodel.canonical_import_policy_changed.connect(
        lambda: context.import_service.set_canonical_import_policy(
            normalize_canonical_import_policy(
                context.settings_store.load().canonical_import_policy
            )
        )
    )
    log_event(
        logger,
        logging.INFO,
        "app_context.services.ready",
        "Application service graph is ready",
        category="startup",
        status="finished",
        worker_count=config.max_background_workers,
        strategy=settings.archive_cache_strategy,
    )
    return context


def _create_path_service(config: AppConfig, settings_store: SettingsStore, settings: AppSettings) -> PathService:
    return PathService(
        config.app_name,
        config.app_author,
        storage_root=Path(settings.storage_location),
        support_root=settings_store.support_root,
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
