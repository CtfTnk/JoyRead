"""Public archive model types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ArchivePasswordRequest:
    archive_path: str
    archive_format: str
    attempt: int
    reason: str | None = None


PasswordProvider = Callable[[ArchivePasswordRequest], str | None]
