"""Startup storage selection: first-run initialization and recovery.

Run once before the path service and database are built. It decides which
storage root the app will open this session and, when the configured library is
unavailable, either adopts a user-selected library, initializes the app default,
or exits without changing settings.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import logging
from pathlib import Path
from time import perf_counter

from joyread.core.operation_context import bind_operation, create_operation
from joyread.core.services.storage_migration_service import StorageMigrationService
from joyread.core.services.storage_validation_service import StorageValidationService
from joyread.infrastructure.config.settings_store import AppSettings, SettingsStore


logger = logging.getLogger(__name__)


class StorageRecoveryDecision(StrEnum):
    """User's choice when the configured library is unavailable at startup."""

    INITIALIZE = "initialize"
    SELECT = "select"
    QUIT = "quit"


@dataclass(frozen=True)
class StorageRecoveryPromptResult:
    decision: StorageRecoveryDecision
    selected_root: str | None = None

    @classmethod
    def initialize(cls) -> StorageRecoveryPromptResult:
        return cls(StorageRecoveryDecision.INITIALIZE)

    @classmethod
    def select(cls, selected_root: str) -> StorageRecoveryPromptResult:
        return cls(StorageRecoveryDecision.SELECT, selected_root)

    @classmethod
    def quit(cls) -> StorageRecoveryPromptResult:
        return cls(StorageRecoveryDecision.QUIT)


class StorageRecoveryCancelled(Exception):
    """Raised when the user closes startup recovery instead of choosing a library."""

    task_failure_kind = "cancelled"


# Called when the configured storage is unavailable. Receives the configured
# location and the current validation/recovery message. Returns Initialize,
# Select(selected_root), or Quit. Kept Qt-free so the core service stays
# UI-agnostic and testable.
RecoveryPrompt = Callable[[str, str], StorageRecoveryPromptResult]


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

    def prepare(self, prompt: RecoveryPrompt | None = None) -> StorageStartupResult:
        """Resolve the storage root to use, updating settings as needed.

        ``prompt`` is consulted on daily startup when the configured library is
        unavailable, letting the user initialize the default library, select an
        existing library, or quit. When it is ``None`` (tests, headless runs),
        recovery falls back automatically.
        """

        operation = create_operation("storage.recovery", category="storage")
        started = perf_counter()
        with bind_operation(operation):
            logger.info(
                "Storage startup resolution started",
                extra={
                    "event": "storage.recovery.started",
                    "category": "storage",
                    "status": "started",
                },
            )
            try:
                result = self._prepare_bound(prompt)
            except StorageRecoveryCancelled:
                logger.info(
                    "Storage startup resolution cancelled",
                    extra={
                        "event": "storage.recovery.cancelled",
                        "category": "storage",
                        "status": "cancelled",
                        "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                    },
                )
                raise
            except Exception as exc:
                logger.error(
                    "Storage startup resolution failed",
                    exc_info=True,
                    extra={
                        "event": "storage.recovery.failed",
                        "category": "storage",
                        "status": "failed",
                        "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                        "error_type": type(exc).__name__,
                    },
                )
                raise
            logger.info(
                "Storage startup resolution finished",
                extra={
                    "event": "storage.recovery.finished",
                    "category": "storage",
                    "status": "finished",
                    "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                    "outcome": "recovered" if result.notice else "configured",
                },
            )
            return result

    def _prepare_bound(self, prompt: RecoveryPrompt | None) -> StorageStartupResult:
        """Resolve startup storage while the public operation is bound."""

        first_run = not self._settings_store.settings_path.exists()
        settings = self._settings_store.load()
        if first_run:
            logger.info("First run detected; initializing default library")
            return self._prepare_first_run(settings)
        return self._prepare_daily(settings, prompt)

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

    def _prepare_daily(
        self, settings: AppSettings, prompt: RecoveryPrompt | None
    ) -> StorageStartupResult:
        current = settings.storage_location
        result = self._validation.validate_lightweight(current)
        if result.ok:
            if settings.last_good_storage_location != current:
                settings = self._settings_store.update(last_good_storage_location=current)
            return StorageStartupResult(settings, None)

        logger.warning("Configured storage at %s is unavailable; recovering", current)

        if prompt is not None:
            return self._prepare_prompted_recovery(current, result.message, prompt)

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

    def _prepare_prompted_recovery(
        self,
        current: str,
        message: str,
        prompt: RecoveryPrompt,
    ) -> StorageStartupResult:
        while True:
            decision = prompt(current, message)
            if decision.decision == StorageRecoveryDecision.QUIT:
                logger.info("Storage recovery cancelled by user for %s", current)
                raise StorageRecoveryCancelled

            if decision.decision == StorageRecoveryDecision.INITIALIZE:
                logger.info("Storage recovery: user chose initialize")
                return self._initialize_default_library(current)

            if decision.decision == StorageRecoveryDecision.SELECT:
                if not decision.selected_root:
                    message = "No folder was selected."
                    continue
                selected_root = str(Path(decision.selected_root).expanduser().resolve())
                result = self._validation.validate_full(Path(selected_root))
                if result.ok:
                    updated = self._settings_store.update(
                        storage_location=selected_root,
                        last_good_storage_location=selected_root,
                    )
                    logger.info("Storage recovery: user selected %s", selected_root)
                    return StorageStartupResult(updated, self._switch_notice(current, selected_root))
                logger.warning(
                    "Storage recovery select rejected (%s): %s",
                    result.code,
                    result.message,
                )
                message = (
                    "That folder cannot be used as a JoyRead library.\n\n"
                    f"Selected location:\n{selected_root}\n\n{result.message}"
                )
                continue

            message = "Choose Initialize or Select to continue."

    def _initialize_default_library(self, current: str) -> StorageStartupResult:
        default_root = str(self._settings_store.default_storage_root)
        default_path = Path(default_root)
        result = self._validation.validate_lightweight(default_path)
        if not result.ok and self._validation.database_path(default_path).exists():
            logger.warning(
                "Default library at %s is unusable; resetting it: %s",
                default_root,
                result.message,
            )
            self._migration.reset_library(default_path)
        updated = self._settings_store.update(
            storage_location=default_root,
            last_good_storage_location=default_root,
        )
        return StorageStartupResult(updated, self._initialize_notice(current, default_root))

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

    def _initialize_notice(self, current: str, target: str) -> str:
        return (
            f"Your library at\n{current}\nwas unavailable, so JoyRead initialized "
            f"a library at\n{target}."
        )
