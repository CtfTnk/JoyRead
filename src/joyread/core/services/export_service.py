"""Export app-managed public book files to a user-selected folder."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import re

from joyread.core.models.export import BookExportRecord
from joyread.core.repositories.book_repository import BookRepository
from joyread.core.services.hash_service import HashService


@dataclass(frozen=True)
class ExportItemResult:
    book_uuid: str
    status: str
    original_file_name: str | None = None
    destination_path: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ExportBatchResult:
    exported_count: int
    skipped_count: int
    failed_count: int
    items: tuple[ExportItemResult, ...]


class ExportService:
    def __init__(self, repository: BookRepository, hash_service: HashService) -> None:
        self._repository = repository
        self._hash_service = hash_service

    def export_books(self, book_ids: tuple[str, ...], destination_dir: str | Path) -> ExportBatchResult:
        target_ids = tuple(dict.fromkeys(book_ids))
        destination = Path(destination_dir).expanduser()
        if not destination.exists():
            raise ValueError(f"Export folder does not exist: {destination}")
        if not destination.is_dir():
            raise ValueError(f"Export destination is not a folder: {destination}")

        records = self._repository.get_export_records(target_ids)
        records_by_id = {record.book_uuid: record for record in records}
        reserved_names: set[str] = set()
        results: list[ExportItemResult] = []

        for book_id in target_ids:
            record = records_by_id.get(book_id)
            if record is None:
                results.append(
                    ExportItemResult(
                        book_uuid=book_id,
                        status="failed",
                        message="Book is not available for public export.",
                    )
                )
                continue
            results.append(self._export_one(record, destination, reserved_names))

        return ExportBatchResult(
            exported_count=sum(item.status == "exported" for item in results),
            skipped_count=sum(item.status == "skipped" for item in results),
            failed_count=sum(item.status == "failed" for item in results),
            items=tuple(results),
        )

    def _export_one(
        self,
        record: BookExportRecord,
        destination_dir: Path,
        reserved_names: set[str],
    ) -> ExportItemResult:
        source_path = Path(record.storage_path).expanduser()
        failure = self._validate_source(record, source_path)
        if failure is not None:
            return ExportItemResult(
                book_uuid=record.book_uuid,
                original_file_name=record.original_file_name,
                status="failed",
                message=failure,
            )

        safe_name = _safe_export_file_name(
            record.original_file_name,
            fallback=f"{record.book_uuid}{source_path.suffix}",
        )
        destination_path = _unique_destination_path(destination_dir, safe_name, reserved_names)
        reserved_names.add(destination_path.name.casefold())
        try:
            shutil.copy2(source_path, destination_path)
        except OSError as exc:
            return ExportItemResult(
                book_uuid=record.book_uuid,
                original_file_name=record.original_file_name,
                status="failed",
                message=str(exc),
            )

        return ExportItemResult(
            book_uuid=record.book_uuid,
            original_file_name=record.original_file_name,
            destination_path=str(destination_path),
            status="exported",
            message="Exported.",
        )

    def _validate_source(self, record: BookExportRecord, source_path: Path) -> str | None:
        if record.is_missing:
            return "Stored file is marked missing."
        if not source_path.exists():
            return f"Stored file does not exist: {source_path}"
        if not source_path.is_file():
            return f"Stored path is not a file: {source_path}"
        if not record.content_hash:
            return "Stored content hash is missing."

        actual_hash = self._hash_service.compute(source_path, record.hash_algorithm)
        if actual_hash != record.content_hash:
            return "Stored file hash does not match JoyRead metadata."
        return None


_WINDOWS_UNSAFE_NAME_RE = re.compile(r'[<>:"|?*\x00-\x1f]')


def _safe_export_file_name(value: object, *, fallback: str) -> str:
    basename = str(value or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
    basename = _WINDOWS_UNSAFE_NAME_RE.sub("_", basename)
    if basename in {"", ".", ".."}:
        basename = fallback
    basename = basename.replace("/", "_").replace("\\", "_")
    return basename or "book"


def _unique_destination_path(destination_dir: Path, file_name: str, reserved_names: set[str]) -> Path:
    candidate = destination_dir / file_name
    stem = candidate.stem
    suffix = candidate.suffix
    index = 1
    while candidate.exists() or candidate.name.casefold() in reserved_names:
        candidate = destination_dir / f"{stem} ({index}){suffix}"
        index += 1
    return candidate
