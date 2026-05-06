"""Manifest-based JoyRead book import service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

from joyread.core.archive import ArchiveImageService, ArchiveValidationCode
from joyread.core.archive.service import ARCHIVE_EXTENSIONS
from joyread.core.services.hash_service import HashService
from joyread.infrastructure.database.database_interpreter import DatabaseInterpreter, DatabasePriority
from joyread.infrastructure.filesystem.path_service import PathService


BOOK_EXTENSIONS = frozenset({".epub", ".pdf"}) | ARCHIVE_EXTENSIONS


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
    failed_count: int
    items: tuple[ImportItemResult, ...]


class ImportService:
    def __init__(
        self,
        paths: PathService,
        database: DatabaseInterpreter,
        archive_service: ArchiveImageService,
        hash_service: HashService,
        hash_algorithm: str = "sha256",
    ) -> None:
        self._paths = paths
        self._database = database
        self._archive_service = archive_service
        self._hash_service = hash_service
        self._hash_algorithm = hash_algorithm

    def import_manifest(self, manifest_path: str | Path) -> ImportBatchResult:
        path = Path(manifest_path).expanduser()
        raw = json.loads(path.read_text(encoding="utf-8"))
        if int(raw.get("version", 0)) != 1:
            raise ValueError("Unsupported import manifest version.")
        items = raw.get("items")
        if not isinstance(items, list):
            raise ValueError("Import manifest must contain an items list.")
        return self.import_items(items, manifest_path=path)

    def import_files(self, paths: list[str | Path]) -> ImportBatchResult:
        return self.import_items([{"source_path": str(path)} for path in paths], manifest_path=None)

    def import_folder(self, path: str | Path) -> ImportBatchResult:
        folder = Path(path).expanduser()
        files = [candidate for candidate in folder.rglob("*") if candidate.is_file()]
        return self.import_files(files)

    def import_items(
        self,
        items: list[dict[str, object]],
        *,
        manifest_path: Path | None,
    ) -> ImportBatchResult:
        batch_id = str(uuid4())
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
        for item in items:
            source_value = str(item.get("source_path") or "")
            external_id = item.get("external_id")
            try:
                result = self._import_one(
                    batch_id=batch_id,
                    source_path=_resolve_source_path(source_value, manifest_dir),
                    source_display=source_value,
                    external_id=str(external_id) if external_id is not None else None,
                )
            except Exception as exc:
                result = self._record_item(
                    batch_id,
                    source_value,
                    str(external_id) if external_id is not None else None,
                    status="failed",
                    message=str(exc),
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
        return ImportBatchResult(
            batch_id=batch_id,
            imported_count=sum(item.status == "imported" for item in results),
            duplicate_count=sum(item.status == "duplicate" for item in results),
            failed_count=sum(item.status == "failed" for item in results),
            items=tuple(results),
        )

    def _import_one(
        self,
        *,
        batch_id: str,
        source_path: Path,
        source_display: str,
        external_id: str | None,
    ) -> ImportItemResult:
        failure = self._validate_source(source_path)
        if failure is not None:
            return self._record_item(
                batch_id,
                source_display,
                external_id,
                status="failed",
                message=failure,
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

    def _validate_source(self, source_path: Path) -> str | None:
        if not source_path.exists():
            return f"Source file does not exist: {source_path}"
        if not source_path.is_file():
            return f"Source path is not a file: {source_path}"
        suffix = source_path.suffix.lower()
        if suffix not in BOOK_EXTENSIONS:
            return f"Unsupported book format: {suffix or source_path.name}"
        if suffix in ARCHIVE_EXTENSIONS:
            validation = self._archive_service.validate_archive(source_path)
            if validation.code != ArchiveValidationCode.OK:
                return validation.message
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
                file_id, original_path, storage_path, file_format, file_size,
                mtime_ns, hash_algorithm, content_hash, state, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'healthy', ?, ?)
            """,
            (
                file_id,
                original_path,
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
            VALUES (?, ?, ?, 'Unknown', 'Unknown', ?, NULL, 0, ?, ?)
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


def _book_type_for_suffix(suffix: str) -> str:
    if suffix == ".epub":
        return "book"
    return "manga"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
