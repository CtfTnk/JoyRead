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


#: ``error_code`` of an unseal job that a later hide cancelled. Not a failure
#: to retry: the worker deletes what the job wrote and drops it.
CANCELLED_ERROR_CODE = "cancelled"


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


class BookSealState(StrEnum):
    """What a hidden book's outstanding job means to someone looking at it.

    Lives with the job records, not the sealing service, so shelf widgets on
    the Reader's import path can name it without pulling in the database.
    """

    ENCRYPTING = "encrypting"
    DECRYPTING = "decrypting"
    #: The book stays hidden but is still a plaintext file.
    ENCRYPT_FAILED = "encrypt_failed"
    #: The book stays hidden and sealed; it can be unhidden again later.
    DECRYPT_FAILED = "decrypt_failed"


def book_seal_states(jobs: dict[str, VaultJob]) -> dict[str, BookSealState]:
    """Each book's state from its outstanding job (``jobs_by_book``).

    A committed job is still shown as in progress: its book is sealed (or
    decrypted) but its cleanup -- deleting the other copy -- is not done yet.
    A cancelled unseal is the worker tidying a decrypted copy nobody wants;
    its book is simply sealed, so it has no state to show.
    """

    states: dict[str, BookSealState] = {}
    for book_id, job in jobs.items():
        sealing = job.direction is VaultJobDirection.SEAL
        if job.phase is VaultJobPhase.FAILED:
            if job.error_code == CANCELLED_ERROR_CODE:
                continue
            states[book_id] = BookSealState.ENCRYPT_FAILED if sealing else BookSealState.DECRYPT_FAILED
        else:
            states[book_id] = BookSealState.ENCRYPTING if sealing else BookSealState.DECRYPTING
    return states


__all__ = [
    "BookSealState",
    "CANCELLED_ERROR_CODE",
    "VAULT_FORMAT_VERSION",
    "book_seal_states",
    "VaultAlreadyExistsError",
    "VaultJob",
    "VaultJobDirection",
    "VaultJobPhase",
    "VaultRecord",
]
