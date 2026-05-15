"""Extraction backend discovery for archive formats that need helper tools."""

from __future__ import annotations

import logging
from dataclasses import dataclass
import os
from pathlib import Path
import platform
import shutil


SEVEN_ZIP_ENV_VAR = "JOYREAD_7ZIP_PATH"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractionBackend:
    name: str
    executable: str
    source: str
    supports_passwords: bool = True


class ExtractionBackendResolver:
    """Resolve JoyRead-owned and system archive extraction helpers.

    Bundled 7-Zip binaries are preferred because encrypted RAR/CBR extraction
    must work without assuming that the user's OS already has a compatible
    command-line backend installed.
    """

    def __init__(self, resource_root: str | Path | None = None) -> None:
        self._resource_root = Path(resource_root) if resource_root is not None else _default_resource_root()

    @property
    def resource_root(self) -> Path:
        return self._resource_root

    def seven_zip(self) -> ExtractionBackend | None:
        for candidate, source in self._seven_zip_candidates():
            if _is_executable_file(candidate):
                logger.debug("7-Zip backend resolved (source=%s)", source)
                return ExtractionBackend("7zip", str(candidate), source, supports_passwords=True)
        for name in ("7zz", "7z"):
            executable = shutil.which(name)
            if executable is not None:
                logger.debug("7-Zip backend resolved via PATH (%s)", name)
                return ExtractionBackend("7zip", executable, f"PATH:{name}", supports_passwords=True)
        logger.warning("No 7-Zip backend available; encrypted/expensive archives may fail")
        return None

    def unar(self) -> ExtractionBackend | None:
        executable = shutil.which("unar")
        if executable is None:
            return None
        logger.debug("unar backend resolved via PATH")
        return ExtractionBackend("unar", executable, "PATH:unar", supports_passwords=True)

    def bsdtar(self) -> ExtractionBackend | None:
        executable = shutil.which("bsdtar")
        if executable is None:
            return None
        logger.debug("bsdtar backend resolved via PATH")
        return ExtractionBackend("bsdtar", executable, "PATH:bsdtar", supports_passwords=False)

    def missing_message(self, *, encrypted: bool = False) -> str:
        attempted = [description for _path, description in self._seven_zip_candidates()]
        attempted.extend(["PATH:7zz", "PATH:7z", "PATH:unar"])
        if not encrypted:
            attempted.append("PATH:bsdtar")
        capability = "encrypted " if encrypted else ""
        return (
            f"RAR/CBR requires a working {capability}extraction backend. "
            f"Tried: {', '.join(attempted)}."
        )

    def _seven_zip_candidates(self) -> tuple[tuple[Path, str], ...]:
        candidates: list[tuple[Path, str]] = []
        platform_dir = self._resource_root / "7zip" / f"{_platform_key()}-{_architecture_key()}"
        for name in _seven_zip_executable_names():
            candidates.append((platform_dir / name, f"bundled:{platform_dir / name}"))

        override = os.environ.get(SEVEN_ZIP_ENV_VAR)
        if override:
            candidates.append((Path(override).expanduser(), f"{SEVEN_ZIP_ENV_VAR}:{override}"))
        return tuple(candidates)


def _default_resource_root() -> Path:
    return Path(__file__).resolve().parents[2] / "resources" / "extractors"


def _platform_key() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "darwin"
    if system.startswith("win"):
        return "windows"
    if system == "linux":
        return "linux"
    return system or "unknown"


def _architecture_key() -> str:
    machine = platform.machine().lower().replace("-", "_")
    if machine in {"amd64", "x64", "x86_64"}:
        return "x86_64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return machine or "unknown"


def _seven_zip_executable_names() -> tuple[str, ...]:
    if _platform_key() == "windows":
        return ("7zz.exe", "7z.exe")
    return ("7zz", "7z")


def _is_executable_file(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    if _platform_key() == "windows":
        return True
    return os.access(path, os.X_OK)
