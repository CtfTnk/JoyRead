"""SQLite storage for the vault record, its unlock slots and sealing jobs.

The vault lives in the Library database, not in settings, so it moves with the
Library: a Library copied to another machine opens there with its passphrase or
recovery code.

Only public and wrapped material is stored here -- the public key, slots whose
private-key wrap needs a secret to open, and job bookkeeping. Nothing in these
tables lets anyone read a sealed book.
"""

from __future__ import annotations

from datetime import datetime
import sqlite3
from typing import Iterable, Sequence
from uuid import uuid4

from joyread.core.models.vault import (
    VAULT_FORMAT_VERSION,
    VaultAlreadyExistsError,
    VaultJob,
    VaultJobDirection,
    VaultJobPhase,
    VaultRecord,
)
from joyread.core.vault import UnlockSlot, VaultPublicKey
from joyread.infrastructure.database.database_interpreter import DatabaseInterpreter, DatabasePriority


_UNSET = object()


class SqliteVaultRepository:
    def __init__(self, database: DatabaseInterpreter) -> None:
        self._database = database

    # -- vault and slots ------------------------------------------------------

    def get_vault(self) -> VaultRecord | None:
        row = self._database.execute(
            lambda connection: connection.execute(
                "SELECT * FROM vaults ORDER BY created_at, vault_id LIMIT 1"
            ).fetchone(),
            DatabasePriority.HIGH,
        )
        return _vault_from_row(row) if row is not None else None

    def create_vault(
        self,
        public_key: VaultPublicKey,
        slots: Sequence[UnlockSlot],
        *,
        vault_id: str | None = None,
    ) -> VaultRecord:
        """Store a new vault and its first slots in one transaction.

        A vault with no slot could never be unlocked, so the two are never
        written apart.
        """

        if not slots:
            raise ValueError("A vault needs at least one unlock slot.")
        new_id = vault_id or str(uuid4())
        now = _now()

        def write(connection: sqlite3.Connection) -> None:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute("SELECT 1 FROM vaults LIMIT 1").fetchone() is not None:
                    raise VaultAlreadyExistsError("This Library already has a vault.")
                connection.execute(
                    """
                    INSERT INTO vaults(vault_id, format_version, public_key, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (new_id, VAULT_FORMAT_VERSION, public_key.raw, now, now),
                )
                for slot in slots:
                    _insert_slot(connection, new_id, slot, now)
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

        self._database.execute(write, DatabasePriority.HIGH)
        return VaultRecord(new_id, VAULT_FORMAT_VERSION, public_key, now, now)

    def delete_vault(self, vault_id: str) -> None:
        """Remove the vault; its slots go with it. Sealed books become unreadable."""

        self._database.execute(
            lambda connection: connection.execute("DELETE FROM vaults WHERE vault_id = ?", (vault_id,)),
            DatabasePriority.HIGH,
        )

    def list_slots(self, vault_id: str) -> list[UnlockSlot]:
        rows = self._database.execute(
            lambda connection: connection.execute(
                """
                SELECT slot_id, slot_type, params_json, wrapped_key
                FROM vault_slots WHERE vault_id = ? ORDER BY created_at, slot_id
                """,
                (vault_id,),
            ).fetchall(),
            DatabasePriority.HIGH,
        )
        return [
            UnlockSlot.from_storage(
                str(row["slot_id"]), str(row["slot_type"]), str(row["params_json"]), bytes(row["wrapped_key"])
            )
            for row in rows
        ]

    def replace_slots(self, vault_id: str, slot_type: str, slot: UnlockSlot) -> None:
        """Swap every slot of *slot_type* for *slot*, atomically.

        Changing the passphrase or issuing a new recovery code. Atomic so a
        crash can never leave the vault with neither the old slot nor the new.
        """

        if slot.slot_type != slot_type:
            raise ValueError("The replacement slot has a different type.")
        now = _now()

        def write(connection: sqlite3.Connection) -> None:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute("SELECT 1 FROM vaults WHERE vault_id = ?", (vault_id,)).fetchone() is None:
                    raise LookupError("The vault no longer exists.")
                connection.execute(
                    "DELETE FROM vault_slots WHERE vault_id = ? AND slot_type = ?",
                    (vault_id, slot_type),
                )
                _insert_slot(connection, vault_id, slot, now)
                connection.execute("UPDATE vaults SET updated_at = ? WHERE vault_id = ?", (now, vault_id))
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

        self._database.execute(write, DatabasePriority.HIGH)

    # -- jobs -----------------------------------------------------------------

    def enqueue_job(self, book_id: str, file_id: str, direction: VaultJobDirection) -> VaultJob:
        """Queue one job as its own transaction.

        A hide that must enqueue in the same transaction as its own writes uses
        :func:`insert_vault_job` inside that transaction instead.
        """

        job_id = self._database.execute(
            lambda connection: insert_vault_job(
                connection, book_id=book_id, file_id=file_id, direction=direction
            ),
            DatabasePriority.NORMAL,
        )
        job = self.get_job(job_id)
        if job is None:  # pragma: no cover - only a concurrent delete can get here.
            raise LookupError("The job was removed as soon as it was queued.")
        return job

    def get_job(self, job_id: str) -> VaultJob | None:
        row = self._database.execute(
            lambda connection: connection.execute(
                "SELECT * FROM vault_jobs WHERE job_id = ?", (job_id,)
            ).fetchone(),
            DatabasePriority.NORMAL,
        )
        return _job_from_row(row) if row is not None else None

    def get_job_for_book(self, book_id: str) -> VaultJob | None:
        row = self._database.execute(
            lambda connection: connection.execute(
                "SELECT * FROM vault_jobs WHERE book_id = ?", (book_id,)
            ).fetchone(),
            DatabasePriority.NORMAL,
        )
        return _job_from_row(row) if row is not None else None

    def list_jobs(
        self,
        *,
        phases: Iterable[VaultJobPhase] | None = None,
        direction: VaultJobDirection | None = None,
    ) -> list[VaultJob]:
        clauses: list[str] = []
        parameters: list[str] = []
        if phases is not None:
            wanted = [VaultJobPhase(phase).value for phase in phases]
            if not wanted:
                return []
            clauses.append(f"phase IN ({', '.join('?' for _ in wanted)})")
            parameters.extend(wanted)
        if direction is not None:
            clauses.append("direction = ?")
            parameters.append(VaultJobDirection(direction).value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._database.execute(
            lambda connection: connection.execute(
                f"SELECT * FROM vault_jobs {where} ORDER BY created_at, job_id", parameters
            ).fetchall(),
            DatabasePriority.NORMAL,
        )
        return [_job_from_row(row) for row in rows]

    def update_job(
        self,
        job_id: str,
        *,
        phase: VaultJobPhase | None = None,
        partial_path: object = _UNSET,
        target_path: object = _UNSET,
        previous_path: object = _UNSET,
        expected_hash: object = _UNSET,
        error_code: object = _UNSET,
        count_attempt: bool = False,
    ) -> bool:
        """Change the given fields; return whether the job still existed.

        A job can vanish under its runner -- deleting the book cascades it
        away -- and the runner must notice rather than write on regardless.
        """

        assignments: list[str] = []
        parameters: list[object] = []
        if phase is not None:
            assignments.append("phase = ?")
            parameters.append(VaultJobPhase(phase).value)
        for column, value in (
            ("partial_path", partial_path),
            ("target_path", target_path),
            ("previous_path", previous_path),
            ("expected_hash", expected_hash),
            ("error_code", error_code),
        ):
            if value is not _UNSET:
                assignments.append(f"{column} = ?")
                parameters.append(value)
        if count_attempt:
            assignments.append("attempts = attempts + 1")
        assignments.append("updated_at = ?")
        parameters.extend((_now(), job_id))
        changed = self._database.execute(
            lambda connection: connection.execute(
                f"UPDATE vault_jobs SET {', '.join(assignments)} WHERE job_id = ?", parameters
            ).rowcount,
            DatabasePriority.NORMAL,
        )
        return bool(changed)

    def delete_job(self, job_id: str) -> None:
        self._database.execute(
            lambda connection: connection.execute("DELETE FROM vault_jobs WHERE job_id = ?", (job_id,)),
            DatabasePriority.NORMAL,
        )


def insert_vault_job(
    connection: sqlite3.Connection,
    *,
    book_id: str,
    file_id: str,
    direction: VaultJobDirection,
) -> str:
    """Insert a queued job on *connection*, inside the caller's transaction.

    Raises ``sqlite3.IntegrityError`` if the book already has a job: one job
    per book, so two runners can never race over the same file.
    """

    job_id = str(uuid4())
    now = _now()
    connection.execute(
        """
        INSERT INTO vault_jobs(job_id, book_id, file_id, direction, phase, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (job_id, book_id, file_id, VaultJobDirection(direction).value, VaultJobPhase.QUEUED.value, now, now),
    )
    return job_id


def _insert_slot(connection: sqlite3.Connection, vault_id: str, slot: UnlockSlot, now: str) -> None:
    connection.execute(
        """
        INSERT INTO vault_slots(slot_id, vault_id, slot_type, params_json, wrapped_key, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (slot.slot_id, vault_id, slot.slot_type, slot.params_json(), slot.wrapped_key, now, now),
    )


def _vault_from_row(row: sqlite3.Row) -> VaultRecord:
    return VaultRecord(
        vault_id=str(row["vault_id"]),
        format_version=int(row["format_version"]),
        public_key=VaultPublicKey(bytes(row["public_key"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _job_from_row(row: sqlite3.Row) -> VaultJob:
    return VaultJob(
        job_id=str(row["job_id"]),
        book_id=str(row["book_id"]),
        file_id=str(row["file_id"]),
        direction=VaultJobDirection(row["direction"]),
        phase=VaultJobPhase(row["phase"]),
        partial_path=row["partial_path"],
        target_path=row["target_path"],
        previous_path=row["previous_path"],
        expected_hash=row["expected_hash"],
        error_code=row["error_code"],
        attempts=int(row["attempts"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


__all__ = ["SqliteVaultRepository", "insert_vault_job"]
