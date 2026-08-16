"""Manual integrity auditing and crash-safe repair for managed libraries.

This module deliberately has no Qt dependency.  A caller may run ``scan`` in
``TaskService`` and present its immutable plan to a user before calling
``apply``.  Filesystem changes are limited to regular files rooted in the
managed ``Books`` / generated-cover directories; symlinks and any path outside
those roots are reported but never followed or removed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import hashlib
import json
import logging
from pathlib import Path, PurePosixPath
from threading import Lock
from time import perf_counter
from typing import Any
from uuid import uuid4

from joyread.core.archive import ArchiveImageService, ArchiveOpenLimits, ArchiveValidationCode
from joyread.core.file_types import ARCHIVE_EXTENSIONS
from joyread.core.reader.pdf import PDF_EXTENSIONS, PdfImageServicePort
from joyread.core.services.archive_extraction_pool import (
    ArchiveExtractionCache,
    archive_cache_storage_key,
)
from joyread.core.services.hash_service import HashService
from joyread.infrastructure.database.database_interpreter import DatabaseInterpreter, DatabasePriority
from joyread.infrastructure.filesystem.path_service import PathService


logger = logging.getLogger(__name__)

#: Files the operating system's file manager writes into any folder a user
#: browses. They are not library content and never will be, so an audit that
#: reported them would offer the user a cleanup that Finder undoes the next
#: time they open the folder.
_PLATFORM_METADATA_NAMES: frozenset[str] = frozenset(
    {".DS_Store", "Thumbs.db", "desktop.ini"}
)

#: macOS AppleDouble sidecars, written when copying to a filesystem without
#: native extended-attribute support.
_PLATFORM_METADATA_PREFIXES: tuple[str, ...] = ("._",)

#: Folded once here rather than per call: the audit asks about every file in
#: the Books tree, so a comprehension inside the predicate would allocate a set
#: per file for a value that never changes.
_PLATFORM_METADATA_LOWERED: frozenset[str] = frozenset(
    name.lower() for name in _PLATFORM_METADATA_NAMES
)


def is_platform_metadata(filename: str) -> bool:
    """Whether a filename is file-manager metadata rather than library content.

    Matched case-insensitively: ``Thumbs.db`` and ``thumbs.db`` are the same
    file on the platforms that create it.
    """

    if filename.lower() in _PLATFORM_METADATA_LOWERED:
        return True
    return filename.startswith(_PLATFORM_METADATA_PREFIXES)


class LibraryAuditAction(StrEnum):
    """The auditable state observed for one ``book_files`` row."""

    HEALTHY = "healthy"
    REPAIRED = "repaired"
    MISSING = "missing"
    CHANGED = "changed"
    MERGE = "merge"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class LibraryAuditItem:
    file_id: str
    storage_path: Path | None
    storage_relative_path: str
    file_format: str
    hash_algorithm: str
    recorded_hash: str
    observed_hash: str | None
    recorded_state: str
    action: LibraryAuditAction
    public_book_ids: tuple[str, ...]
    private_book_ids: tuple[str, ...]
    probe_code: str | None = None
    message: str | None = None
    duplicate_file_id: str | None = None

    @property
    def book_ids(self) -> tuple[str, ...]:
        return self.public_book_ids + self.private_book_ids

    @property
    def needs_apply(self) -> bool:
        return self.action is not LibraryAuditAction.HEALTHY


@dataclass(frozen=True)
class LibraryAuditOrphan:
    path: Path
    byte_size: int
    kind: str


@dataclass(frozen=True)
class LibraryAuditPlan:
    operation_id: str
    storage_root: Path
    items: tuple[LibraryAuditItem, ...]
    orphan_files: tuple[LibraryAuditOrphan, ...]
    orphan_cache_files: tuple[LibraryAuditOrphan, ...]
    reclaimable_bytes: int

    @property
    def changed_count(self) -> int:
        return sum(item.action is LibraryAuditAction.CHANGED for item in self.items)

    @property
    def merge_count(self) -> int:
        return sum(item.action is LibraryAuditAction.MERGE for item in self.items)

    @property
    def missing_count(self) -> int:
        return sum(item.action is LibraryAuditAction.MISSING for item in self.items)

    @property
    def unavailable_count(self) -> int:
        return sum(item.action is LibraryAuditAction.UNAVAILABLE for item in self.items)

    @property
    def repair_count(self) -> int:
        return sum(item.action is LibraryAuditAction.REPAIRED for item in self.items)

    @property
    def has_changes(self) -> bool:
        return any(item.needs_apply for item in self.items) or bool(
            self.orphan_files or self.orphan_cache_files
        )


@dataclass(frozen=True)
class LibraryAuditReport:
    operation_id: str
    changed_count: int
    merged_count: int
    missing_count: int
    unavailable_count: int
    repaired_count: int
    cleaned_file_count: int
    cleaned_cache_count: int
    reclaimed_bytes: int
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class LibraryMaintenanceRecoveryReport:
    recovered_count: int
    discarded_count: int
    conflicts: tuple[str, ...] = ()


class LibraryMaintenanceLease:
    """An exclusive maintenance permit that may cross a Qt worker/UI boundary.

    Storage changes close the old database in a worker, but replacement
    services must be wired on the UI thread.  A plain ``threading.Lock`` can
    be released by either thread, so this lease keeps import/audit work blocked
    until that hand-off has completed.  ``release`` is deliberately idempotent
    because both normal and failure UI paths need to clean it up.
    """

    def __init__(self, lock: Lock, operation: str) -> None:
        self._lock = lock
        self.operation = operation
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._lock.release()
        logger.debug("Library maintenance gate released operation=%s", self.operation)


class LibraryMaintenanceCoordinator:
    """One process-wide gate for import, audit, and storage mutations."""

    def __init__(self) -> None:
        self._lock = Lock()

    def acquire(self, operation: str) -> LibraryMaintenanceLease:
        """Acquire a lease for work that finishes in another event-loop phase."""

        logger.debug("Library maintenance gate waiting operation=%s", operation)
        self._lock.acquire()
        logger.debug("Library maintenance gate acquired operation=%s", operation)
        return LibraryMaintenanceLease(self._lock, operation)

    @contextmanager
    def hold(self, operation: str) -> Iterator[None]:
        lease = self.acquire(operation)
        try:
            yield
        finally:
            lease.release()


class LibraryMaintenanceService:
    """Audit content-addressed managed files and apply an approved repair plan."""

    def __init__(
        self,
        paths: PathService,
        database: DatabaseInterpreter,
        hash_service: HashService,
        archive_service: ArchiveImageService,
        *,
        pdf_service: PdfImageServicePort | None = None,
        archive_limits: ArchiveOpenLimits | None = None,
        extraction_cache: ArchiveExtractionCache | None = None,
        invalidate_file_cache: Callable[[str], None] | None = None,
        coordinator: LibraryMaintenanceCoordinator | None = None,
    ) -> None:
        self._paths = paths
        self._database = database
        self._hash_service = hash_service
        self._archive_service = archive_service
        self._pdf_service = pdf_service
        self._archive_limits = archive_limits or ArchiveOpenLimits()
        self._extraction_cache = extraction_cache
        self._invalidate_file_cache = invalidate_file_cache
        self._coordinator = coordinator or LibraryMaintenanceCoordinator()

    def set_archive_open_limits(self, limits: ArchiveOpenLimits) -> None:
        self._archive_limits = limits

    def scan(self) -> LibraryAuditPlan:
        """Hash and probe every managed file without changing disk or database."""

        operation_id = uuid4().hex
        started = perf_counter()
        with self._coordinator.hold("library-audit-scan"):
            limits = self._archive_limits
            rows = self._database.execute(_load_audit_rows, DatabasePriority.HIGH)
            items = self._scan_items(rows, limits)
            orphan_files = self._find_book_orphans(items)
            orphan_cache_files = self._find_cache_orphans(items)
        reclaimable = sum(item.byte_size for item in orphan_files + orphan_cache_files)
        plan = LibraryAuditPlan(
            operation_id=operation_id,
            storage_root=self._paths.storage_root,
            items=tuple(items),
            orphan_files=tuple(orphan_files),
            orphan_cache_files=tuple(orphan_cache_files),
            reclaimable_bytes=reclaimable,
        )
        logger.info(
            "Library audit scan operation=%s files=%d changed=%d merged=%d missing=%d unavailable=%d "
            "orphans=%d cache_orphans=%d elapsed_ms=%.0f",
            operation_id,
            len(plan.items),
            plan.changed_count,
            plan.merge_count,
            plan.missing_count,
            plan.unavailable_count,
            len(plan.orphan_files),
            len(plan.orphan_cache_files),
            (perf_counter() - started) * 1000.0,
        )
        return plan

    def apply(self, plan: LibraryAuditPlan) -> LibraryAuditReport:
        """Apply a user-approved plan, rechecking each mutable candidate first."""

        if plan.storage_root != self._paths.storage_root:
            raise ValueError("Library audit plan belongs to a different storage root.")

        changed = merged = missing = unavailable = repaired = 0
        cleaned_file = cleaned_cache = reclaimed = 0
        skipped: list[str] = []
        errors: list[str] = []
        with self._coordinator.hold("library-audit-apply"):
            limits = self._archive_limits
            for item in plan.items:
                try:
                    outcome = self._apply_item(item, limits)
                except Exception as exc:  # Keep one bad file from aborting cleanup for the rest.
                    logger.exception(
                        "Library audit apply failed operation=%s file_id=%s",
                        plan.operation_id,
                        item.file_id,
                    )
                    errors.append(f"{item.file_id}: {type(exc).__name__}")
                    continue
                if outcome == LibraryAuditAction.CHANGED:
                    changed += 1
                elif outcome == LibraryAuditAction.MERGE:
                    merged += 1
                elif outcome == LibraryAuditAction.MISSING:
                    missing += 1
                elif outcome == LibraryAuditAction.UNAVAILABLE:
                    unavailable += 1
                elif outcome == LibraryAuditAction.REPAIRED:
                    repaired += 1
                elif outcome is None and item.needs_apply:
                    skipped.append(item.file_id)

            referenced = self._referenced_storage_paths()
            for orphan in plan.orphan_files:
                if orphan.path in referenced:
                    continue
                if self._unlink_regular_file(orphan.path, self._paths.paths.books):
                    cleaned_file += 1
                    reclaimed += orphan.byte_size
            for orphan in plan.orphan_cache_files:
                if self._unlink_cache_file(orphan.path):
                    cleaned_cache += 1
                    reclaimed += orphan.byte_size

        report = LibraryAuditReport(
            operation_id=plan.operation_id,
            changed_count=changed,
            merged_count=merged,
            missing_count=missing,
            unavailable_count=unavailable,
            repaired_count=repaired,
            cleaned_file_count=cleaned_file,
            cleaned_cache_count=cleaned_cache,
            reclaimed_bytes=reclaimed,
            skipped=tuple(skipped),
            errors=tuple(errors),
        )
        logger.info(
            "Library audit apply operation=%s changed=%d merged=%d missing=%d unavailable=%d repaired=%d "
            "cleaned=%d cache_cleaned=%d skipped=%d errors=%d",
            report.operation_id,
            report.changed_count,
            report.merged_count,
            report.missing_count,
            report.unavailable_count,
            report.repaired_count,
            report.cleaned_file_count,
            report.cleaned_cache_count,
            len(report.skipped),
            len(report.errors),
        )
        return report

    def recover_pending_journal(self) -> LibraryMaintenanceRecoveryReport:
        """Finish or discard deterministically recoverable interrupted renames."""

        recovered = discarded = 0
        conflicts: list[str] = []
        with self._coordinator.hold("library-maintenance-recovery"):
            rows = self._database.execute(_load_pending_journal, DatabasePriority.HIGH)
            for row in rows:
                journal_id = str(row["journal_id"])
                source = self._storage_path(str(row["from_storage_path"]))
                target = self._storage_path(str(row["to_storage_path"]))
                if source is None or target is None:
                    conflicts.append(journal_id)
                    continue
                source_exists = self._is_regular_file(source, self._paths.paths.books)
                target_exists = self._is_regular_file(target, self._paths.paths.books)
                if source_exists and not target_exists:
                    self._database.execute(
                        lambda connection, journal_id=journal_id: connection.execute(
                            "DELETE FROM library_maintenance_journal WHERE journal_id = ?",
                            (journal_id,),
                        ),
                        DatabasePriority.NORMAL,
                    )
                    discarded += 1
                    continue
                if target_exists and not source_exists:
                    try:
                        payload = json.loads(str(row["payload_json"]))
                        expected_hash = str(payload["content_hash"])
                        algorithm = str(row["hash_algorithm"])
                        if self._hash_service.compute(target, algorithm) != expected_hash:
                            conflicts.append(journal_id)
                            continue
                        affected = self._complete_rename_journal(
                            journal_id,
                            str(row["file_id"]),
                            expected_hash,
                            str(row["to_storage_path"]),
                        )
                    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                        conflicts.append(journal_id)
                        continue
                    self._invalidate_artifacts(str(row["file_id"]), affected)
                    recovered += 1
                    continue
                conflicts.append(journal_id)
        if recovered or discarded or conflicts:
            logger.warning(
                "Library maintenance journal recovery recovered=%d discarded=%d conflicts=%d",
                recovered,
                discarded,
                len(conflicts),
            )
        return LibraryMaintenanceRecoveryReport(recovered, discarded, tuple(conflicts))

    def _scan_items(
        self,
        rows: tuple[dict[str, Any], ...],
        limits: ArchiveOpenLimits,
    ) -> list[LibraryAuditItem]:
        known_hashes = {
            (str(row["hash_algorithm"]), str(row["content_hash"])): str(row["file_id"])
            for row in rows
        }
        items: list[LibraryAuditItem] = []
        for row in rows:
            file_id = str(row["file_id"])
            storage_relative = str(row["storage_path"])
            path = self._storage_path(storage_relative)
            public_ids = tuple(sorted(str(value) for value in row["public_book_ids"]))
            private_ids = tuple(sorted(str(value) for value in row["private_book_ids"]))
            base = dict(
                file_id=file_id,
                storage_path=path,
                storage_relative_path=storage_relative,
                file_format=str(row["file_format"]),
                hash_algorithm=str(row["hash_algorithm"]),
                recorded_hash=str(row["content_hash"]),
                recorded_state=str(row["state"]),
                public_book_ids=public_ids,
                private_book_ids=private_ids,
            )
            if path is None or not self._is_regular_file(path, self._paths.paths.books):
                action = (
                    LibraryAuditAction.MISSING
                    if path is not None and not path.exists()
                    else LibraryAuditAction.UNAVAILABLE
                )
                items.append(
                    LibraryAuditItem(
                        **base,
                        observed_hash=None,
                        action=action,
                        probe_code="unsafe_path" if path is None or path.exists() else None,
                        message="Managed file is missing or unsafe to access.",
                    )
                )
                continue
            try:
                observed_hash = self._hash_service.compute(path, str(row["hash_algorithm"]))
            except OSError:
                items.append(
                    LibraryAuditItem(
                        **base,
                        observed_hash=None,
                        action=LibraryAuditAction.UNAVAILABLE,
                        probe_code="hash_failed",
                        message="Managed file could not be hashed.",
                    )
                )
                continue
            valid, probe_code, message = self._probe(path, limits)
            if not valid:
                items.append(
                    LibraryAuditItem(
                        **base,
                        observed_hash=observed_hash,
                        action=LibraryAuditAction.UNAVAILABLE,
                        probe_code=probe_code,
                        message=message,
                    )
                )
                continue
            if observed_hash != str(row["content_hash"]):
                duplicate_file_id = known_hashes.get((str(row["hash_algorithm"]), observed_hash))
                action = (
                    LibraryAuditAction.MERGE
                    if duplicate_file_id is not None and duplicate_file_id != file_id
                    else LibraryAuditAction.CHANGED
                )
                items.append(
                    LibraryAuditItem(
                        **base,
                        observed_hash=observed_hash,
                        action=action,
                        duplicate_file_id=duplicate_file_id,
                    )
                )
                continue
            action = (
                LibraryAuditAction.REPAIRED
                if row["state"] != "healthy" or row["integrity_error_code"] is not None
                else LibraryAuditAction.HEALTHY
            )
            items.append(LibraryAuditItem(**base, observed_hash=observed_hash, action=action))
        return items

    def _apply_item(
        self,
        item: LibraryAuditItem,
        limits: ArchiveOpenLimits,
    ) -> LibraryAuditAction | None:
        if item.action is LibraryAuditAction.HEALTHY:
            return None
        path = item.storage_path
        if item.action is LibraryAuditAction.MISSING:
            if path is None or path.exists():
                return None
            self._set_file_state(item.file_id, "missing", None)
            self._invalidate_artifacts(item.file_id, item.book_ids)
            return LibraryAuditAction.MISSING
        if path is None or not self._is_regular_file(path, self._paths.paths.books):
            self._set_file_state(item.file_id, "unavailable", "unsafe_path")
            self._invalidate_artifacts(item.file_id, item.book_ids)
            return LibraryAuditAction.UNAVAILABLE

        try:
            observed_hash = self._hash_service.compute(path, item.hash_algorithm)
        except OSError:
            self._set_file_state(item.file_id, "unavailable", "hash_failed")
            self._invalidate_artifacts(item.file_id, item.book_ids)
            return LibraryAuditAction.UNAVAILABLE
        if observed_hash != item.observed_hash:
            return None
        valid, probe_code, _message = self._probe(path, limits)
        if not valid:
            self._set_file_state(item.file_id, "unavailable", probe_code)
            self._invalidate_artifacts(item.file_id, item.book_ids)
            return LibraryAuditAction.UNAVAILABLE
        if observed_hash == item.recorded_hash:
            self._set_file_state(item.file_id, "healthy", None)
            return LibraryAuditAction.REPAIRED

        duplicate = self._find_file_by_hash(item.hash_algorithm, observed_hash, item.file_id)
        if duplicate is not None:
            affected = self._merge_duplicate_file(item.file_id, str(duplicate["file_id"]))
            self._unlink_regular_file(path, self._paths.paths.books)
            self._invalidate_artifacts(item.file_id, affected)
            return LibraryAuditAction.MERGE

        target = self._content_addressed_target(path, observed_hash)
        if target.exists():
            if not self._is_regular_file(target, self._paths.paths.books):
                return None
            try:
                target_hash = self._hash_service.compute(target, item.hash_algorithm)
            except OSError:
                return None
            if target_hash != observed_hash:
                return None
            affected = self._update_changed_file(
                item.file_id,
                observed_hash,
                self._paths.resolver.to_storage_relative(target),
                journal_id=None,
            )
            self._unlink_regular_file(path, self._paths.paths.books)
            self._invalidate_artifacts(item.file_id, affected)
            return LibraryAuditAction.CHANGED

        target.parent.mkdir(parents=True, exist_ok=True)
        journal_id = uuid4().hex
        target_relative = self._paths.resolver.to_storage_relative(target)
        self._create_rename_journal(
            journal_id,
            item.file_id,
            item.storage_relative_path,
            target_relative,
            observed_hash,
        )
        try:
            path.replace(target)
        except OSError:
            self._delete_journal(journal_id)
            raise
        affected = self._complete_rename_journal(
            journal_id,
            item.file_id,
            observed_hash,
            target_relative,
        )
        self._invalidate_artifacts(item.file_id, affected)
        return LibraryAuditAction.CHANGED

    def _probe(
        self,
        path: Path,
        limits: ArchiveOpenLimits,
    ) -> tuple[bool, str | None, str | None]:
        try:
            suffix = path.suffix.lower()
            if suffix in ARCHIVE_EXTENSIONS:
                probe = self._archive_service.probe_archive(path, limits=limits)
                return probe.is_valid, probe.code.value, probe.message
            if suffix in PDF_EXTENSIONS:
                if self._pdf_service is None:
                    return False, "pdf_backend_unavailable", "PDF support is unavailable in this runtime."
                probe = self._pdf_service.probe_pdf(path)
                return probe.is_valid, probe.error_type, probe.message
        except Exception as exc:  # A malformed third-party container must not abort the whole audit.
            logger.warning("Library audit probe failed path=%s error=%s", path, type(exc).__name__)
            return False, type(exc).__name__, "Managed file could not be probed."
        return False, "unsupported_format", "Managed file has an unsupported format."

    def _content_addressed_target(self, source: Path, content_hash: str) -> Path:
        suffix = source.suffix.lower()
        return self._paths.paths.books / content_hash[:2] / f"{content_hash}{suffix}"

    def _storage_path(self, value: str) -> Path | None:
        raw = str(value or "")
        try:
            relative = PurePosixPath(raw)
        except TypeError:
            return None
        if not raw or relative.is_absolute() or any(part == ".." for part in relative.parts):
            return None
        candidate = self._paths.storage_root.joinpath(*relative.parts)
        books_root = self._paths.paths.books
        try:
            candidate.relative_to(books_root)
        except ValueError:
            return None
        current = self._paths.storage_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return None
        return candidate

    def _is_regular_file(self, path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        current = root
        try:
            for part in path.relative_to(root).parts:
                current = current / part
                if current.is_symlink():
                    return False
            return path.is_file() and not path.is_symlink()
        except OSError:
            return False

    def _unlink_regular_file(self, path: Path, root: Path) -> bool:
        if not self._is_regular_file(path, root):
            return False
        try:
            path.unlink()
        except OSError:
            return False
        return True

    def _unlink_cache_file(self, path: Path) -> bool:
        """Delete a regular orphan only from a known generated-cache root."""

        return self._unlink_regular_file(path, self._paths.paths.thumbnails) or self._unlink_regular_file(
            path,
            self._paths.paths.cache,
        )

    def _find_book_orphans(self, items: list[LibraryAuditItem]) -> list[LibraryAuditOrphan]:
        root = self._paths.paths.books
        referenced = {item.storage_path for item in items if item.storage_path is not None}
        return self._find_orphans(root, referenced, kind="book")

    def _find_cache_orphans(self, items: list[LibraryAuditItem]) -> list[LibraryAuditOrphan]:
        covers = self._find_generated_cover_orphans(items)
        extraction = self._find_extraction_cache_orphans(items)
        return sorted(covers + extraction, key=lambda orphan: str(orphan.path))

    def _find_generated_cover_orphans(self, items: list[LibraryAuditItem]) -> list[LibraryAuditOrphan]:
        root = self._paths.paths.thumbnails / "covers"
        known_book_ids = {book_id for item in items for book_id in item.book_ids}
        orphans: list[LibraryAuditOrphan] = []
        if not root.exists():
            return orphans
        try:
            candidates = tuple(root.glob("*-generated-*.png"))
        except OSError:
            return orphans
        for path in candidates:
            if not self._is_regular_file(path, self._paths.paths.thumbnails):
                continue
            book_id = path.name.split("-generated-", 1)[0]
            if book_id in known_book_ids:
                continue
            try:
                byte_size = path.stat().st_size
            except OSError:
                continue
            orphans.append(LibraryAuditOrphan(path, byte_size, "cache"))
        return sorted(orphans, key=lambda orphan: str(orphan.path))

    def _find_extraction_cache_orphans(self, items: list[LibraryAuditItem]) -> list[LibraryAuditOrphan]:
        """Find stale managed cache entries whose ``file:<id>`` has no row.

        Content-addressed external caches are legitimate global LRU data and
        are not database orphans. Ephemeral entries are reclaimed by pool
        startup reconciliation. Only the managed ``m-`` namespace participates
        in this audit.
        """

        known_keys = {archive_cache_storage_key(f"file:{item.file_id}") for item in items}
        orphans: list[LibraryAuditOrphan] = []
        zip_root = self._paths.paths.cache / ".archive_zip_bundles"
        if zip_root.exists():
            try:
                candidates = tuple(zip_root.iterdir())
            except OSError:
                candidates = ()
            for path in candidates:
                if not self._is_regular_file(path, self._paths.paths.cache):
                    continue
                key = _zip_bundle_key(path.name)
                if key is None or not key.startswith("m-") or key in known_keys:
                    continue
                orphan = _orphan_from_path(path, "extraction-cache")
                if orphan is not None:
                    orphans.append(orphan)

        hidden_root = self._paths.paths.cache / ".archive_image_pages"
        if hidden_root.exists():
            try:
                directories = tuple(hidden_root.iterdir())
            except OSError:
                directories = ()
            for directory in directories:
                if not directory.name.startswith("m-") or directory.name in known_keys:
                    continue
                try:
                    files = tuple(directory.rglob("*")) if directory.is_dir() and not directory.is_symlink() else ()
                except OSError:
                    files = ()
                for path in files:
                    if not self._is_regular_file(path, self._paths.paths.cache):
                        continue
                    orphan = _orphan_from_path(path, "extraction-cache")
                    if orphan is not None:
                        orphans.append(orphan)
        return orphans

    def _find_orphans(
        self,
        root: Path,
        referenced: set[Path],
        *,
        kind: str,
    ) -> list[LibraryAuditOrphan]:
        if not root.exists():
            return []
        try:
            candidates = tuple(root.rglob("*"))
        except OSError:
            return []
        orphans: list[LibraryAuditOrphan] = []
        for path in candidates:
            if path in referenced or not self._is_regular_file(path, root):
                continue
            if is_platform_metadata(path.name):
                # The file manager recreates these the moment the user opens
                # the folder, so reporting them produces an orphan that comes
                # back after every cleanup and never converges.
                logger.debug("Ignoring platform metadata in audit: %s", path.name)
                continue
            try:
                byte_size = path.stat().st_size
            except OSError:
                continue
            logger.debug("Audit orphan (%s): %s bytes=%d", kind, path.name, byte_size)
            orphans.append(LibraryAuditOrphan(path, byte_size, kind))
        return sorted(orphans, key=lambda orphan: str(orphan.path))

    def _referenced_storage_paths(self) -> set[Path]:
        values = self._database.execute(_load_storage_paths, DatabasePriority.HIGH)
        return {path for value in values if (path := self._storage_path(value)) is not None}

    def _set_file_state(self, file_id: str, state: str, error_code: str | None) -> None:
        now = _now()
        self._database.execute(
            lambda connection: connection.execute(
                """
                UPDATE book_files
                SET state = ?, integrity_error_code = ?, updated_at = ?
                WHERE file_id = ?
                """,
                (state, error_code, now, file_id),
            ),
            DatabasePriority.NORMAL,
        )

    def _find_file_by_hash(self, algorithm: str, content_hash: str, excluding_file_id: str):
        return self._database.execute(
            lambda connection: connection.execute(
                """
                SELECT file_id, storage_path
                FROM book_files
                WHERE hash_algorithm = ? AND content_hash = ? AND file_id != ?
                """,
                (algorithm, content_hash, excluding_file_id),
            ).fetchone(),
            DatabasePriority.HIGH,
        )

    def _create_rename_journal(
        self,
        journal_id: str,
        file_id: str,
        source_relative: str,
        target_relative: str,
        content_hash: str,
    ) -> None:
        payload = json.dumps({"content_hash": content_hash}, sort_keys=True)
        self._database.execute(
            lambda connection: connection.execute(
                """
                INSERT INTO library_maintenance_journal(
                    journal_id, operation_kind, file_id, from_storage_path,
                    to_storage_path, payload_json, created_at
                ) VALUES (?, 'rename', ?, ?, ?, ?, ?)
                """,
                (journal_id, file_id, source_relative, target_relative, payload, _now()),
            ),
            DatabasePriority.NORMAL,
        )

    def _delete_journal(self, journal_id: str) -> None:
        self._database.execute(
            lambda connection: connection.execute(
                "DELETE FROM library_maintenance_journal WHERE journal_id = ?", (journal_id,)
            ),
            DatabasePriority.NORMAL,
        )

    def _complete_rename_journal(
        self,
        journal_id: str,
        file_id: str,
        content_hash: str,
        storage_relative: str,
    ) -> tuple[str, ...]:
        return self._update_changed_file(file_id, content_hash, storage_relative, journal_id=journal_id)

    def _update_changed_file(
        self,
        file_id: str,
        content_hash: str,
        storage_relative: str,
        *,
        journal_id: str | None,
    ) -> tuple[str, ...]:
        now = _now()

        def write(connection):  # noqa: ANN001 - SQLite callback shape.
            public_ids, private_ids = _book_ids_for_file(connection, file_id)
            connection.execute("BEGIN")
            try:
                connection.execute(
                    """
                    UPDATE book_files
                    SET storage_path = ?, content_hash = ?, state = 'healthy',
                        integrity_error_code = NULL, updated_at = ?
                    WHERE file_id = ?
                    """,
                    (storage_relative, content_hash, now, file_id),
                )
                _reset_navigation_for_file(connection, public_ids, private_ids)
                if journal_id is not None:
                    connection.execute(
                        "DELETE FROM library_maintenance_journal WHERE journal_id = ?", (journal_id,)
                    )
            except Exception:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")
            return public_ids + private_ids

        return self._database.execute(write, DatabasePriority.NORMAL)

    def _merge_duplicate_file(self, old_file_id: str, target_file_id: str) -> tuple[str, ...]:
        now = _now()

        def write(connection):  # noqa: ANN001 - SQLite callback shape.
            public_ids, private_ids = _book_ids_for_file(connection, old_file_id)
            connection.execute("BEGIN")
            try:
                connection.execute(
                    "UPDATE books SET file_id = ?, updated_at = ? WHERE file_id = ?",
                    (target_file_id, now, old_file_id),
                )
                connection.execute(
                    "UPDATE private_books SET file_id = ?, updated_at = ? WHERE file_id = ?",
                    (target_file_id, now, old_file_id),
                )
                _reset_navigation_for_file(connection, public_ids, private_ids)
                connection.execute("DELETE FROM book_files WHERE file_id = ?", (old_file_id,))
            except Exception:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")
            return public_ids + private_ids

        return self._database.execute(write, DatabasePriority.NORMAL)

    def _invalidate_artifacts(self, file_id: str, book_ids: tuple[str, ...]) -> None:
        cache = self._extraction_cache
        if cache is not None:
            purge = getattr(cache, "purge", None)
            if callable(purge):
                purge(f"file:{file_id}")
        if self._invalidate_file_cache is not None:
            self._invalidate_file_cache(file_id)
        covers_root = self._paths.paths.thumbnails / "covers"
        for book_id in book_ids:
            try:
                candidates = tuple(covers_root.glob(f"{book_id}-generated-*.png"))
            except OSError:
                continue
            for cover in candidates:
                self._unlink_regular_file(cover, self._paths.paths.thumbnails)


def _load_audit_rows(connection) -> tuple[dict[str, Any], ...]:  # noqa: ANN001 - SQLite callback shape.
    file_rows = connection.execute(
        """
        SELECT
            file_id, storage_path, file_format, hash_algorithm, content_hash,
            state, integrity_error_code
        FROM book_files
        ORDER BY file_id
        """
    ).fetchall()
    public_by_file: dict[str, list[str]] = {}
    for row in connection.execute("SELECT file_id, book_id FROM books").fetchall():
        public_by_file.setdefault(str(row["file_id"]), []).append(str(row["book_id"]))
    private_by_file: dict[str, list[str]] = {}
    for row in connection.execute("SELECT file_id, private_book_id FROM private_books").fetchall():
        private_by_file.setdefault(str(row["file_id"]), []).append(str(row["private_book_id"]))
    return tuple(
        {
            "file_id": str(row["file_id"]),
            "storage_path": str(row["storage_path"]),
            "file_format": str(row["file_format"]),
            "hash_algorithm": str(row["hash_algorithm"]),
            "content_hash": str(row["content_hash"]),
            "state": str(row["state"]),
            "integrity_error_code": row["integrity_error_code"],
            "public_book_ids": tuple(public_by_file.get(str(row["file_id"]), ())),
            "private_book_ids": tuple(private_by_file.get(str(row["file_id"]), ())),
        }
        for row in file_rows
    )


def _load_storage_paths(connection) -> tuple[str, ...]:  # noqa: ANN001 - SQLite callback shape.
    return tuple(
        str(row["storage_path"])
        for row in connection.execute("SELECT storage_path FROM book_files").fetchall()
    )


def _load_pending_journal(connection):  # noqa: ANN001 - SQLite callback shape.
    return connection.execute(
        """
        SELECT journal.journal_id, journal.operation_kind, journal.file_id,
               journal.from_storage_path, journal.to_storage_path,
               journal.payload_json, files.hash_algorithm
        FROM library_maintenance_journal AS journal
        LEFT JOIN book_files AS files ON files.file_id = journal.file_id
        ORDER BY journal.created_at, journal.journal_id
        """
    ).fetchall()


def _book_ids_for_file(connection, file_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:  # noqa: ANN001
    public_ids = tuple(
        str(row["book_id"])
        for row in connection.execute("SELECT book_id FROM books WHERE file_id = ?", (file_id,)).fetchall()
    )
    private_ids = tuple(
        str(row["private_book_id"])
        for row in connection.execute(
            "SELECT private_book_id FROM private_books WHERE file_id = ?", (file_id,)
        ).fetchall()
    )
    return public_ids, private_ids


def _reset_navigation_for_file(
    connection,
    public_ids: tuple[str, ...],
    private_ids: tuple[str, ...],
) -> None:  # noqa: ANN001 - SQLite callback shape.
    if public_ids:
        placeholders = ", ".join("?" for _book_id in public_ids)
        connection.execute(
            f"DELETE FROM progress WHERE book_scope = 'public' AND book_id IN ({placeholders})",
            public_ids,
        )
        connection.execute(
            f"DELETE FROM bookmarks WHERE book_scope = 'public' AND book_id IN ({placeholders})",
            public_ids,
        )
        connection.execute(f"DELETE FROM recent_books WHERE book_id IN ({placeholders})", public_ids)
    if private_ids:
        placeholders = ", ".join("?" for _book_id in private_ids)
        connection.execute(
            f"DELETE FROM progress WHERE book_scope = 'private' AND book_id IN ({placeholders})",
            private_ids,
        )
        connection.execute(
            f"DELETE FROM bookmarks WHERE book_scope = 'private' AND book_id IN ({placeholders})",
            private_ids,
        )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _zip_bundle_key(filename: str) -> str | None:
    if filename.endswith(".partial.zip"):
        key = filename[: -len(".partial.zip")]
    elif filename.endswith(".zip"):
        key = filename[: -len(".zip")]
    else:
        return None
    if (
        len(key) != 66
        or key[:2] not in {"m-", "x-", "e-"}
        or any(character not in "0123456789abcdef" for character in key[2:])
    ):
        return None
    return key


def _orphan_from_path(path: Path, kind: str) -> LibraryAuditOrphan | None:
    try:
        return LibraryAuditOrphan(path, path.stat().st_size, kind)
    except OSError:
        return None
