"""The vault and its sealing jobs, as the Library database records them."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from joyread.core.vault import VaultPublicKey


#: Version of the vault record itself (key pair plus slots), independent of
#: the ``.jrl`` file format version.
VAULT_FORMAT_VERSION = 1


class VaultAlreadyExistsError(RuntimeError):
    """A Library has at most one vault; Reset and Erase removes it first."""


@dataclass(frozen=True)
class VaultRecord:
    vault_id: str
    format_version: int
    public_key: VaultPublicKey
    created_at: str
    updated_at: str


class VaultJobDirection(StrEnum):
    SEAL = "seal"
    UNSEAL = "unseal"


class VaultJobPhase(StrEnum):
    """How far a job got. Recovery after a crash resumes from here.

    The phases order the two irreversible steps -- repointing the database row
    and deleting the old file -- so that a crash between any two of them
    leaves exactly one file the row can trust.
    """

    #: Recorded with the hide or unhide itself. Nothing has been written.
    QUEUED = "queued"
    #: The new file is complete at ``target_path``; the row still names the old one.
    WRITTEN = "written"
    #: The row names the new file; the old one may still need deleting.
    COMMITTED = "committed"
    #: Stopped with ``error_code``. Retried on the next launch or unlock.
    FAILED = "failed"


@dataclass(frozen=True)
class VaultJob:
    """One queued seal or unseal. Paths are storage-relative, like ``book_files``."""

    job_id: str
    book_id: str
    file_id: str
    direction: VaultJobDirection
    phase: VaultJobPhase
    partial_path: str | None
    target_path: str | None
    previous_path: str | None
    expected_hash: str | None
    error_code: str | None
    attempts: int
    created_at: str
    updated_at: str


__all__ = [
    "VAULT_FORMAT_VERSION",
    "VaultAlreadyExistsError",
    "VaultJob",
    "VaultJobDirection",
    "VaultJobPhase",
    "VaultRecord",
]
