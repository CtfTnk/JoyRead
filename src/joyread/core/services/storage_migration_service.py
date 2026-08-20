"""Filesystem operations behind Move / Reset of the JoyRead library folder.

The library is the movable storage root. *Move* copies the current library into
a fresh ``<parent>/JoyRead-Library`` and re-points settings at it; *Reset*
wipes the current root and lets the app rebuild an empty one. Both validate the
result through :class:`StorageValidationService` before any destructive step.

Config (``settings.json``) and Logs live in the external support root and are
deliberately untouched by these operations, so settings survive a move and
recovery can still read them when a library is unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
import shutil
from time import perf_counter
from uuid import uuid4

from joyread.core.operation_context import bind_operation, create_operation
from joyread.core.services.storage_validation_service import StorageValidationService
from joyread.infrastructure.config.settings_store import SettingsStore
from joyread.infrastructure.config.storage_names import LIBRARY_DIRECTORY_NAME


logger = logging.getLogger(__name__)


class StorageMigrationError(Exception):
    """Raised when a Move/Reset cannot complete; old storage stays in use."""

    task_failure_kind = "controlled"


@dataclass(frozen=True)
class StorageMigrationResult:
    old_root: Path
    target_root: Path


class StorageMigrationService:
    def __init__(
        self,
        settings_store: SettingsStore,
        validation_service: StorageValidationService,
    ) -> None:
        self._settings_store = settings_store
        self._validation = validation_service

    def target_root_for(self, target_parent: Path) -> Path:
        return target_parent.expanduser().resolve() / LIBRARY_DIRECTORY_NAME

    def move_to_parent(self, old_root: Path, target_parent: Path) -> StorageMigrationResult:
        """Copy the current library into ``<parent>/JoyRead-Library`` and adopt it.

        The caller must close the database before invoking this so the copy is
        consistent. On any failure the staging copy is removed, settings are
        left unchanged, and the old root is preserved.
        """

        operation = create_operation("storage.move", category="storage")
        started = perf_counter()
        with bind_operation(operation):
            logger.info(
                "Library move started",
                extra={
                    "event": "storage.move.started",
                    "category": "storage",
                    "status": "started",
                },
            )
            try:
                result = self._move_to_parent_bound(old_root, target_parent)
            except StorageMigrationError as exc:
                logger.warning(
                    "Library move failed",
                    extra={
                        "event": "storage.move.failed",
                        "category": "storage",
                        "status": "failed",
                        "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                        "error_type": type(exc).__name__,
                        "reason": str(exc),
                    },
                )
                raise
            logger.info(
                "Library move finished",
                extra={
                    "event": "storage.move.finished",
                    "category": "storage",
                    "status": "finished",
                    "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                },
            )
            return result

    def _move_to_parent_bound(
        self,
        old_root: Path,
        target_parent: Path,
    ) -> StorageMigrationResult:
        """Perform a move while its public operation context is bound."""

        old_root = old_root.expanduser().resolve()
        parent = target_parent.expanduser().resolve()
        target_root = parent / LIBRARY_DIRECTORY_NAME
        logger.info("Move library: %s -> %s", old_root, target_root)

        if target_root == old_root:
            logger.info("Move library no-op: target equals current root")
            return StorageMigrationResult(old_root=old_root, target_root=old_root)

        if target_root.exists():
            raise StorageMigrationError(
                "A JoyRead-Library folder already exists in that location. "
                "Choose 'Select Existing Library' to use it, or remove it first."
            )
        if parent == old_root or parent.is_relative_to(old_root) or old_root.is_relative_to(target_root):
            raise StorageMigrationError(
                "The destination cannot be inside the current library folder."
            )

        staging = parent / f".{LIBRARY_DIRECTORY_NAME}.staging-{uuid4().hex}"
        if staging.exists():
            shutil.rmtree(staging)
        parent.mkdir(parents=True, exist_ok=True)

        try:
            if old_root.exists():
                logger.info("Copying current library into staging %s", staging)
                shutil.copytree(old_root, staging)
            else:
                staging.mkdir(parents=True, exist_ok=True)

            validation = self._validation.validate_full(staging)
            if not validation.ok:
                raise StorageMigrationError(
                    f"The copied library failed validation: {validation.message}"
                )

            staging.replace(target_root)
        except StorageMigrationError:
            _safe_rmtree(staging)
            raise
        except OSError as exc:
            _safe_rmtree(staging)
            logger.error(
                "Library move filesystem operation failed",
                exc_info=True,
                extra={
                    "event": "storage.move.filesystem_failed",
                    "category": "storage",
                    "status": "failed",
                    "error_type": type(exc).__name__,
                },
            )
            raise StorageMigrationError(f"Could not move the library: {exc}") from exc

        # Record the destination as known-good in the same write that adopts
        # it. The old root is deleted a few lines below, so a fallback still
        # pointing there would send startup recovery to a directory that no
        # longer exists -- and it is only corrected on the next clean launch,
        # which is exactly the launch that might need it. `validate_full`
        # above is what makes "known-good" a fact rather than an assumption.
        self._settings_store.update(
            storage_location=str(target_root),
            last_good_storage_location=str(target_root),
        )

        if old_root != target_root and old_root.exists():
            logger.info("Removing previous library root %s", old_root)
            _safe_rmtree(old_root)

        logger.info("Move library complete (root=%s)", target_root)
        return StorageMigrationResult(old_root=old_root, target_root=target_root)

    def reset_library(self, root: Path) -> None:
        """Erase the current library folder so the app can rebuild it empty.

        The directory structure and an empty database are recreated by the
        subsequent storage reload, not here.
        """

        operation = create_operation("storage.reset", category="storage")
        started = perf_counter()
        with bind_operation(operation):
            logger.info(
                "Library reset started",
                extra={
                    "event": "storage.reset.started",
                    "category": "storage",
                    "status": "started",
                },
            )
            try:
                root = root.expanduser().resolve()
                if root.exists():
                    shutil.rmtree(root)
                root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.error(
                    "Library reset failed",
                    exc_info=True,
                    extra={
                        "event": "storage.reset.failed",
                        "category": "storage",
                        "status": "failed",
                        "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                        "error_type": type(exc).__name__,
                    },
                )
                raise
            logger.info(
                "Library reset finished",
                extra={
                    "event": "storage.reset.finished",
                    "category": "storage",
                    "status": "finished",
                    "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                },
            )


def _safe_rmtree(path: Path) -> None:
    try:
        if path.exists():
            shutil.rmtree(path)
    except OSError as exc:  # pragma: no cover - best-effort cleanup.
        logger.warning("Could not remove %s during storage migration: %s", path, exc)
