"""Manifest-based JoyRead book import service."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import json
import os
from pathlib import Path
import sqlite3
from time import perf_counter
from typing import Callable
from uuid import uuid4

from joyread.core.archive import (
    ArchiveImageService,
    ArchiveOpenLimits,
    ArchiveValidationCode,
)
from joyread.core.archive.canonical import (
    CanonicalWriteCancelled,
    CanonicalWriter,
    CbzWriter,
)
from joyread.core.archive.errors import ArchiveCancelled, ArchiveError
from joyread.core.archive.inspection import ArchiveImportInspection, ImportRejection
from joyread.core.archive.metadata import read_archive_metadata, select_sidecars
from joyread.core.models.archive_metadata import BookMetadata
from joyread.core.models.import_policy import (
    DEFAULT_CANONICAL_IMPORT_POLICY,
    CanonicalImportPolicy,
    should_convert,
)
from joyread.core.models.storage_layout import (
    STORAGE_KIND_CANONICAL,
    STORAGE_KIND_VERBATIM,
    storage_target,
)
from joyread.core.archive.service import ARCHIVE_EXTENSIONS
from joyread.core.file_types import SUPPORTED_READER_EXTENSIONS
from joyread.core.operation_context import bind_operation, create_operation
from joyread.core.reader.pdf import PDF_EXTENSIONS, PdfImageServicePort
from joyread.core.services.hash_service import HashService
from joyread.core.services.library_maintenance_service import LibraryMaintenanceCoordinator
from joyread.core.services.tag_service import TagService
from joyread.infrastructure.database.database_interpreter import DatabaseInterpreter, DatabasePriority
from joyread.infrastructure.filesystem.path_service import PathService


BOOK_EXTENSIONS = SUPPORTED_READER_EXTENSIONS

logger = logging.getLogger(__name__)


class ImportStage(StrEnum):
    """Where an item is in the pipeline.

    Only ``CONVERTING`` has an honest denominator -- the page count is known
    once inspection finishes. The others stay ordinal rather than reporting a
    fabricated percentage over work whose size nobody has measured yet.

    ``EXTRACTING`` is separate from ``CONVERTING`` because it really is a
    separate wait: a container is pulled out of the source in one pass before
    any of its pages can be written, and a nested book alternates between the
    two once per container.
    """

    STAGING = "staging"
    INSPECTING = "inspecting"
    EXTRACTING = "extracting"
    CONVERTING = "converting"
    RECORDING = "recording"


@dataclass(frozen=True)
class ImportProgress:
    """One progress tick for one item of a batch."""

    stage: ImportStage
    source_path: str
    item_index: int
    item_count: int
    #: Pages written and pages expected. Both zero outside ``CONVERTING``.
    unit_done: int = 0
    unit_total: int = 0


class _ItemProgress:
    """Binds a batch's callback to one item so stages need not repeat identity.

    A plain class rather than a closure: this outlives the loop iteration that
    made it, and a closure would keep that whole frame -- ``items``, every
    interim result -- alive with it.
    """

    __slots__ = ("_callback", "_source", "_index", "_count")

    def __init__(
        self,
        callback: "Callable[[ImportProgress], None] | None",
        source: str,
        index: int,
        count: int,
    ) -> None:
        self._callback = callback
        self._source = source
        self._index = index
        self._count = count

    def stage(self, stage: ImportStage) -> None:
        self._emit(stage, 0, 0)

    def converting(self, done: int, total: int) -> None:
        self._emit(ImportStage.CONVERTING, done, total)

    def _emit(self, stage: ImportStage, done: int, total: int) -> None:
        if self._callback is None:
            return
        self._callback(
            ImportProgress(
                stage=stage,
                source_path=self._source,
                item_index=self._index,
                item_count=self._count,
                unit_done=done,
                unit_total=total,
            )
        )


@dataclass(frozen=True)
class ImportItemResult:
    source_path: str
    status: str
    book_id: str | None = None
    file_id: str | None = None
    message: str | None = None
    #: Tags that were dropped rather than linked, almost always because the
    #: library is at its tag capacity. The import still succeeded, so the status
    #: stays ``imported`` -- but "succeeded, minus some of your metadata" is not
    #: something a log line alone should carry.
    tags_rejected: int = 0


@dataclass(frozen=True)
class ImportBatchResult:
    batch_id: str
    imported_count: int
    duplicate_count: int
    skipped_count: int
    failed_count: int
    items: tuple[ImportItemResult, ...]


@dataclass(frozen=True)
class ImportPreflightResult:
    source_path: str
    can_import: bool
    status: str
    message: str | None = None
    archive_validation_code: ArchiveValidationCode | None = None


@dataclass(frozen=True)
class _ValidationFailure:
    """Internal signal that a single source file cannot be imported.

    Carries the reported ``status`` (``failed`` / ``skipped`` / etc.) and a
    user-facing ``message``. ``ImportService`` raises this instead of a
    generic exception so the batch loop can record a per-item result
    without aborting the rest of the batch.
    """

    status: str
    message: str
    archive_validation_code: ArchiveValidationCode | None = None


@dataclass(frozen=True)
class _StagedArtifact:
    """The file that will be stored, and what the row should say about it."""

    path: Path
    stored_hash: str
    storage_kind: str
    #: User-facing result text. Conversion is visible in the import list because
    #: it changed what the library holds, and a silent change is worse than a
    #: noisy one.
    message: str


class ImportService:
    """Orchestrates the multi-step process of importing books into the library.

    Each import (whether triggered from a file dialog, a folder scan, or a
    JSON manifest) walks the same pipeline: establish a supported source
    suffix, hash while copying to staging, probe only that staged managed copy,
    atomically publish it into ``Books/``, insert the database rows
    (``book_files`` + ``books`` + ``import_items``), and report a per-item
    result. The service depends on
    :class:`HashService` for content hashing,
    :class:`ArchiveImageService` / :class:`PdfImageService` for validating
    that the file is actually openable, and a :class:`DatabaseInterpreter`
    for serialized writes.

    ``verify_imported_file_integrity`` trades an extra source hash pass for a
    strong source-vs-staging comparison. The normal disabled mode still hashes
    every staged byte for content-addressed placement and duplicate detection.
    """

    def __init__(
        self,
        paths: PathService,
        database: DatabaseInterpreter,
        archive_service: ArchiveImageService,
        hash_service: HashService,
        hash_algorithm: str = "sha256",
        pdf_service: PdfImageServicePort | None = None,
        tag_service: TagService | None = None,
        archive_limits: ArchiveOpenLimits | None = None,
        verify_imported_file_integrity: bool = True,
        maintenance_coordinator: LibraryMaintenanceCoordinator | None = None,
        canonical_import_policy: CanonicalImportPolicy = DEFAULT_CANONICAL_IMPORT_POLICY,
        canonical_writer: CanonicalWriter | None = None,
    ) -> None:
        self._paths = paths
        self._database = database
        self._archive_service = archive_service
        self._pdf_service = pdf_service
        self._hash_service = hash_service
        self._hash_algorithm = hash_algorithm
        self._tag_service = tag_service
        self._archive_limits = archive_limits or ArchiveOpenLimits()
        self._verify_imported_file_integrity = bool(verify_imported_file_integrity)
        self._maintenance_coordinator = maintenance_coordinator or LibraryMaintenanceCoordinator()
        self._canonical_import_policy = canonical_import_policy
        self._canonical_writer = canonical_writer or CbzWriter()

    def set_archive_open_limits(self, limits: ArchiveOpenLimits) -> None:
        """Use new limits for later validation without disrupting active jobs."""

        self._archive_limits = limits

    def set_verify_imported_file_integrity(self, enabled: bool) -> None:
        self._verify_imported_file_integrity = bool(enabled)

    def set_canonical_import_policy(self, policy: CanonicalImportPolicy) -> None:
        """Change the policy without disrupting an import already running."""

        self._canonical_import_policy = policy

    def import_manifest(
        self,
        manifest_path: str | Path,
        *,
        nested_archive_max_depth: int | None = None,
        archive_global_file_max_depth: int | None = None,
        progress: Callable[[ImportProgress], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ImportBatchResult:
        """Import every entry in a JSON manifest file.

        Manifests are JoyRead's machine-readable batch import format used by
        scripts and the "Import from manifest" UI option. The version field
        is checked because format v1 hard-codes the schema for ``items``;
        bumping the version is a deliberate breaking change and we'd rather
        fail loudly than silently misread an unfamiliar shape.
        """

        path = Path(manifest_path).expanduser()
        logger.debug("Reading import manifest path=%s", path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if int(raw.get("version", 0)) != 1:
            raise ValueError("Unsupported import manifest version.")
        items = raw.get("items")
        if not isinstance(items, list):
            raise ValueError("Import manifest must contain an items list.")
        return self.import_items(
            items,
            manifest_path=path,
            nested_archive_max_depth=nested_archive_max_depth,
            archive_global_file_max_depth=archive_global_file_max_depth,
            progress=progress,
            is_cancelled=is_cancelled,
        )

    def import_files(
        self,
        paths: list[str | Path],
        *,
        nested_archive_max_depth: int | None = None,
        archive_global_file_max_depth: int | None = None,
        progress: Callable[[ImportProgress], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ImportBatchResult:
        """Import a list of explicit source paths chosen by the user.

        Convenience wrapper around :meth:`import_items` for the most common
        case: the user picked one or more files in a file dialog. No manifest
        is involved, so per-item metadata defaults are used.
        """

        logger.info(
            "Import files requested count=%d nested_depth=%s global_depth=%s",
            len(paths),
            nested_archive_max_depth,
            archive_global_file_max_depth,
        )
        return self.import_items(
            [{"source_path": str(path)} for path in paths],
            manifest_path=None,
            nested_archive_max_depth=nested_archive_max_depth,
            archive_global_file_max_depth=archive_global_file_max_depth,
            progress=progress,
            is_cancelled=is_cancelled,
        )

    def preflight_file(
        self,
        path: str | Path,
        *,
        nested_archive_max_depth: int | None = None,
        archive_global_file_max_depth: int | None = None,
    ) -> ImportPreflightResult:
        """Perform only source-path and supported-suffix checks.

        This method is retained for integrations that want to disable an
        import command early. It intentionally does not probe an external
        container: normal import validates only its staged managed copy.
        """

        source_path = Path(path).expanduser()
        logger.debug(
            "Import preflight start path=%s nested_depth=%s global_depth=%s",
            source_path,
            nested_archive_max_depth,
            archive_global_file_max_depth,
        )
        failure = self._validate_source_candidate(source_path)
        if failure is not None:
            logger.debug(
                "Import preflight rejected path=%s status=%s message=%s",
                source_path,
                failure.status,
                failure.message,
            )
            return ImportPreflightResult(
                source_path=str(source_path),
                can_import=False,
                status=failure.status,
                message=failure.message,
                archive_validation_code=failure.archive_validation_code,
            )
        logger.debug("Import preflight ok path=%s", source_path)
        return ImportPreflightResult(str(source_path), True, "importable")

    def import_folder(
        self,
        path: str | Path,
        *,
        max_depth: int = 1,
        nested_archive_max_depth: int | None = None,
        archive_global_file_max_depth: int | None = None,
        progress: Callable[[ImportProgress], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ImportBatchResult:
        folder = Path(path).expanduser()
        files = _supported_files_within_depth(folder, max_depth=max_depth)
        logger.info("Import folder requested path=%s depth=%d matched=%d", folder, max_depth, len(files))
        return self.import_files(
            files,
            progress=progress,
            is_cancelled=is_cancelled,
            nested_archive_max_depth=nested_archive_max_depth,
            archive_global_file_max_depth=archive_global_file_max_depth,
        )

    def import_paths(
        self,
        paths: list[str | Path],
        *,
        max_depth: int = 1,
        nested_archive_max_depth: int | None = None,
        archive_global_file_max_depth: int | None = None,
        progress: Callable[[ImportProgress], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ImportBatchResult:
        """Import a mixed selection of files and folders as one batch.

        Serves drag-and-drop, where the user hands over whatever they had
        selected in Finder. Folders are expanded with the same depth rule
        :meth:`import_folder` uses, then everything runs through a single
        :meth:`import_items` call -- one batch id, one result, one summary for
        the user. Importing each dropped item separately would work, but would
        report a separate outcome for each, which is not what one gesture means.
        """

        files: list[Path] = []
        seen: set[str] = set()

        def _add(candidate: Path) -> None:
            # Folders may overlap each other, or repeat a file dropped by name.
            # Resolve before comparing: the same file reached through a
            # symlinked folder and through its real path spells differently,
            # and on macOS so do /tmp and /private/tmp. Comparing raw strings
            # imports it twice and reports a duplicate against the user's own
            # single drop. The unresolved path is what gets imported, so a
            # symlinked source is still recorded the way the user named it.
            key = os.path.normcase(str(candidate.resolve(strict=False)))
            if key not in seen:
                seen.add(key)
                files.append(candidate)

        folder_count = 0
        skipped_unsupported = 0
        for value in paths:
            path = Path(value).expanduser()
            if path.is_dir():
                folder_count += 1
                for found in _supported_files_within_depth(path, max_depth=max_depth):
                    _add(found)
                continue
            # Same suffix rule the folder walk applies. Without it this method
            # disagrees with ``import_folder`` about what a book is: a stray
            # .txt would be reported as a failed import here and skipped
            # silently there, for the same file in the same batch.
            if path.suffix.lower() not in BOOK_EXTENSIONS:
                skipped_unsupported += 1
                continue
            _add(path)

        logger.info(
            "Import paths requested requested=%d folders=%d resolved=%d "
            "unsupported=%d depth=%d",
            len(paths),
            folder_count,
            len(files),
            skipped_unsupported,
            max_depth,
        )
        return self.import_files(
            files,
            nested_archive_max_depth=nested_archive_max_depth,
            archive_global_file_max_depth=archive_global_file_max_depth,
            progress=progress,
            is_cancelled=is_cancelled,
        )

    def import_items(
        self,
        items: list[dict[str, object]],
        *,
        manifest_path: Path | None,
        nested_archive_max_depth: int | None = None,
        archive_global_file_max_depth: int | None = None,
        progress: Callable[[ImportProgress], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ImportBatchResult:
        """Serialize a complete import batch against audit and storage moves."""

        operation = create_operation("import.batch", category="import")
        started = perf_counter()
        with bind_operation(operation):
            logger.info(
                "Import batch started",
                extra={
                    "event": "import.batch.started",
                    "category": "import",
                    "status": "started",
                    "batch_id": operation.operation_id,
                    "count": len(items),
                },
            )
            try:
                with self._maintenance_coordinator.hold("import"):
                    self._reclaim_abandoned_staging()
                    limits = self._archive_limits_for(
                        nested_archive_max_depth,
                        archive_global_file_max_depth,
                    )
                    return self._import_items_locked(
                        items,
                        batch_id=operation.operation_id,
                        started_monotonic=started,
                        manifest_path=manifest_path,
                        nested_archive_max_depth=nested_archive_max_depth,
                        archive_global_file_max_depth=archive_global_file_max_depth,
                        limits=limits,
                        progress=progress,
                        is_cancelled=is_cancelled,
                    )
            except Exception as exc:
                logger.error(
                    "Import batch failed",
                    exc_info=True,
                    extra={
                        "event": "import.batch.failed",
                        "category": "import",
                        "status": "failed",
                        "batch_id": operation.operation_id,
                        "count": len(items),
                        "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                        "error_type": type(exc).__name__,
                    },
                )
                raise

    def _import_items_locked(
        self,
        items: list[dict[str, object]],
        *,
        batch_id: str,
        started_monotonic: float,
        manifest_path: Path | None,
        nested_archive_max_depth: int | None = None,
        archive_global_file_max_depth: int | None = None,
        limits: ArchiveOpenLimits,
        progress: Callable[[ImportProgress], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ImportBatchResult:
        """Core import loop: validate, hash, copy, insert per item.

        Each entry in ``items`` is a dict with at least ``source_path``;
        optional keys (``title``, ``author``, ``language_tag``, ``tag_ids``,
        etc.) come from the manifest format. Failures on individual items
        are recorded as :class:`ImportItemResult` rows in the returned batch
        so the caller can show a per-file outcome instead of just
        "the import failed".
        """

        started_at = _now()
        manifest_display = str(manifest_path) if manifest_path is not None else None
        self._database.execute(
            lambda connection: connection.execute(
                """
                INSERT INTO import_batches(batch_id, manifest_path, started_at, status)
                VALUES (?, ?, ?, 'running')
                """,
                (batch_id, manifest_display, started_at),
            ),
            DatabasePriority.NORMAL,
        )

        results: list[ImportItemResult] = []
        manifest_dir = manifest_path.parent if manifest_path is not None else None
        for index, item in enumerate(items):
            source_value = str(item.get("source_path") or "")
            external_id = item.get("external_id")
            if is_cancelled is not None and is_cancelled():
                # Stop between items rather than recording the rest as failures.
                # Everything already imported stays imported: a cancelled batch
                # is a shorter batch, not a rolled-back one.
                logger.info(
                    "Import batch cancelled before item %d of %d", index + 1, len(items)
                )
                break
            try:
                result = self._import_one(
                    batch_id=batch_id,
                    source_path=_resolve_source_path(source_value, manifest_dir),
                    source_display=source_value,
                    external_id=str(external_id) if external_id is not None else None,
                    limits=limits,
                    report=_ItemProgress(
                        progress, source_value, index, len(items)
                    ),
                    is_cancelled=is_cancelled,
                )
            except CanonicalWriteCancelled:
                # Cancelling mid-conversion is a choice, not a defect, so the
                # item is skipped rather than failed -- and the batch stops
                # instead of carrying on into work the user just called off.
                logger.info("Import cancelled during conversion of %s", source_value)
                results.append(
                    self._record_item(
                        batch_id,
                        source_value,
                        str(external_id) if external_id is not None else None,
                        status="skipped",
                        message="Import cancelled.",
                    )
                )
                break
            except Exception as exc:
                logger.warning("Import item %s failed: %s", source_value, exc, exc_info=True)
                result = self._record_item(
                    batch_id,
                    source_value,
                    str(external_id) if external_id is not None else None,
                    status="failed",
                    message=str(exc),
                )
            logger.debug(
                "Import item %s -> %s (book_id=%s)",
                source_value,
                result.status,
                result.book_id,
            )
            # Tag association: applies once the row exists (imported or
            # duplicate); skipped for failed/skipped statuses so we don't
            # tag a book that does not own a stable book_id yet.
            if (
                self._tag_service is not None
                and result.book_id is not None
                and result.status in {"imported", "duplicate"}
            ):
                rejected = self._apply_tags(result.book_id, item.get("tags"))
                if rejected:
                    # Added, not assigned: the archive's own sidecar may already
                    # have had tags rejected during the import, and reporting
                    # only the manifest's would under-count what was dropped.
                    # The row is already written, so this rides on the returned
                    # result rather than the persisted message.
                    result = replace(
                        result, tags_rejected=result.tags_rejected + rejected
                    )
            results.append(result)

        completed_at = _now()
        final_status = "completed_with_errors" if any(item.status == "failed" for item in results) else "completed"
        self._database.execute(
            lambda connection: connection.execute(
                """
                UPDATE import_batches
                SET completed_at = ?, status = ?
                WHERE batch_id = ?
                """,
                (completed_at, final_status, batch_id),
            ),
            DatabasePriority.NORMAL,
        )
        batch_result = ImportBatchResult(
            batch_id=batch_id,
            imported_count=sum(item.status == "imported" for item in results),
            duplicate_count=sum(item.status == "duplicate" for item in results),
            skipped_count=sum(item.status == "skipped" for item in results),
            failed_count=sum(item.status == "failed" for item in results),
            items=tuple(results),
        )
        logger.log(
            logging.WARNING if batch_result.failed_count else logging.INFO,
            "Import batch finished",
            extra={
                "event": "import.batch.finished",
                "category": "import",
                "status": "completed_with_errors" if batch_result.failed_count else "finished",
                "batch_id": batch_id,
                "count": len(items),
                "imported_count": batch_result.imported_count,
                "duplicate_count": batch_result.duplicate_count,
                "skipped_count": batch_result.skipped_count,
                "failed_count": batch_result.failed_count,
                "duration_ms": round((perf_counter() - started_monotonic) * 1000.0, 3),
            },
        )
        return batch_result

    def _apply_tags(self, book_id: str, raw_tags: object) -> int:
        """Link tag names to a book, whatever named them.

        Two sources feed this: a manifest's ``tags`` list, and the tags an
        archive's own metadata sidecar carried. They get identical treatment
        on purpose -- a tag is a tag, and the capacity limit, the
        normalisation, and the rejection logging should not depend on how it
        arrived.

        Returns how many were dropped, so a caller can report an import that
        succeeded with less metadata than the archive offered.
        """

        if not isinstance(raw_tags, list):
            return 0
        tag_service = self._tag_service
        if tag_service is None:
            return 0
        created_or_reused = 0
        rejected = 0
        for entry in raw_tags:
            if not isinstance(entry, str):
                rejected += 1
                continue
            tag = tag_service.find_or_create(entry)
            if tag is None:
                rejected += 1
                continue
            try:
                tag_service.link_book(tag.tag_id, book_id)
            except Exception:
                logger.exception(
                    "Failed to link tag tag_id=%s to book_id=%s",
                    tag.tag_id,
                    book_id,
                )
                continue
            created_or_reused += 1
        if rejected:
            logger.warning(
                "Import tags for book=%s: linked=%d rejected=%d",
                book_id,
                created_or_reused,
                rejected,
            )
        else:
            logger.debug(
                "Import tags for book=%s: linked=%d",
                book_id,
                created_or_reused,
            )
        return rejected

    def _import_one(
        self,
        *,
        batch_id: str,
        source_path: Path,
        source_display: str,
        external_id: str | None,
        limits: ArchiveOpenLimits,
        report: _ItemProgress | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ImportItemResult:
        failure = self._validate_source_candidate(source_path)
        if failure is not None:
            logger.debug(
                "Import item rejected source=%s status=%s message=%s",
                source_path,
                failure.status,
                failure.message,
            )
            return self._record_item(
                batch_id,
                source_display,
                external_id,
                status=failure.status,
                message=failure.message,
            )

        size_failure = self._check_source_size(source_path, limits)
        if size_failure is not None:
            return self._record_item(
                batch_id,
                source_display,
                external_id,
                status=size_failure.status,
                message=size_failure.message,
            )

        source_hash: str | None = None
        if self._verify_imported_file_integrity:
            logger.debug("Import item pre-hashing source=%s algorithm=%s", source_path, self._hash_algorithm)
            source_hash = self._hash_service.compute(source_path, self._hash_algorithm)
            duplicate = self._find_duplicate(source_hash)
            if duplicate is not None:
                return self._duplicate_result(batch_id, source_display, external_id, duplicate)

        if report is not None:
            report.stage(ImportStage.STAGING)
        staging_path = self._staging_path(source_path)
        try:
            staged_hash = self._hash_service.copy_with_hash(
                source_path,
                staging_path,
                self._hash_algorithm,
            )
        except Exception:
            staging_path.unlink(missing_ok=True)
            raise

        if source_hash is not None and source_hash != staged_hash:
            staging_path.unlink(missing_ok=True)
            return self._record_item(
                batch_id,
                source_display,
                external_id,
                status="failed",
                message="Source file changed while it was being imported.",
            )

        duplicate = self._find_duplicate(staged_hash)
        if duplicate is not None:
            staging_path.unlink(missing_ok=True)
            return self._duplicate_result(batch_id, source_display, external_id, duplicate)

        if report is not None:
            report.stage(ImportStage.INSPECTING)
        staged_failure, inspection = self._validate_staged_file(staging_path, limits)
        if staged_failure is not None:
            staging_path.unlink(missing_ok=True)
            return self._record_item(
                batch_id,
                source_display,
                external_id,
                status=staged_failure.status,
                message=staged_failure.message,
            )

        metadata = (
            read_archive_metadata(inspection.metadata_entries)
            if inspection is not None
            else BookMetadata()
        )
        file_id = str(uuid4())
        try:
            artifact = self._stage_artifact(
                staging_path,
                staged_hash,
                inspection=inspection,
                limits=limits,
                is_cancelled=is_cancelled,
                report=report,
            )
        except Exception:
            staging_path.unlink(missing_ok=True)
            raise

        try:
            storage_path, created_target = self._publish_staging(
                artifact.path, file_id, artifact.stored_hash, artifact.storage_kind
            )
        except Exception:
            artifact.path.unlink(missing_ok=True)
            raise
        if report is not None:
            report.stage(ImportStage.RECORDING)
        book_id = str(uuid4())
        now = _now()
        file_format = artifact.path.suffix.lstrip(".").upper()
        book_type = _book_type_for_suffix(artifact.path.suffix.lower())
        # Persist the managed file location relative to the storage root so the
        # whole library folder can be moved or re-pointed without rewriting rows.
        relative_storage_path = self._paths.resolver.to_storage_relative(storage_path)
        try:
            self._database.execute(
                lambda connection: _insert_imported_book(
                    connection,
                    file_id=file_id,
                    book_id=book_id,
                    original_path=str(source_path),
                    original_file_name=source_path.name,
                    storage_path=relative_storage_path,
                    file_format=file_format,
                    hash_algorithm=self._hash_algorithm,
                    # Two hashes because they answer different questions. The
                    # source hash identifies what the user handed over, so it
                    # keeps naming the same book after conversion repackages it;
                    # the stored hash is the artifact's own integrity baseline.
                    # A verbatim import makes them equal, which is why the split
                    # is invisible until something actually repackages.
                    source_hash=staged_hash,
                    stored_hash=artifact.stored_hash,
                    storage_kind=artifact.storage_kind,
                    title=metadata.preferred_title or source_path.stem,
                    author=metadata.author,
                    language_tag=metadata.language_tag,
                    book_type=book_type,
                    now=now,
                ),
                DatabasePriority.NORMAL,
            )
        except Exception:
            if created_target:
                storage_path.unlink(missing_ok=True)
            raise
        rejected = self._apply_tags(book_id, list(metadata.tags)) if metadata.tags else 0
        return self._record_item(
            batch_id,
            source_display,
            external_id,
            status="imported",
            book_id=book_id,
            file_id=file_id,
            message=artifact.message,
            tags_rejected=rejected,
        )

    def _validate_source_candidate(self, source_path: Path) -> _ValidationFailure | None:
        """Check only the source invariants that precede a streaming copy."""

        logger.debug("Checking import source path=%s", source_path)
        if not source_path.exists():
            return _ValidationFailure("failed", f"Source file does not exist: {source_path}")
        if not source_path.is_file():
            return _ValidationFailure("failed", f"Source path is not a file: {source_path}")
        suffix = source_path.suffix.lower()
        if suffix not in BOOK_EXTENSIONS:
            return _ValidationFailure("failed", f"Unsupported book format: {suffix or source_path.name}")
        return None

    def _archive_limits_for(
        self,
        nested_archive_max_depth: int | None,
        archive_global_file_max_depth: int | None,
    ) -> ArchiveOpenLimits:
        limits = self._archive_limits
        if nested_archive_max_depth is not None:
            limits = replace(
                limits,
                nested_archive_max_depth=_core_depth_limit(nested_archive_max_depth),
            )
        if archive_global_file_max_depth is not None:
            limits = replace(
                limits,
                global_file_max_depth=_core_depth_limit(archive_global_file_max_depth),
            )
        return limits

    def _check_source_size(self, source_path: Path, limits: ArchiveOpenLimits) -> _ValidationFailure | None:
        if source_path.suffix.lower() not in ARCHIVE_EXTENSIONS or limits.max_source_bytes is None:
            return None
        try:
            source_size = source_path.stat().st_size
        except OSError:
            return _ValidationFailure("failed", f"Could not inspect source archive: {source_path}")
        if source_size > limits.max_source_bytes:
            return _ValidationFailure("failed", "Archive exceeds the configured maximum archive size.")
        return None

    def _validate_staged_file(
        self,
        staging_path: Path,
        limits: ArchiveOpenLimits,
    ) -> tuple[_ValidationFailure | None, ArchiveImportInspection | None]:
        """Decide whether the library may keep the staged file.

        Archives go through ``inspect_for_import`` rather than ``probe_archive``:
        the probe only ever saw the top level, so an encrypted or unreadable
        *nested* archive used to be accepted and then surfaced as a broken book
        when the reader reached it. The walk also returns the metadata sidecars,
        which is why the inspection is returned rather than reduced to a verdict
        -- re-opening the archive to fetch them would mean materializing every
        nested container a second time.
        """

        suffix = staging_path.suffix.lower()
        if suffix in ARCHIVE_EXTENSIONS:
            inspection = self._archive_service.inspect_for_import(staging_path, limits=limits)
            if inspection.accepted:
                return None, inspection
            return _import_rejection_failure(inspection, staging_path), None
        if suffix in PDF_EXTENSIONS:
            if self._pdf_service is None:
                return (
                    _ValidationFailure("failed", "PDF support is unavailable in this runtime."),
                    None,
                )
            probe = self._pdf_service.probe_pdf(staging_path)
            if not probe.is_valid:
                return _ValidationFailure("failed", probe.message), None
        return None, None

    def _stage_artifact(
        self,
        staging_path: Path,
        staged_hash: str,
        *,
        inspection: ArchiveImportInspection | None,
        limits: ArchiveOpenLimits,
        is_cancelled: Callable[[], bool] | None,
        report: _ItemProgress | None,
    ) -> _StagedArtifact:
        """Produce the file that will actually be stored, converting if asked.

        The original is never kept alongside a conversion: the user still has
        their own copy where they put it, and storing both would make one book
        two rows with two different identities.
        """

        suffix = staging_path.suffix.lower()
        convert = inspection is not None and should_convert(
            self._canonical_import_policy,
            suffix=suffix,
            has_nested_archives=inspection.nested_archive_count > 0,
        )
        if not convert:
            # The copy already hashed every byte on its way through; re-reading
            # the file to learn what it just told us would double import I/O.
            return _StagedArtifact(
                staging_path, staged_hash, STORAGE_KIND_VERBATIM, "Imported."
            )

        canonical_path = staging_path.with_name(
            f"{staging_path.stem}.canonical{self._canonical_writer.suffix}"
        )
        if report is not None and inspection is not None:
            # Announce conversion before it starts, not on the first written
            # page. Bulk extraction pulls the whole container out before the
            # writer gets page one, so on a large solid archive the dialog
            # otherwise sits on "Checking contents..." through nearly all of the
            # work and flips to "Converting" once it is essentially done.
            report.converting(0, inspection.image_count)
        try:
            self._archive_service.convert_to_canonical(
                staging_path,
                canonical_path,
                limits=limits,
                writer=self._canonical_writer,
                # From the inspection that already ran at the gate. Letting the
                # converter find these itself means a second full walk of every
                # nested container, which is the expensive part of an import.
                sidecars=select_sidecars(inspection.metadata_entries),
                is_cancelled=is_cancelled,
                on_page=(report.converting if report is not None else None),
                on_extract=(
                    (lambda: report.stage(ImportStage.EXTRACTING))
                    if report is not None
                    else None
                ),
            )
        except CanonicalWriteCancelled:
            canonical_path.unlink(missing_ok=True)
            raise
        except ArchiveCancelled as exc:
            # ``ArchiveCancelled`` subclasses ``ArchiveError``, so without this
            # it lands in the recovery branch below: a user who cancelled would
            # get the book imported verbatim and reported as a success. It is
            # the same event as ``CanonicalWriteCancelled`` -- the backend
            # noticed first -- so it is reported as that one.
            canonical_path.unlink(missing_ok=True)
            raise CanonicalWriteCancelled() from exc
        except (ArchiveError, OSError) as exc:
            # The archive already passed the import gate, so its bytes are
            # keepable as they are. Losing the conversion costs the user some
            # read speed; losing the import would cost them the book.
            logger.warning(
                "Canonical conversion failed; storing the source as-is",
                extra={
                    "event": "import.canonical.failed",
                    "category": "import",
                    "status": "recovered",
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                },
            )
            canonical_path.unlink(missing_ok=True)
            return _StagedArtifact(
                staging_path,
                staged_hash,
                STORAGE_KIND_VERBATIM,
                "Imported without conversion.",
            )

        try:
            stored_hash = self._hash_service.compute(canonical_path, self._hash_algorithm)
        except Exception:
            # Hash the artifact before dropping the source, and clean up the
            # artifact if that fails. Otherwise a failed hash strands a full
            # copy of the book in staging with nothing referencing it, and the
            # source it was built from is already gone.
            canonical_path.unlink(missing_ok=True)
            raise
        staging_path.unlink(missing_ok=True)
        return _StagedArtifact(
            canonical_path, stored_hash, STORAGE_KIND_CANONICAL, "Imported and converted."
        )

    def _staging_path(self, source_path: Path) -> Path:
        suffix = source_path.suffix.lower()
        return self._staging_dir() / f"{uuid4().hex}{suffix}"

    def _staging_dir(self) -> Path:
        return self._paths.paths.books / ".staging"

    def _reclaim_abandoned_staging(self) -> None:
        """Delete staged copies left behind by a previous run.

        Every in-process failure already unlinks its own staging file, so
        anything found here outlived the process that made it -- a crash, a
        kill, or a power loss part-way through a copy. Each one is a full copy
        of a book, so leaving them accumulates real disk.

        Safe to do unconditionally at batch start because the maintenance lease
        serializes imports: no other import can hold a staging file at this
        moment, so nothing here is still in use.
        """

        staging_dir = self._staging_dir()
        if not staging_dir.is_dir():
            return
        for path in staging_dir.iterdir():
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError as exc:  # pragma: no cover - best-effort reclamation.
                logger.warning("Could not reclaim abandoned staging file %s: %s", path.name, exc)
                continue
            logger.info("Reclaimed abandoned import staging file (%d bytes)", size)

    def _publish_staging(
        self,
        staging_path: Path,
        file_id: str,
        stored_hash: str,
        storage_kind: str,
    ) -> tuple[Path, bool]:
        """Move the staged artifact to the one path its row owns.

        ``storage_target`` decides that path, and library maintenance asks the
        same function -- they have to agree or maintenance will "repair" a
        healthy book by relocating it somewhere import would never look.
        """

        target = storage_target(
            self._paths.paths.books,
            storage_kind=storage_kind,
            stored_hash=stored_hash,
            file_id=file_id,
            suffix=staging_path.suffix.lower(),
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing_hash = self._hash_service.compute(target, self._hash_algorithm)
            if existing_hash != stored_hash:
                raise RuntimeError("Managed library target conflicts with a different file.")
            staging_path.unlink(missing_ok=True)
            return target, False
        staging_path.replace(target)
        return target, True

    def _duplicate_result(
        self,
        batch_id: str,
        source_display: str,
        external_id: str | None,
        duplicate: sqlite3.Row,
    ) -> ImportItemResult:
        logger.info("Import duplicate detected book_id=%s", duplicate["book_id"])
        return self._record_item(
            batch_id,
            source_display,
            external_id,
            status="duplicate",
            book_id=duplicate["book_id"],
            file_id=duplicate["file_id"],
            message="Book already exists in JoyRead.",
        )

    def _find_duplicate(self, source_hash: str) -> sqlite3.Row | None:
        """Match on what the user handed over, never on what we stored.

        These are the same string today. Once canonical import repackages an
        archive they diverge, and matching on the stored artifact would let the
        same source import twice -- a rebuilt CBZ does not hash like the CBR it
        came from.
        """

        return self._database.execute(
            lambda connection: connection.execute(
                """
                SELECT
                    book_files.file_id,
                    books.book_id,
                    private_books.private_book_id
                FROM book_files
                LEFT JOIN books ON books.file_id = book_files.file_id
                LEFT JOIN private_books ON private_books.file_id = book_files.file_id
                WHERE book_files.hash_algorithm = ? AND book_files.source_hash = ?
                """,
                (self._hash_algorithm, source_hash),
            ).fetchone(),
            DatabasePriority.HIGH,
        )

    def _record_item(
        self,
        batch_id: str,
        source_path: str,
        external_id: str | None,
        *,
        status: str,
        book_id: str | None = None,
        file_id: str | None = None,
        message: str | None = None,
        tags_rejected: int = 0,
    ) -> ImportItemResult:
        now = _now()
        self._database.execute(
            lambda connection: connection.execute(
                """
                INSERT INTO import_items(
                    import_item_id, batch_id, source_path, external_id, status,
                    book_id, file_id, message, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), batch_id, source_path, external_id, status, book_id, file_id, message, now, now),
            ),
            DatabasePriority.NORMAL,
        )
        return ImportItemResult(
            source_path=source_path,
            status=status,
            book_id=book_id,
            file_id=file_id,
            message=message,
            tags_rejected=tags_rejected,
        )


#: Rejections a user can act on by supplying a different file, versus ones they
#: can act on by changing a setting. Both fail the import; only the wording and
#: the reported status differ.
_ENCRYPTED_REJECTIONS = frozenset(
    {ImportRejection.ENCRYPTED_ROOT, ImportRejection.ENCRYPTED_NESTED}
)


def _import_rejection_failure(
    inspection: ArchiveImportInspection,
    staging_path: Path,
) -> _ValidationFailure:
    """Turn a refused inspection into the item status the batch reports.

    Encryption stays ``skipped`` rather than ``failed``: it is the one rejection
    that is not a defect in the file, and a batch that reports it as a failure
    tells the user something is broken when nothing is.
    """

    if inspection.rejection in _ENCRYPTED_REJECTIONS:
        return _ValidationFailure(
            "skipped",
            f"Skipped encrypted archive: {staging_path.name}",
            archive_validation_code=ArchiveValidationCode.PASSWORD_REQUIRED,
        )
    return _ValidationFailure("failed", inspection.message)


def _insert_imported_book(
    connection: sqlite3.Connection,
    *,
    file_id: str,
    book_id: str,
    original_path: str,
    original_file_name: str,
    storage_path: str,
    file_format: str,
    hash_algorithm: str,
    source_hash: str,
    stored_hash: str,
    storage_kind: str,
    title: str,
    book_type: str,
    now: str,
    author: str | None = None,
    language_tag: str = "und",
) -> None:
    connection.execute("BEGIN")
    try:
        connection.execute(
            """
            INSERT INTO book_files(
                file_id, original_path, original_file_name, storage_path, file_format,
                hash_algorithm, source_hash, stored_hash, storage_kind,
                state, integrity_error_code, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'healthy', NULL, ?, ?)
            """,
            (
                file_id,
                original_path,
                original_file_name,
                storage_path,
                file_format,
                hash_algorithm,
                source_hash,
                stored_hash,
                storage_kind,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO books(
                book_id, file_id, title, author, language_tag, book_type,
                cover_path, is_favourite, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, NULL, 0, ?, ?)
            """,
            (
                book_id,
                file_id,
                title,
                # ``Unknown`` and ``und`` are the schema's own defaults for
                # "nobody told us", so an absent sidecar field lands exactly
                # where an import with no sidecar at all would.
                author or "Unknown",
                language_tag,
                book_type,
                now,
                now,
            ),
        )
    except Exception:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def _resolve_source_path(source_path: str, manifest_dir: Path | None) -> Path:
    path = Path(source_path).expanduser()
    if path.is_absolute():
        return path
    if manifest_dir is not None and (manifest_dir / path).exists():
        return (manifest_dir / path).resolve()
    return (Path.cwd() / path).resolve()


def _supported_files_within_depth(folder: Path, *, max_depth: int) -> list[Path]:
    if not folder.exists() or not folder.is_dir():
        return []
    limit = max(1, int(max_depth))
    files: list[Path] = []
    for candidate in folder.rglob("*"):
        if not candidate.is_file():
            continue
        try:
            relative = candidate.relative_to(folder)
        except ValueError:
            continue
        depth = len(relative.parts)
        if depth <= limit and candidate.suffix.lower() in BOOK_EXTENSIONS:
            files.append(candidate)
    return sorted(files, key=lambda path: str(path).casefold())


def _book_type_for_suffix(suffix: str) -> str:
    if suffix == ".epub":
        return "book"
    return "manga"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _core_depth_limit(value: object) -> int | None:
    depth = int(value)
    return None if depth == -1 else depth
