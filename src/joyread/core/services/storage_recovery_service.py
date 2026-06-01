"""Startup storage selection: first-run initialization and recovery.

Run once before the path service and database are built. It decides which
storage root the app will open this session and, when the configured library is
unavailable, falls back to the last-known-good root or the app's default —
never silently overwriting a user's library outside first-run initialization.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path

from joyread.core.services.storage_migration_service import StorageMigrationService
from joyread.core.services.storage_validation_service import StorageValidationService
from joyread.infrastructure.config.settings_store import AppSettings, SettingsStore


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StorageStartupResult:
    settings: AppSettings
    notice: str | None = None


class StorageRecoveryService:
    def __init__(
        self,
        settings_store: SettingsStore,
        validation_service: StorageValidationService,
        migration_service: StorageMigrationService,
    ) -> None:
        self._settings_store = settings_store
        self._validation = validation_service
        self._migration = migration_service

    def prepare(self) -> StorageStartupResult:
        """Resolve the storage root to use, updating settings as needed."""

        first_run = not self._settings_store.settings_path.exists()
        settings = self._settings_store.load()
        if first_run:
            logger.info("First run detected; initializing default library")
            return self._prepare_first_run(settings)
        return self._prepare_daily(settings)

    # -- first run ----------------------------------------------------------

    def _prepare_first_run(self, settings: AppSettings) -> StorageStartupResult:
        default_root = Path(settings.storage_location)
        database = self._validation.database_path(default_root)
        notice: str | None = None

        if database.is_file():
            # A library already lives at our default location. Reuse it if it
            # is compatible; otherwise overwrite it (first-run overwrite only
            # ever targets the app's own default root, never a chosen library).
            result = self._validation.validate_full(default_root)
            if not result.ok:
                logger.warning(
                    "First-run default library incompatible (%s); resetting: %s",
                    result.code,
                    result.message,
                )
                self._migration.reset_library(default_root)
                notice = (
                    "An incompatible JoyRead library was found and has been reset "
                    "to a new, empty library."
                )
        # When no database exists yet, the normal startup path will create an
        # empty one at this root.
        updated = self._settings_store.update(last_good_storage_location=str(default_root))
        return StorageStartupResult(updated, notice)

    # -- daily startup ------------------------------------------------------

    def _prepare_daily(self, settings: AppSettings) -> StorageStartupResult:
        current = settings.storage_location
        if self._validation.validate_lightweight(current).ok:
            if settings.last_good_storage_location != current:
                settings = self._settings_store.update(last_good_storage_location=current)
            return StorageStartupResult(settings, None)

        logger.warning("Configured storage at %s is unavailable; recovering", current)
        default_root = str(self._settings_store.default_storage_root)

        for candidate in self._fallback_candidates(settings, current, default_root):
            if self._validation.validate_lightweight(candidate).ok:
                updated = self._settings_store.update(
                    storage_location=candidate,
                    last_good_storage_location=candidate,
                )
                logger.info("Recovered storage by switching to %s", candidate)
                return StorageStartupResult(updated, self._switch_notice(current, candidate))

        # Last resort: start empty at the app's own default root. Reset it if it
        # exists but is broken so the app always opens a usable library.
        if not self._validation.validate_lightweight(default_root).ok:
            if self._validation.database_path(Path(default_root)).exists():
                logger.warning("Default library at %s is unusable; resetting it", default_root)
                self._migration.reset_library(Path(default_root))
        updated = self._settings_store.update(storage_location=default_root)
        return StorageStartupResult(updated, self._empty_notice(current, default_root))

    def _fallback_candidates(
        self, settings: AppSettings, current: str, default_root: str
    ) -> list[str]:
        candidates: list[str] = []
        last_good = settings.last_good_storage_location
        if last_good and last_good != current:
            candidates.append(last_good)
        if default_root != current and default_root not in candidates:
            candidates.append(default_root)
        return candidates

    def _switch_notice(self, current: str, target: str) -> str:
        return (
            f"Your library at\n{current}\nwas unavailable, so JoyRead switched to\n{target}."
        )

    def _empty_notice(self, current: str, target: str) -> str:
        return (
            f"Your library at\n{current}\nwas unavailable and no backup could be opened, "
            f"so JoyRead started with an empty library at\n{target}."
        )
