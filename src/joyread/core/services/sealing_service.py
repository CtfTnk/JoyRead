"""Hidden Space sealing: encrypt hidden books, decrypt unhidden ones, survive crashes.

Hiding a book is one database transaction that sets ``is_hidden`` and queues
a seal job (``SqliteSealingRepository``). This service's worker then turns the
job into a sealed ``.jrl`` in steps a crash can always resume from:

1. **queued** -- record the partial file, write ``<target>.partial``, fsync it,
   and prove it decrypts to what was written. Seal the cover too.
2. **written** -- the finished file was renamed into place; the row still
   names the plaintext original.
3. **committed** -- one transaction repoints ``book_files`` (path, kind,
   hash) and ``books.cover_path``.
4. **cleanup** -- delete the plaintext original, its PNG covers and its
   extraction-pool entries, then drop the job.

A crash between any two steps leaves exactly one file the row can trust, and
the next run continues from the recorded phase. Unhiding runs the same steps
the other way. The book leaves Hidden in the commit transaction, so it is never
shown on the public shelf while its file is still sealed.

**What needs the key.** Sealing needs only the vault's public key, so hidden
books are encrypted -- and crashes recovered -- while the vault is locked. Only
decrypting an unhidden book waits for the vault to unlock.

**Threading.** One daemon thread runs the jobs, one at a time, each holding the
Library maintenance gate so it never interleaves with import, audit or a
storage change. Requests (hide, unhide) are short database transactions and
may come from any thread. ``pause``/``resume`` bracket a storage change and
``close`` ends the worker for good. A job interrupted by either stays queued.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import errno
import logging
import os
from pathlib import Path, PurePosixPath
import threading
from time import monotonic
from typing import Callable, Iterable

from joyread.core.archive import ArchiveError, ArchiveImageService, ArchiveOpenLimits
from joyread.core.archive.canonical import CBZ_SUFFIX, CanonicalWriteCancelled
from joyread.core.archive.errors import ArchiveCancelled
from joyread.core.archive.metadata import select_sidecars
from joyread.core.file_types import ARCHIVE_EXTENSIONS, PDF_EXTENSIONS, SEALED_EXTENSION
from joyread.core.models.storage_layout import (
    STORAGE_KIND_CANONICAL,
    STORAGE_KIND_SEALED,
    STORAGE_KIND_VERBATIM,
    storage_target,
)
from joyread.core.models.vault import VaultJob, VaultJobDirection, VaultJobPhase
from joyread.core.repositories.sqlite_sealing_repository import (
    CANCELLED_ERROR_CODE,
    SealingSubject,
    SqliteSealingRepository,
    UnhideOutcome,
)
from joyread.core.services.hash_service import HashService
from joyread.core.services.library_maintenance_service import (
    LibraryMaintenanceCoordinator,
    LibraryMaintenanceLease,
)
from joyread.core.services.sealed_cover_store import MAX_COVER_BYTES, SealedCoverStore
from joyread.core.services.sealing_writers import (
    SealedCanonicalWriter,
    SealedOutput,
    SourceChangedError,
    fsync_directory,
    fsync_file,
    seal_verbatim,
)
from joyread.core.services.vault_service import VaultService
from joyread.core.vault import (
    ContentType,
    VaultError,
    VaultKeyring,
    VaultLockedError,
    VaultOperationCancelled,
    VaultPublicKey,
)
from joyread.infrastructure.filesystem.path_service import PathService


logger = logging.getLogger(__name__)

_PARTIAL_SUFFIX = ".partial"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: Where an unhidden book's cover goes back to. Fixed, so a crash between
#: writing it and committing can find it again without the key.
RESTORED_COVER_SUFFIX = "-custom-restored.png"

#: How long one wait for the maintenance gate lasts before the worker checks
#: whether it was asked to stop. Short enough that a storage change is never
#: held up noticeably by a worker that is only waiting.
_GATE_POLL_SECONDS = 0.25

#: How long an unhide waits for a seal of the same book to finish its cleanup.
DEFAULT_UNHIDE_WAIT_SECONDS = 30.0

Listener = Callable[[tuple[str, ...]], None]


class _JobFailure(Exception):
    """Stop this job with a stable ``error_code`` for the UI to explain."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _SealPlan:
    content_type: ContentType
    #: An archive JoyRead did not write: repackage it as a canonical CBZ on
    #: the way in. A canonical CBZ or a PDF is sealed byte for byte.
    convert: bool
    file_format: str


@dataclass(frozen=True)
class _UnsealPlan:
    storage_kind: str
    suffix: str
    file_format: str


class SealingService:
    def __init__(
        self,
        paths: PathService,
        vault: VaultService,
        repository: SqliteSealingRepository,
        archive_service: ArchiveImageService,
        hash_service: HashService,
        cover_store: SealedCoverStore,
        *,
        coordinator: LibraryMaintenanceCoordinator,
        archive_limits: ArchiveOpenLimits | None = None,
        invalidate_artifacts: Callable[[str, tuple[str, ...]], None] | None = None,
        invalidate_cover: Callable[[str], None] | None = None,
    ) -> None:
        self._paths = paths
        self._vault = vault
        self._repository = repository
        self._archive_service = archive_service
        self._hash_service = hash_service
        self._cover_store = cover_store
        self._coordinator = coordinator
        self._archive_limits = archive_limits or ArchiveOpenLimits()
        self._invalidate_artifacts = invalidate_artifacts
        self._invalidate_cover = invalidate_cover

        self._state = threading.Condition()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._paused = False
        self._closed = False
        self._busy = False
        #: Bumped after every job; an unhide that met a busy seal waits on it.
        self._progress = 0
        self._listeners: list[Listener] = []

    @property
    def _keyring(self) -> VaultKeyring:
        return self._vault.keyring

    def set_archive_open_limits(self, limits: ArchiveOpenLimits) -> None:
        self._archive_limits = limits

    def set_archive_service(self, archive_service: ArchiveImageService) -> None:
        """Use the engine a cache-strategy change put in place, from the next job on."""

        self._archive_service = archive_service

    # -- requests (any thread) ------------------------------------------------

    def hide_books(self, book_ids: Iterable[str]) -> tuple[str, ...]:
        """Hide books and queue their seals. Needs a vault; the key is not needed."""

        targets = tuple(dict.fromkeys(book_ids))
        if self._vault.public_key is None:
            raise VaultError("Hidden Space has no vault to seal books to.", code="no_vault")
        queued = self._repository.hide_books(targets)
        self._notify(targets)
        self.wake()
        return queued

    def unhide_books(
        self,
        book_ids: Iterable[str],
        *,
        wait_seconds: float = DEFAULT_UNHIDE_WAIT_SECONDS,
    ) -> dict[str, UnhideOutcome]:
        """Make books visible, queueing decryption for the sealed ones.

        A book whose seal is still finishing its cleanup answers ``BUSY``. That
        state lasts milliseconds while the worker runs, so this waits up to
        *wait_seconds* for it and asks again. Call it off the UI thread.
        """

        targets = tuple(dict.fromkeys(book_ids))
        outcomes = self._repository.request_unhide(targets)
        deadline = monotonic() + max(0.0, wait_seconds)
        busy = tuple(book_id for book_id, outcome in outcomes.items() if outcome is UnhideOutcome.BUSY)
        while busy:
            self.wake()
            if not self._wait_for_progress(deadline - monotonic()):
                break
            # A cleanup that failed again (a file still held open) will not
            # succeed by asking faster; stop waiting on it and answer BUSY.
            jobs = self._repository.jobs_by_book()
            stuck = {
                book_id
                for book_id in busy
                if (job := jobs.get(book_id)) is not None
                and job.phase is VaultJobPhase.COMMITTED
                and job.error_code is not None
            }
            retried = self._repository.request_unhide(busy)
            outcomes.update(retried)
            busy = tuple(
                book_id
                for book_id, outcome in retried.items()
                if outcome is UnhideOutcome.BUSY and book_id not in stuck
            )
        self._notify(targets)
        self.wake()
        return outcomes

    def queue_hidden_books(self) -> int:
        """Queue seals for hidden books that have none: after setup or the upgrade."""

        queued = self._repository.queue_hidden_plain_books()
        if queued:
            self.wake()
        return queued

    def retry_failed(self) -> int:
        """Give every failed job another go: on launch, and when the vault unlocks."""

        retried = self._repository.retry_failed_jobs()
        if retried:
            self.wake()
        return retried

    def jobs_by_book(self) -> dict[str, VaultJob]:
        return self._repository.jobs_by_book()

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        """Call *listener* with the book ids whose job changed. Runs on the worker."""

        with self._state:
            self._listeners.append(listener)

        def remove() -> None:
            with self._state:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return remove

    # -- lifecycle --------------------------------------------------------------

    def recover(self) -> int:
        """Launch-time bookkeeping: retry failed jobs and queue missed seals.

        Database only and quick. The files themselves are recovered by the
        worker, from each job's phase, once :meth:`start` runs.
        """

        if not self._vault.has_vault:
            return 0
        return self._repository.retry_failed_jobs() + self._repository.queue_hidden_plain_books()

    def start(self) -> None:
        with self._state:
            if self._closed or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="joyread-sealing", daemon=True)
            self._thread.start()
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    def pause(self) -> None:
        """Stop between steps; the running job aborts and stays queued. Reversible."""

        with self._state:
            self._paused = True

    def resume(self) -> None:
        with self._state:
            self._paused = False
        self.wake()

    def close(self, timeout: float = 10.0) -> None:
        """End the worker for good. A job in progress stays queued for next time."""

        with self._state:
            self._closed = True
            thread = self._thread
        self._wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                logger.warning(
                    "Hidden Space sealing worker did not stop in time",
                    extra={"event": "sealing.close.timeout", "category": "vault", "status": "failed"},
                )

    def pending_task_count(self) -> int:
        """1 while a job is still unwinding, for the storage-change drain."""

        with self._state:
            return 1 if self._busy else 0

    def wait_idle(self, timeout: float) -> bool:
        deadline = monotonic() + max(0.0, timeout)
        with self._state:
            while self._busy:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._state.wait(remaining)
            return True

    def _should_stop(self) -> bool:
        with self._state:
            return self._paused or self._closed

    def _run(self) -> None:
        while True:
            self._wake.wait()
            with self._state:
                if self._closed:
                    return
                self._wake.clear()
                if self._paused:
                    continue
                self._busy = True
            try:
                self.run_pending(is_cancelled=self._should_stop)
            except Exception:
                logger.error(
                    "Hidden Space sealing worker failed",
                    exc_info=True,
                    extra={"event": "sealing.worker.failed", "category": "vault", "status": "failed"},
                )
            finally:
                with self._state:
                    self._busy = False
                    self._state.notify_all()

    # -- the worker -------------------------------------------------------------

    def run_pending(self, *, is_cancelled: Callable[[], bool] | None = None) -> int:
        """Work through every runnable job once, on the calling thread."""

        stop = is_cancelled or (lambda: False)
        attempted: set[str] = set()
        processed = 0
        while not stop():
            runnable = self._repository.runnable_jobs(can_decrypt=self._vault.is_unlocked)
            job = next((job for job in runnable if job.job_id not in attempted), None)
            if job is None:
                break
            attempted.add(job.job_id)
            lease = self._acquire_gate(stop)
            if lease is None:
                break
            try:
                self._process(job.job_id, stop)
            except Exception:
                logger.error(
                    "Hidden Space job failed unexpectedly",
                    exc_info=True,
                    extra={"event": "sealing.job.crashed", "category": "vault", "status": "failed",
                           "direction": job.direction.value},
                )
            finally:
                lease.release()
            processed += 1
            with self._state:
                self._progress += 1
                self._state.notify_all()
            self._notify((job.book_id,))
        return processed

    def _acquire_gate(self, stop: Callable[[], bool]) -> LibraryMaintenanceLease | None:
        while not stop():
            lease = self._coordinator.try_acquire("hidden-space-sealing", _GATE_POLL_SECONDS)
            if lease is not None:
                return lease
        return None

    def _process(self, job_id: str, stop: Callable[[], bool]) -> None:
        subject = self._repository.load_subject(job_id)
        if subject is None:
            return
        if subject.job.direction is VaultJobDirection.SEAL:
            self._seal(subject, stop)
        else:
            self._unseal(subject, stop)

    # -- sealing ----------------------------------------------------------------

    def _seal(self, subject: SealingSubject, stop: Callable[[], bool]) -> None:
        job = subject.job
        if job.phase is VaultJobPhase.COMMITTED:
            self._finish_seal(subject)
            return
        if job.phase is VaultJobPhase.WRITTEN:
            self._commit_seal(subject, stop)
            return
        if job.phase is not VaultJobPhase.QUEUED:
            return
        if not subject.is_hidden or subject.storage_kind == STORAGE_KIND_SEALED:
            # Unhidden in between, or a duplicate: nothing left to encrypt.
            self._repository.finish(job.job_id)
            return
        public_key = self._vault.public_key
        if public_key is None:
            self._fail(job, "no_vault")
            return
        if subject.shared:
            self._fail(job, "shared_file")
            return
        source = self._book_path(subject.storage_path)
        if source is None or not source.is_file():
            self._fail(job, "source_missing")
            return
        plan = _seal_plan(subject)
        if plan is None:
            self._fail(job, "unsupported_format")
            return

        target = storage_target(
            self._paths.paths.books,
            storage_kind=STORAGE_KIND_SEALED,
            stored_hash=subject.stored_hash,
            file_id=subject.file_id,
            suffix=SEALED_EXTENSION,
        )
        partial = target.with_name(target.name + _PARTIAL_SUFFIX)
        _remove_quietly(partial)
        if not self._repository.claim_partial(job.job_id, self._relative(partial)):
            return
        try:
            output = self._write_sealed(subject, source, partial, public_key, plan, stop)
            output.verify(partial, is_cancelled=stop)
            self._seal_cover(subject, public_key)
            os.replace(partial, target)
            fsync_directory(target.parent)
        except BaseException as exc:
            _remove_quietly(partial)
            # The cover may already be sealed; nothing points at it yet.
            self._discard_seal_outputs(subject, None)
            if _is_cancellation(exc):
                self._repository.release_partial(job.job_id)
                return
            if not isinstance(exc, Exception):
                raise
            self._fail(job, _failure_code(exc), exc)
            return
        if not self._repository.mark_written(
            job.job_id,
            target_path=self._relative(target),
            previous_path=subject.storage_path,
            expected_hash=output.stored_hash,
        ):
            # Unhidden or deleted while this ran. What it wrote is ciphertext
            # nothing will point at; take it back.
            self._discard_seal_outputs(subject, target)
            return
        reloaded = self._repository.load_subject(job.job_id)
        if reloaded is not None:
            self._commit_seal(reloaded, stop, verified=True, file_format=plan.file_format)

    def _write_sealed(
        self,
        subject: SealingSubject,
        source: Path,
        partial: Path,
        public_key: VaultPublicKey,
        plan: _SealPlan,
        stop: Callable[[], bool],
    ) -> SealedOutput:
        def digest_factory():  # noqa: ANN202 - hashlib object.
            return self._hash_service.new_digest(subject.hash_algorithm)

        if not plan.convert:
            return seal_verbatim(
                source,
                partial,
                public_key,
                plan.content_type,
                digest_factory=digest_factory,
                expected_source_hash=subject.stored_hash,
                is_cancelled=stop,
            )
        limits = self._archive_limits
        # Import already ran this gate; running it again finds the metadata
        # sidecars the canonical CBZ should carry, and refuses an archive the
        # converter would only fail on halfway.
        inspection = self._archive_service.inspect_for_import(source, limits=limits)
        if not inspection.accepted:
            raise _JobFailure("unsupported_content")
        writer = SealedCanonicalWriter(public_key, digest_factory)
        self._archive_service.convert_to_canonical(
            source,
            partial,
            limits=limits,
            writer=writer,
            sidecars=select_sidecars(inspection.metadata_entries),
            is_cancelled=stop,
        )
        if writer.output is None:  # pragma: no cover - the writer sets it or raises.
            raise _JobFailure("conversion_failed")
        return writer.output

    def _commit_seal(
        self,
        subject: SealingSubject,
        stop: Callable[[], bool],
        *,
        verified: bool = False,
        file_format: str | None = None,
    ) -> None:
        job = subject.job
        target = self._book_path(job.target_path)
        if not verified and not self._written_file_matches(target, job, subject.hash_algorithm):
            # A crash after the rename, and the file is not what was recorded.
            # Start over; the row still names the plaintext.
            _remove_quietly(target)
            if self._repository.requeue_written(job.job_id):
                reloaded = self._repository.load_subject(job.job_id)
                if reloaded is not None:
                    self._seal(reloaded, stop)
            return
        sealed_cover = self._cover_store.path_for(subject.book_id)
        cover_path = self._relative(sealed_cover) if sealed_cover.is_file() else None
        committed = self._repository.commit_seal(
            job.job_id,
            storage_path=str(job.target_path),
            stored_hash=str(job.expected_hash),
            file_format=file_format or _sealed_file_format(subject),
            cover_path=cover_path,
        )
        if not committed:
            self._discard_seal_outputs(subject, target)
            return
        logger.info(
            "Hidden Space book sealed",
            extra={"event": "sealing.sealed", "category": "vault", "status": "finished",
                   "file_id": subject.file_id},
        )
        reloaded = self._repository.load_subject(job.job_id)
        if reloaded is not None:
            self._finish_seal(reloaded)

    def _finish_seal(self, subject: SealingSubject) -> None:
        job = subject.job
        failures: list[BaseException] = []
        previous = self._book_path(job.previous_path)
        if previous is not None and previous != self._book_path(subject.storage_path):
            _remove_into(previous, failures)
        for cover in self._plain_covers(subject.book_id):
            _remove_into(cover, failures)
        self._invalidate(subject)
        if failures:
            # Usually a Windows file still held open. The delete is retried on
            # the next run and the next launch; the row already names the seal.
            self._repository.note_cleanup_failure(job.job_id, _failure_code(failures[0]))
            logger.warning(
                "Hidden Space could not remove a book's plaintext yet",
                extra={"event": "sealing.cleanup.failed", "category": "vault", "status": "failed",
                       "file_id": subject.file_id, "error_type": type(failures[0]).__name__},
            )
            return
        self._repository.finish(job.job_id)

    def _seal_cover(self, subject: SealingSubject, public_key: VaultPublicKey) -> None:
        """Seal the book's current cover under its fixed name, if it has one."""

        sealed = self._cover_store.path_for(subject.book_id)
        current = self._thumbnail_path(subject.cover_path)
        if current == sealed and sealed.is_file():
            return
        candidate = current if current is not None and current.suffix.lower() == ".png" else None
        if candidate is None or not candidate.is_file():
            candidate = self._largest_generated_cover(subject.book_id)
        if candidate is None:
            return
        try:
            if candidate.stat().st_size > MAX_COVER_BYTES:
                return
            data = candidate.read_bytes()
            if data.startswith(_PNG_SIGNATURE):
                self._cover_store.write(subject.book_id, data, public_key)
        except OSError as exc:
            # The book matters more than its cover: seal it without one, and
            # the shelf renders a cover from the sealed pages while unlocked.
            logger.warning(
                "Hidden Space could not seal a book's cover",
                extra={"event": "sealing.cover.failed", "category": "vault", "status": "failed",
                       "error_type": type(exc).__name__},
            )

    def _discard_seal_outputs(self, subject: SealingSubject, target: Path | None) -> None:
        if target is not None and target != self._book_path(subject.storage_path):
            _remove_quietly(target)
        current = self._thumbnail_path(subject.cover_path)
        if current != self._cover_store.path_for(subject.book_id):
            try:
                self._cover_store.delete(subject.book_id)
            except OSError:
                pass

    # -- unsealing ----------------------------------------------------------------

    def _unseal(self, subject: SealingSubject, stop: Callable[[], bool]) -> None:
        job = subject.job
        if job.phase is VaultJobPhase.FAILED:
            if job.error_code == CANCELLED_ERROR_CODE:
                self._discard_cancelled_unseal(subject)
            return
        if job.phase is VaultJobPhase.COMMITTED:
            self._finish_unseal(subject)
            return
        if job.phase is VaultJobPhase.WRITTEN:
            self._commit_unseal(subject, stop)
            return
        if subject.storage_kind != STORAGE_KIND_SEALED:
            self._repository.finish_plain_unhide(job.job_id)
            return
        if not self._vault.is_unlocked:
            return
        source = self._book_path(subject.storage_path)
        if source is None or not source.is_file():
            self._fail(job, "source_missing")
            return
        try:
            sealed = self._keyring.open_sealed(source)
        except VaultLockedError:
            return
        except (VaultError, OSError) as exc:
            self._fail(job, _failure_code(exc), exc)
            return

        partial: Path | None = None
        try:
            with sealed:
                plan = _unseal_plan(sealed.content_type)
                if plan is None:
                    raise _JobFailure("unsupported_format")
                # Named by file id while the hash is still unknown; a PDF moves
                # to its content address on the rename below.
                partial = storage_target(
                    self._paths.paths.books,
                    storage_kind=STORAGE_KIND_CANONICAL,
                    stored_hash="",
                    file_id=subject.file_id,
                    suffix=plan.suffix + _PARTIAL_SUFFIX,
                )
                _remove_quietly(partial)
                if not self._repository.claim_partial(job.job_id, self._relative(partial)):
                    return
                plain_hash = self._decrypt_to(sealed, partial, subject.hash_algorithm, stop)
            if plan.storage_kind == STORAGE_KIND_VERBATIM and plain_hash != subject.source_hash:
                # A verbatim row's hash is its identity; anything else would
                # file the book under the wrong name.
                raise _JobFailure("hash_mismatch")
            target = storage_target(
                self._paths.paths.books,
                storage_kind=plan.storage_kind,
                stored_hash=plain_hash,
                file_id=subject.file_id,
                suffix=plan.suffix,
            )
            self._restore_cover(subject)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if self._hash_service.compute(target, subject.hash_algorithm) != plain_hash:
                    raise _JobFailure("target_conflict")
                _remove_quietly(partial)
            else:
                os.replace(partial, target)
                fsync_directory(target.parent)
        except BaseException as exc:
            if partial is not None:
                _remove_quietly(partial)
            if _is_cancellation(exc) or isinstance(exc, VaultLockedError):
                self._repository.release_partial(job.job_id)
                return
            if not isinstance(exc, Exception):
                raise
            self._fail(job, _failure_code(exc), exc)
            return
        if not self._repository.mark_written(
            job.job_id,
            target_path=self._relative(target),
            previous_path=subject.storage_path,
            expected_hash=plain_hash,
        ):
            # Hidden again, or deleted, while this ran: the decrypted copy
            # must not outlive the job that knew about it.
            _remove_quietly(target)
            _remove_quietly(self._restored_cover_path(subject.book_id))
            return
        reloaded = self._repository.load_subject(job.job_id)
        if reloaded is not None:
            self._commit_unseal(reloaded, stop, verified=True)

    def _decrypt_to(self, sealed, partial: Path, algorithm: str, stop: Callable[[], bool]) -> str:  # noqa: ANN001
        digest = self._hash_service.new_digest(algorithm)
        partial.parent.mkdir(parents=True, exist_ok=True)
        with partial.open("wb") as sink:
            for block in sealed.iter_content(is_cancelled=stop):
                digest.update(block)
                sink.write(block)
            fsync_file(sink)
        return digest.hexdigest()

    def _commit_unseal(self, subject: SealingSubject, stop: Callable[[], bool], *, verified: bool = False) -> None:
        job = subject.job
        target = self._book_path(job.target_path)
        if not verified and not self._written_file_matches(target, job, subject.hash_algorithm):
            _remove_quietly(target)
            if self._repository.requeue_written(job.job_id):
                reloaded = self._repository.load_subject(job.job_id)
                if reloaded is not None:
                    self._unseal(reloaded, stop)
            return
        if target is None:  # pragma: no cover - the path was recorded by this service.
            return
        plan = _unseal_plan_for_suffix(target.suffix.lower())
        restored = self._restored_cover_path(subject.book_id)
        committed = self._repository.commit_unseal(
            job.job_id,
            storage_path=str(job.target_path),
            storage_kind=plan.storage_kind,
            stored_hash=str(job.expected_hash),
            file_format=plan.file_format,
            cover_path=self._relative(restored) if restored.is_file() else None,
        )
        if not committed:
            _remove_quietly(target)
            _remove_quietly(restored)
            return
        logger.info(
            "Hidden Space book unsealed",
            extra={"event": "sealing.unsealed", "category": "vault", "status": "finished",
                   "file_id": subject.file_id},
        )
        reloaded = self._repository.load_subject(job.job_id)
        if reloaded is not None:
            self._finish_unseal(reloaded)

    def _finish_unseal(self, subject: SealingSubject) -> None:
        job = subject.job
        failures: list[BaseException] = []
        previous = self._book_path(job.previous_path)
        if previous is not None and previous != self._book_path(subject.storage_path):
            _remove_into(previous, failures)
        try:
            self._cover_store.delete(subject.book_id)
        except OSError as exc:
            failures.append(exc)
        self._invalidate(subject)
        if failures:
            self._repository.note_cleanup_failure(job.job_id, _failure_code(failures[0]))
            return
        self._repository.finish(job.job_id)

    def _discard_cancelled_unseal(self, subject: SealingSubject) -> None:
        """Delete the decrypted copy a cancelled unseal wrote, then drop the job."""

        job = subject.job
        current = self._book_path(subject.storage_path)
        failures: list[BaseException] = []
        for value in (job.partial_path, job.target_path):
            path = self._book_path(value)
            if path is not None and path != current:
                _remove_into(path, failures)
        _remove_into(self._restored_cover_path(subject.book_id), failures)
        if failures:
            return
        self._repository.finish(job.job_id)

    def _restore_cover(self, subject: SealingSubject) -> None:
        """Decrypt the sealed cover back to a PNG the public shelf can show."""

        sealed = self._cover_store.path_for(subject.book_id)
        if self._thumbnail_path(subject.cover_path) != sealed or not sealed.is_file():
            return
        try:
            data = self._cover_store.read(subject.book_id)
        except VaultLockedError:
            raise
        except VaultError:
            # A damaged cover costs the book its cover, not its pages. The
            # shelf regenerates one from the decrypted book.
            return
        if data is None:
            return
        restored = self._restored_cover_path(subject.book_id)
        partial = restored.with_name(restored.name + _PARTIAL_SUFFIX)
        try:
            with partial.open("wb") as handle:
                handle.write(data)
                fsync_file(handle)
            os.replace(partial, restored)
        except OSError as exc:
            _remove_quietly(partial)
            logger.warning(
                "Hidden Space could not restore a book's cover",
                extra={"event": "sealing.cover.failed", "category": "vault", "status": "failed",
                       "error_type": type(exc).__name__},
            )
        except BaseException:
            _remove_quietly(partial)
            raise

    # -- helpers ----------------------------------------------------------------

    def _written_file_matches(self, target: Path | None, job: VaultJob, algorithm: str) -> bool:
        if target is None or not target.is_file() or not job.expected_hash:
            return False
        try:
            return self._hash_service.compute(target, algorithm) == job.expected_hash
        except OSError:
            return False

    def _fail(self, job: VaultJob, code: str, exc: BaseException | None = None) -> None:
        self._repository.fail_job(job.job_id, code)
        logger.warning(
            "Hidden Space job stopped",
            extra={"event": "sealing.job.failed", "category": "vault", "status": "failed",
                   "direction": job.direction.value, "error_code": code,
                   "error_type": type(exc).__name__ if exc is not None else None},
        )

    def _invalidate(self, subject: SealingSubject) -> None:
        try:
            if self._invalidate_artifacts is not None:
                self._invalidate_artifacts(subject.file_id, (subject.book_id,))
            if self._invalidate_cover is not None:
                self._invalidate_cover(subject.book_id)
        except Exception:
            logger.warning("Hidden Space could not invalidate a book's caches", exc_info=True)

    def _book_path(self, relative: str | None) -> Path | None:
        """A storage-relative path, if it resolves inside ``Books/``."""

        return self._managed_path(relative, self._paths.paths.books)

    def _thumbnail_path(self, relative: str | None) -> Path | None:
        return self._managed_path(relative, self._paths.paths.thumbnails)

    def _managed_path(self, relative: str | None, root: Path) -> Path | None:
        if not relative:
            return None
        try:
            absolute = self._paths.resolver.to_storage_absolute(relative)
            absolute.relative_to(root.resolve())
        except ValueError:
            return None
        return absolute

    def _relative(self, path: Path) -> str:
        return self._paths.resolver.to_storage_relative(path)

    def _covers_dir(self) -> Path:
        return self._cover_store.covers_dir

    def _plain_covers(self, book_id: str) -> tuple[Path, ...]:
        try:
            return tuple(self._covers_dir().glob(f"{book_id}-*.png"))
        except OSError:
            return ()

    def _largest_generated_cover(self, book_id: str) -> Path | None:
        best: tuple[int, Path] | None = None
        try:
            candidates = tuple(self._covers_dir().glob(f"{book_id}-generated-*.png"))
        except OSError:
            return None
        for path in candidates:
            dimensions = path.stem.rsplit("-", 1)[-1].split("x")
            try:
                area = int(dimensions[0]) * int(dimensions[1])
            except (IndexError, ValueError):
                area = 0
            if best is None or area > best[0]:
                best = (area, path)
        return best[1] if best is not None else None

    def _restored_cover_path(self, book_id: str) -> Path:
        return self._covers_dir() / f"{book_id}{RESTORED_COVER_SUFFIX}"

    def _wait_for_progress(self, timeout: float) -> bool:
        """Wait for the worker to finish a job. False if there is no worker to wait for."""

        if timeout <= 0:
            return False
        with self._state:
            if self._thread is None or self._closed:
                return False
            seen = self._progress
            self._state.wait_for(lambda: self._progress != seen or self._closed, timeout)
            return self._progress != seen

    def _notify(self, book_ids: tuple[str, ...]) -> None:
        with self._state:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(book_ids)
            except Exception:
                logger.warning("Hidden Space job listener failed", exc_info=True)


def _seal_plan(subject: SealingSubject) -> _SealPlan | None:
    suffix = PurePosixPath(subject.storage_path).suffix.lower()
    if suffix in PDF_EXTENSIONS:
        return _SealPlan(ContentType.PDF, convert=False, file_format="PDF")
    if suffix in ARCHIVE_EXTENSIONS:
        canonical = subject.storage_kind == STORAGE_KIND_CANONICAL and suffix == CBZ_SUFFIX
        return _SealPlan(ContentType.CBZ, convert=not canonical, file_format="CBZ")
    return None


def _sealed_file_format(subject: SealingSubject) -> str:
    plan = _seal_plan(subject)
    return plan.file_format if plan is not None else subject.file_format


def _unseal_plan(content_type: ContentType) -> _UnsealPlan | None:
    if content_type is ContentType.CBZ:
        return _UnsealPlan(STORAGE_KIND_CANONICAL, CBZ_SUFFIX, "CBZ")
    if content_type is ContentType.PDF:
        # A PDF is never repackaged, so it goes back to being the user's own
        # file, content-addressed by the hash it was imported under.
        return _UnsealPlan(STORAGE_KIND_VERBATIM, ".pdf", "PDF")
    return None


def _unseal_plan_for_suffix(suffix: str) -> _UnsealPlan:
    return _unseal_plan(ContentType.PDF if suffix in PDF_EXTENSIONS else ContentType.CBZ)  # type: ignore[return-value]


def _is_cancellation(exc: BaseException) -> bool:
    return isinstance(exc, (CanonicalWriteCancelled, ArchiveCancelled, VaultOperationCancelled))


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, _JobFailure):
        return exc.code
    if isinstance(exc, SourceChangedError):
        return "source_changed"
    if isinstance(exc, VaultError):
        return exc.code
    if isinstance(exc, ArchiveError):
        return "conversion_failed"
    if isinstance(exc, OSError):
        if exc.errno == errno.ENOSPC:
            return "disk_full"
        if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EBUSY):
            return "file_in_use"
        return "io_error"
    return "unexpected"


def _remove_quietly(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Hidden Space could not remove a working file", exc_info=True)


def _remove_into(path: Path, failures: list[BaseException]) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        failures.append(exc)


__all__ = [
    "DEFAULT_UNHIDE_WAIT_SECONDS",
    "RESTORED_COVER_SUFFIX",
    "SealingService",
    "UnhideOutcome",
]
