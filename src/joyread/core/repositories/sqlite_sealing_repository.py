"""Book-level transitions of the Hidden Space seal/unseal queue.

Every method is one transaction over ``books``, ``book_files`` and
``vault_jobs``, so the user's intent and the job that carries it out can never
disagree after a crash. Paths are storage-relative, like everywhere else in the
database. ``SealingService`` does the filesystem work in between.

Three rules keep a request from corrupting a job the worker is running:

* **Every worker step is a compare-and-set on the job's phase.** A request may
  delete a job mid-run (unhiding a book still waiting to be sealed, deleting
  the book); the worker notices at its next step and removes only what it
  wrote itself.
* **A request never discards a file only a job knows about.** An unseal job
  that has started writing owns a *decrypted* copy, so cancelling it marks it
  ``failed``/``cancelled`` instead of deleting it, and the worker deletes the
  copy. A plain delete would leave plaintext nothing remembers.
* **A seal that has committed still owes the delete of the plaintext
  original.** Unhiding then answers ``BUSY`` and asks again once the worker
  has finished, rather than abandoning that delete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import sqlite3
from typing import Iterable

from joyread.core.models.storage_layout import STORAGE_KIND_SEALED
from joyread.core.models.vault import CANCELLED_ERROR_CODE, VaultJob, VaultJobDirection, VaultJobPhase
from joyread.core.repositories.sqlite_book_repository import apply_book_hidden_state
from joyread.core.repositories.sqlite_vault_repository import insert_vault_job, job_from_row
from joyread.infrastructure.database.database_interpreter import DatabaseInterpreter, DatabasePriority



class UnhideOutcome(StrEnum):
    #: The book is plain and visible now.
    VISIBLE = "visible"
    #: An unseal job decrypts it; the book leaves Hidden when that commits.
    UNSEALING = "unsealing"
    #: A seal is finishing its cleanup. Ask again once it has.
    BUSY = "busy"
    MISSING = "missing"


@dataclass(frozen=True)
class SealingSubject:
    """A job with the rows it acts on, read together."""

    job: VaultJob
    is_hidden: bool
    cover_path: str | None
    storage_path: str
    storage_kind: str
    file_format: str
    hash_algorithm: str
    source_hash: str
    stored_hash: str
    #: Another book, or a private record, uses the same file. Sealing it would
    #: take the book away from them too.
    shared: bool

    @property
    def book_id(self) -> str:
        return self.job.book_id

    @property
    def file_id(self) -> str:
        return self.job.file_id


class SqliteSealingRepository:
    def __init__(self, database: DatabaseInterpreter) -> None:
        self._database = database

    # -- requests -------------------------------------------------------------

    def hide_books(self, book_ids: Iterable[str]) -> tuple[str, ...]:
        """Hide each book and queue the seal that encrypts it, atomically.

        Returns the books that gained a seal job. A book already sealed, or
        already being sealed, gains none; hiding a book whose unhide is still
        pending cancels that unhide instead.
        """

        targets = tuple(dict.fromkeys(book_ids))
        now = _now()

        def write(connection: sqlite3.Connection) -> tuple[str, ...]:
            queued: list[str] = []
            with _immediate(connection):
                for book_id in targets:
                    book = _book_row(connection, book_id)
                    if book is None:
                        continue
                    job = _job_for_book(connection, book_id)
                    if job is not None:
                        if job.direction is VaultJobDirection.SEAL:
                            continue
                        if job.phase is not VaultJobPhase.COMMITTED:
                            # Still hidden and still sealed: hiding again just
                            # takes back the unhide.
                            _cancel_unseal(connection, job, now)
                            continue
                        # The unseal finished. All it still owes is deleting
                        # ciphertext that the new seal overwrites anyway.
                        _delete_job(connection, job.job_id)
                    if not book["is_hidden"]:
                        apply_book_hidden_state(connection, book_id, True, now)
                    if book["storage_kind"] != STORAGE_KIND_SEALED:
                        insert_vault_job(
                            connection,
                            book_id=book_id,
                            file_id=str(book["file_id"]),
                            direction=VaultJobDirection.SEAL,
                        )
                        queued.append(book_id)
            return tuple(queued)

        return self._database.execute(write, DatabasePriority.NORMAL)

    def request_unhide(self, book_ids: Iterable[str]) -> dict[str, UnhideOutcome]:
        """Make each book visible, or queue the unseal that will."""

        targets = tuple(dict.fromkeys(book_ids))
        now = _now()

        def write(connection: sqlite3.Connection) -> dict[str, UnhideOutcome]:
            outcomes: dict[str, UnhideOutcome] = {}
            with _immediate(connection):
                for book_id in targets:
                    outcomes[book_id] = _request_unhide_one(connection, book_id, now)
            return outcomes

        return self._database.execute(write, DatabasePriority.NORMAL)

    def queue_hidden_plain_books(self) -> int:
        """Bring every book in line with its hidden flag. Returns jobs queued.

        A hidden book with a plain file gains a seal job: the upgrade from soft
        hiding, and any hide that happened before the Library had a vault. A
        sealed file whose book is visible cannot be read where it is shown, so
        it is hidden again with an unseal queued -- the same state an unhide
        that has not committed yet is in.
        """

        now = _now()

        def write(connection: sqlite3.Connection) -> int:
            queued = 0
            with _immediate(connection):
                rows = connection.execute(
                    """
                    SELECT books.book_id, books.file_id, books.is_hidden, book_files.storage_kind
                    FROM books
                    JOIN book_files ON book_files.file_id = books.file_id
                    LEFT JOIN vault_jobs ON vault_jobs.book_id = books.book_id
                    WHERE vault_jobs.job_id IS NULL
                      AND (
                          (books.is_hidden = 1 AND book_files.storage_kind != ?)
                          OR (books.is_hidden = 0 AND book_files.storage_kind = ?)
                      )
                    ORDER BY books.created_at, books.book_id
                    """,
                    (STORAGE_KIND_SEALED, STORAGE_KIND_SEALED),
                ).fetchall()
                for row in rows:
                    if row["is_hidden"]:
                        direction = VaultJobDirection.SEAL
                    else:
                        apply_book_hidden_state(connection, str(row["book_id"]), True, now)
                        direction = VaultJobDirection.UNSEAL
                    insert_vault_job(
                        connection,
                        book_id=str(row["book_id"]),
                        file_id=str(row["file_id"]),
                        direction=direction,
                    )
                    queued += 1
            return queued

        return self._database.execute(write, DatabasePriority.NORMAL)

    def retry_failed_jobs(self) -> int:
        """Queue every failed job again, except the cancelled ones."""

        now = _now()
        return self._database.execute(
            lambda connection: connection.execute(
                """
                UPDATE vault_jobs
                SET phase = ?, error_code = NULL, attempts = attempts + 1, updated_at = ?
                WHERE phase = ? AND (error_code IS NULL OR error_code != ?)
                """,
                (VaultJobPhase.QUEUED.value, now, VaultJobPhase.FAILED.value, CANCELLED_ERROR_CODE),
            ).rowcount,
            DatabasePriority.NORMAL,
        )

    # -- the worker -----------------------------------------------------------

    def runnable_jobs(self, *, can_decrypt: bool) -> list[VaultJob]:
        """Jobs the worker can make progress on now, oldest first.

        Only a queued unseal needs the private key. Everything else -- sealing,
        committing a written file, cleaning up -- works while locked, so a
        crash recovers without anyone unlocking.
        """

        rows = self._database.execute(
            lambda connection: connection.execute(
                """
                SELECT * FROM vault_jobs
                WHERE phase IN (?, ?)
                   OR (phase = ? AND (direction = ? OR ?))
                   OR (phase = ? AND error_code = ?)
                ORDER BY created_at, job_id
                """,
                (
                    VaultJobPhase.WRITTEN.value,
                    VaultJobPhase.COMMITTED.value,
                    VaultJobPhase.QUEUED.value,
                    VaultJobDirection.SEAL.value,
                    1 if can_decrypt else 0,
                    VaultJobPhase.FAILED.value,
                    CANCELLED_ERROR_CODE,
                ),
            ).fetchall(),
            DatabasePriority.NORMAL,
        )
        return [job_from_row(row) for row in rows]

    def load_subject(self, job_id: str) -> SealingSubject | None:
        def read(connection: sqlite3.Connection) -> SealingSubject | None:
            row = connection.execute(
                """
                SELECT
                    vault_jobs.*,
                    books.is_hidden, books.cover_path,
                    book_files.storage_path, book_files.storage_kind, book_files.file_format,
                    book_files.hash_algorithm, book_files.source_hash, book_files.stored_hash,
                    (SELECT COUNT(*) FROM books AS other
                       WHERE other.file_id = vault_jobs.file_id AND other.book_id != vault_jobs.book_id)
                    + (SELECT COUNT(*) FROM private_books WHERE private_books.file_id = vault_jobs.file_id)
                    AS other_references
                FROM vault_jobs
                JOIN books ON books.book_id = vault_jobs.book_id
                JOIN book_files ON book_files.file_id = vault_jobs.file_id
                WHERE vault_jobs.job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            return SealingSubject(
                job=job_from_row(row),
                is_hidden=bool(row["is_hidden"]),
                cover_path=row["cover_path"],
                storage_path=str(row["storage_path"]),
                storage_kind=str(row["storage_kind"]),
                file_format=str(row["file_format"] or ""),
                hash_algorithm=str(row["hash_algorithm"]),
                source_hash=str(row["source_hash"]),
                stored_hash=str(row["stored_hash"]),
                shared=int(row["other_references"]) > 0,
            )

        return self._database.execute(read, DatabasePriority.NORMAL)

    def claim_partial(self, job_id: str, partial_path: str) -> bool:
        """Record the file about to be written, so a crash knows to remove it."""

        return self._update_if(job_id, VaultJobPhase.QUEUED, partial_path=partial_path)

    def release_partial(self, job_id: str) -> bool:
        """The partial file is gone; the job stays queued for another try."""

        return self._update_if(job_id, VaultJobPhase.QUEUED, partial_path=None)

    def mark_written(self, job_id: str, *, target_path: str, previous_path: str, expected_hash: str) -> bool:
        return self._update_if(
            job_id,
            VaultJobPhase.QUEUED,
            phase=VaultJobPhase.WRITTEN,
            partial_path=None,
            target_path=target_path,
            previous_path=previous_path,
            expected_hash=expected_hash,
        )

    def requeue_written(self, job_id: str) -> bool:
        """A written file failed its recheck; start the job over."""

        return self._update_if(
            job_id,
            VaultJobPhase.WRITTEN,
            phase=VaultJobPhase.QUEUED,
            target_path=None,
            previous_path=None,
            expected_hash=None,
        )

    def fail_job(self, job_id: str, error_code: str) -> bool:
        return self._update_if(
            job_id, VaultJobPhase.QUEUED, phase=VaultJobPhase.FAILED, partial_path=None, error_code=error_code
        )

    def note_cleanup_failure(self, job_id: str, error_code: str) -> bool:
        """Keep a committed job for another cleanup attempt, saying why."""

        return self._update_if(job_id, VaultJobPhase.COMMITTED, error_code=error_code)

    def commit_seal(
        self,
        job_id: str,
        *,
        storage_path: str,
        stored_hash: str,
        file_format: str,
        cover_path: str | None,
    ) -> bool:
        """Point the row at the sealed file, all at once. False if the job is gone."""

        now = _now()

        def write(connection: sqlite3.Connection) -> bool:
            with _immediate(connection):
                job = _job_if(connection, job_id, VaultJobPhase.WRITTEN)
                if job is None:
                    return False
                connection.execute(
                    """
                    UPDATE book_files
                    SET storage_path = ?, storage_kind = ?, stored_hash = ?, file_format = ?,
                        state = 'healthy', integrity_error_code = NULL, updated_at = ?
                    WHERE file_id = ?
                    """,
                    (storage_path, STORAGE_KIND_SEALED, stored_hash, file_format, now, job.file_id),
                )
                connection.execute(
                    "UPDATE books SET cover_path = ?, updated_at = ? WHERE book_id = ?",
                    (cover_path, now, job.book_id),
                )
                _set_phase(connection, job_id, VaultJobPhase.COMMITTED, now)
            return True

        return self._database.execute(write, DatabasePriority.NORMAL)

    def commit_unseal(
        self,
        job_id: str,
        *,
        storage_path: str,
        storage_kind: str,
        stored_hash: str,
        file_format: str,
        cover_path: str | None,
    ) -> bool:
        """Point the row at the decrypted file and let the book out of Hidden, at once."""

        now = _now()

        def write(connection: sqlite3.Connection) -> bool:
            with _immediate(connection):
                job = _job_if(connection, job_id, VaultJobPhase.WRITTEN)
                if job is None:
                    return False
                connection.execute(
                    """
                    UPDATE book_files
                    SET storage_path = ?, storage_kind = ?, stored_hash = ?, file_format = ?,
                        state = 'healthy', integrity_error_code = NULL, updated_at = ?
                    WHERE file_id = ?
                    """,
                    (storage_path, storage_kind, stored_hash, file_format, now, job.file_id),
                )
                connection.execute(
                    "UPDATE books SET cover_path = ?, updated_at = ? WHERE book_id = ?",
                    (cover_path, now, job.book_id),
                )
                apply_book_hidden_state(connection, job.book_id, False, now)
                _set_phase(connection, job_id, VaultJobPhase.COMMITTED, now)
            return True

        return self._database.execute(write, DatabasePriority.NORMAL)

    def finish_plain_unhide(self, job_id: str) -> bool:
        """An unseal found its file already plain: just let the book out of Hidden."""

        now = _now()

        def write(connection: sqlite3.Connection) -> bool:
            with _immediate(connection):
                job = _job_if(connection, job_id, VaultJobPhase.QUEUED)
                if job is None:
                    return False
                apply_book_hidden_state(connection, job.book_id, False, now)
                _delete_job(connection, job_id)
            return True

        return self._database.execute(write, DatabasePriority.NORMAL)

    def finish(self, job_id: str) -> None:
        self._database.execute(
            lambda connection: _delete_job(connection, job_id),
            DatabasePriority.NORMAL,
        )

    def jobs_by_book(self) -> dict[str, VaultJob]:
        """Every outstanding job, for showing "Encrypting..." and failures."""

        rows = self._database.execute(
            lambda connection: connection.execute("SELECT * FROM vault_jobs").fetchall(),
            DatabasePriority.HIGH,
        )
        return {str(row["book_id"]): job_from_row(row) for row in rows}

    # -- what others must not touch -------------------------------------------

    def job_artifact_paths(self) -> set[str]:
        """Every path a job wrote or still owes work on. Never orphans."""

        rows = self._database.execute(
            lambda connection: connection.execute(
                "SELECT partial_path, target_path, previous_path FROM vault_jobs"
            ).fetchall(),
            DatabasePriority.HIGH,
        )
        return {
            str(value)
            for row in rows
            for value in (row["partial_path"], row["target_path"], row["previous_path"])
            if value
        }

    def _update_if(self, job_id: str, expected: VaultJobPhase, /, **changes: object) -> bool:
        """Apply *changes* only while the job is still in phase *expected*."""

        assignments = [f"{column} = ?" for column in changes]
        values = [value.value if isinstance(value, VaultJobPhase) else value for value in changes.values()]
        now = _now()
        return bool(
            self._database.execute(
                lambda connection: connection.execute(
                    f"""
                    UPDATE vault_jobs SET {', '.join(assignments)}, updated_at = ?
                    WHERE job_id = ? AND phase = ?
                    """,
                    (*values, now, job_id, expected.value),
                ).rowcount,
                DatabasePriority.NORMAL,
            )
        )


def _request_unhide_one(connection: sqlite3.Connection, book_id: str, now: str) -> UnhideOutcome:
    book = _book_row(connection, book_id)
    if book is None:
        return UnhideOutcome.MISSING
    job = _job_for_book(connection, book_id)
    if job is not None:
        if job.direction is VaultJobDirection.SEAL:
            if job.phase is VaultJobPhase.COMMITTED:
                return UnhideOutcome.BUSY
            # The row still names the plain file: drop the seal. Whatever the
            # job wrote so far is ciphertext, and the worker removes it.
            _delete_job(connection, job.job_id)
            apply_book_hidden_state(connection, book_id, False, now)
            return UnhideOutcome.VISIBLE
        if job.phase is VaultJobPhase.FAILED:
            if job.error_code == CANCELLED_ERROR_CODE:
                # The worker has yet to delete what the cancelled unseal
                # wrote; a new job would race it over the same paths.
                return UnhideOutcome.BUSY
            connection.execute(
                """
                UPDATE vault_jobs SET phase = ?, error_code = NULL, attempts = attempts + 1, updated_at = ?
                WHERE job_id = ?
                """,
                (VaultJobPhase.QUEUED.value, now, job.job_id),
            )
        return UnhideOutcome.UNSEALING
    if not book["is_hidden"]:
        return UnhideOutcome.VISIBLE
    if book["storage_kind"] == STORAGE_KIND_SEALED:
        insert_vault_job(
            connection, book_id=book_id, file_id=str(book["file_id"]), direction=VaultJobDirection.UNSEAL
        )
        return UnhideOutcome.UNSEALING
    apply_book_hidden_state(connection, book_id, False, now)
    return UnhideOutcome.VISIBLE


def _cancel_unseal(connection: sqlite3.Connection, job: VaultJob, now: str) -> None:
    if job.phase is VaultJobPhase.QUEUED and job.partial_path is None and job.target_path is None:
        _delete_job(connection, job.job_id)
        return
    # It may have written a decrypted copy. Only the worker deletes that, so
    # the job stays until it has.
    connection.execute(
        "UPDATE vault_jobs SET phase = ?, error_code = ?, updated_at = ? WHERE job_id = ?",
        (VaultJobPhase.FAILED.value, CANCELLED_ERROR_CODE, now, job.job_id),
    )


class _immediate:
    """``BEGIN IMMEDIATE`` ... ``COMMIT``, rolled back on any exception."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self._connection.execute("BEGIN IMMEDIATE")
        return self._connection

    def __exit__(self, exc_type, _exc, _traceback) -> bool:  # noqa: ANN001
        self._connection.execute("ROLLBACK" if exc_type is not None else "COMMIT")
        return False


def _book_row(connection: sqlite3.Connection, book_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT books.book_id, books.file_id, books.is_hidden, book_files.storage_kind
        FROM books JOIN book_files ON book_files.file_id = books.file_id
        WHERE books.book_id = ?
        """,
        (book_id,),
    ).fetchone()


def _job_for_book(connection: sqlite3.Connection, book_id: str) -> VaultJob | None:
    row = connection.execute("SELECT * FROM vault_jobs WHERE book_id = ?", (book_id,)).fetchone()
    return job_from_row(row) if row is not None else None


def _job_if(connection: sqlite3.Connection, job_id: str, phase: VaultJobPhase) -> VaultJob | None:
    row = connection.execute(
        "SELECT * FROM vault_jobs WHERE job_id = ? AND phase = ?", (job_id, phase.value)
    ).fetchone()
    return job_from_row(row) if row is not None else None


def _set_phase(connection: sqlite3.Connection, job_id: str, phase: VaultJobPhase, now: str) -> None:
    connection.execute(
        "UPDATE vault_jobs SET phase = ?, error_code = NULL, updated_at = ? WHERE job_id = ?",
        (phase.value, now, job_id),
    )


def _delete_job(connection: sqlite3.Connection, job_id: str) -> None:
    connection.execute("DELETE FROM vault_jobs WHERE job_id = ?", (job_id,))


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


__all__ = [
    "CANCELLED_ERROR_CODE",
    "SealingSubject",
    "SqliteSealingRepository",
    "UnhideOutcome",
]
