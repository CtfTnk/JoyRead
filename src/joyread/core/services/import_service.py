"""Manifest-based JoyRead book import service."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

from joyread.core.archive import ArchiveImageService, ArchivePasswordPolicy, ArchiveValidationCode
from joyread.core.archive.service import ARCHIVE_EXTENSIONS
from joyread.core.reader.pdf_session import PDF_EXTENSIONS, PdfImageService
from joyread.core.services.hash_service import HashService
from joyread.infrastructure.database.database_interpreter import DatabaseInterpreter, DatabasePriority
from joyread.infrastructure.filesystem.path_service import PathService


BOOK_EXTENSIONS = frozenset({".epub", ".pdf"}) | ARCHIVE_EXTENSIONS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImportItemResult:
    source_path: str
    status: str
    book_id: str | None = None
    file_id: str | None = None
    message: str | None = None


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


@dataclass(frozen=True)
class _ValidationFailure:
    status: str
    message: str


class ImportService:
    def __init__(
        self,
        paths: PathService,
        database: DatabaseInterpreter,
        archive_service: ArchiveImageService,
        hash_service: HashService,
        hash_algorithm: str = "sha256",
        pdf_service: PdfImageService | None = None,
    ) -> None:
        self._paths = paths
        self._database = database
        self._archive_service = archive_service
        self._pdf_service = pdf_service or PdfImageService()
        self._hash_service = hash_service
        self._hash_algorithm = hash_algorithm

    def import_manifest(
        self,
        manifest_path: str | Path,
        *,
        archive_internal_max_depth: int | None = None,
    ) -> ImportBatchResult:
        path = Path(manifest_path).expanduser()
        raw = json.loads(path.read_text(encoding="utf-8"))
        if int(raw.get("version", 0)) != 1:
            raise ValueError("Unsupported import manifest version.")
        items = raw.get("items")
        if not isinstance(items, list):
            raise ValueError("Import manifest must contain an items list.")
        return self.import_items(
            items,
            manifest_path=path,
            archive_internal_max_depth=archive_internal_max_depth,
        )

    def import_files(
        self,
        paths: list[str | Path],
        *,
        archive_internal_max_depth: int | None = None,
    ) -> ImportBatchResult:
        return self.import_items(
            [{"source_path": str(path)} for path in paths],
            manifest_path=None,
            archive_internal_max_depth=archive_internal_max_depth,
        )

    def preflight_file(
        self,
        path: str | Path,
        *,
        archive_internal_max_depth: int | None = None,
    ) -> ImportPreflightResult:
        source_path = Path(path).expanduser()
        failure = self._validate_source(source_path, archive_internal_max_depth)
        if failure is not None:
            return ImportPreflightResult(
                source_path=str(source_path),
                can_import=False,
                status=failure.status,
                message=failure.message,
            )
        return ImportPreflightResult(str(source_path), True, "importable")

    def import_folder(
        self,
        path: str | Path,
        *,
        max_depth: int = 1,
        archive_internal_max_depth: int | None = None,
    ) -> ImportBatchResult:
        folder = Path(path).expanduser()
        files = _supported_files_within_depth(folder, max_depth=max_depth)
        return self.import_files(files, archive_internal_max_depth=archive_internal_max_depth)

    def import_items(
        self,
        items: list[dict[str, object]],
        *,
        manifest_path: Path | None,
        archive_internal_max_depth: int | None = None,
    ) -> ImportBatchResult:
        batch_id = str(uuid4())
        started_at = _now()
        manifest_display = str(manifest_path) if manifest_path is not None else None
        logger.info(
            "Import batch %s starting: %d item(s) manifest=%s max_depth=%s",
            batch_id,
            len(items),
            manifest_display,
            archive_internal_max_depth,
        )
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
        for item in items:
            source_value = str(item.get("source_path") or "")
            external_id = item.get("external_id")
            try:
                result = self._import_one(
                    batch_id=batch_id,
                    source_path=_resolve_source_path(source_value, manifest_dir),
                    source_display=source_value,
                    external_id=str(external_id) if external_id is not None else None,
                    archive_internal_max_depth=archive_internal_max_depth,
                )
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
        logger.info(
            "Import batch %s finished: imported=%d duplicate=%d skipped=%d failed=%d",
            batch_id,
            batch_result.imported_count,
            batch_result.duplicate_count,
            batch_result.skipped_count,
            batch_result.failed_count,
        )
        return batch_result

    def _import_one(
        self,
        *,
        batch_id: str,
        source_path: Path,
        source_display: str,
        external_id: str | None,
        archive_internal_max_depth: int | None,
    ) -> ImportItemResult:
        failure = self._validate_source(source_path, archive_internal_max_depth)
        if failure is not None:
            return self._record_item(
                batch_id,
                source_display,
                external_id,
                status=failure.status,
                message=failure.message,
            )

        content_hash = self._hash_service.compute(source_path, self._hash_algorithm)
        duplicate = self._find_duplicate(content_hash)
        if duplicate is not None:
            return self._record_item(
                batch_id,
                source_display,
                external_id,
                status="duplicate",
                book_id=duplicate["book_id"],
                file_id=duplicate["file_id"],
                message="Book already exists in JoyRead.",
            )

        storage_path = self._copy_to_books(source_path, content_hash)
        copied_hash = self._hash_service.compute(storage_path, self._hash_algorithm)
        if copied_hash != content_hash:
            storage_path.unlink(missing_ok=True)
            return self._record_item(
                batch_id,
                source_display,
                external_id,
                status="failed",
                message="Copied file hash did not match source hash.",
            )

        stat = storage_path.stat()
        file_id = str(uuid4())
        book_id = str(uuid4())
        now = _now()
        file_format = source_path.suffix.lstrip(".").upper()
        book_type = _book_type_for_suffix(source_path.suffix.lower())
        self._database.execute(
            lambda connection: _insert_imported_book(
                connection,
                file_id=file_id,
                book_id=book_id,
                original_path=str(source_path),
                original_file_name=source_path.name,
                storage_path=str(storage_path),
                file_format=file_format,
                file_size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                hash_algorithm=self._hash_algorithm,
                content_hash=content_hash,
                title=source_path.stem,
                book_type=book_type,
                now=now,
            ),
            DatabasePriority.NORMAL,
        )
        return self._record_item(
            batch_id,
            source_display,
            external_id,
            status="imported",
            book_id=book_id,
            file_id=file_id,
            message="Imported.",
        )

    def _validate_source(self, source_path: Path, archive_internal_max_depth: int | None) -> _ValidationFailure | None:
        if not source_path.exists():
            return _ValidationFailure("failed", f"Source file does not exist: {source_path}")
        if not source_path.is_file():
            return _ValidationFailure("failed", f"Source path is not a file: {source_path}")
        suffix = source_path.suffix.lower()
        if suffix not in BOOK_EXTENSIONS:
            return _ValidationFailure("failed", f"Unsupported book format: {suffix or source_path.name}")
        if suffix in ARCHIVE_EXTENSIONS:
            validation = self._archive_service.validate_archive(
                source_path,
                password_policy=ArchivePasswordPolicy.FORBID,
                max_depth=archive_internal_max_depth if archive_internal_max_depth is not None else 2,
            )
            if validation.code != ArchiveValidationCode.OK:
                if validation.code in {ArchiveValidationCode.PASSWORD_REQUIRED, ArchiveValidationCode.PASSWORD_REJECTED}:
                    return _ValidationFailure("skipped", validation.message)
                return _ValidationFailure("failed", validation.message)
        if suffix in PDF_EXTENSIONS:
            validation = self._pdf_service.validate_pdf(source_path)
            if not validation.is_valid:
                return _ValidationFailure("failed", validation.message)
        return None

    def _copy_to_books(self, source_path: Path, content_hash: str) -> Path:
        suffix = source_path.suffix.lower()
        target_dir = self._paths.paths.books / content_hash[:2]
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{content_hash}{suffix}"
        if target.exists():
            return target
        temp = target.with_suffix(f"{target.suffix}.tmp-{uuid4().hex}")
        shutil.copy2(source_path, temp)
        temp.replace(target)
        return target

    def _find_duplicate(self, content_hash: str) -> sqlite3.Row | None:
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
                WHERE book_files.hash_algorithm = ? AND book_files.content_hash = ?
                """,
                (self._hash_algorithm, content_hash),
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
        )


def _insert_imported_book(
    connection: sqlite3.Connection,
    *,
    file_id: str,
    book_id: str,
    original_path: str,
    original_file_name: str,
    storage_path: str,
    file_format: str,
    file_size: int,
    mtime_ns: int,
    hash_algorithm: str,
    content_hash: str,
    title: str,
    book_type: str,
    now: str,
) -> None:
    connection.execute("BEGIN")
    try:
        connection.execute(
            """
            INSERT INTO book_files(
                file_id, original_path, original_file_name, storage_path, file_format, file_size,
                mtime_ns, hash_algorithm, content_hash, state, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'healthy', ?, ?)
            """,
            (
                file_id,
                original_path,
                original_file_name,
                storage_path,
                file_format,
                file_size,
                mtime_ns,
                hash_algorithm,
                content_hash,
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
            VALUES (?, ?, ?, 'Unknown', 'und', ?, NULL, 0, ?, ?)
            """,
            (book_id, file_id, title, book_type, now, now),
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
