"""Move JoyRead's storage root while keeping support config stable."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import shutil
from uuid import uuid4

from joyread.infrastructure.config.settings_store import SettingsStore


@dataclass(frozen=True)
class StorageMigrationResult:
    old_root: Path
    new_root: Path
    old_backup_root: Path | None
    replaced_destination_backup: Path | None


class StorageMigrationService:
    def __init__(self, settings_store: SettingsStore) -> None:
        self._settings_store = settings_store

    def move_storage_location(self, old_root: Path, new_root: Path) -> StorageMigrationResult:
        old_root = old_root.expanduser().resolve()
        new_root = new_root.expanduser().resolve()
        if old_root == new_root:
            self._settings_store.update(storage_location=str(new_root))
            return StorageMigrationResult(old_root, new_root, None, None)
        if new_root.is_relative_to(old_root):
            raise ValueError("New JoyRead storage location cannot be inside the current storage location.")

        _assert_writable_destination(new_root)
        staging = new_root.with_name(f"{new_root.name}.tmp-{uuid4().hex}")
        if staging.exists():
            shutil.rmtree(staging)

        if old_root.exists():
            shutil.copytree(old_root, staging)
        else:
            staging.mkdir(parents=True, exist_ok=True)

        replaced_backup = None
        if new_root.exists() and any(new_root.iterdir()):
            replaced_backup = _backup_path(new_root, "replaced")
            shutil.move(str(new_root), str(replaced_backup))
        elif new_root.exists():
            new_root.rmdir()

        shutil.move(str(staging), str(new_root))

        old_backup = None
        if old_root.exists():
            old_backup = _backup_path(old_root, "backup")
            shutil.move(str(old_root), str(old_backup))

        self._settings_store.update(storage_location=str(new_root))
        return StorageMigrationResult(old_root, new_root, old_backup, replaced_backup)


def _assert_writable_destination(path: Path) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    probe = parent / f".joyread-write-test-{uuid4().hex}"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()


def _backup_path(path: Path, label: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return path.with_name(f"{path.name}.{label}-{stamp}-{uuid4().hex[:8]}")
