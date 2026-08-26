"""Export app-managed public book files to a user-selected folder."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
import shutil
import re

from joyread.core.models.export import BookExportRecord
from joyread.core.repositories.book_repository import BookRepository
from joyread.core.services.hash_service import HashService


logger = logging.getLogger(__name__)


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
        logger.info("Exporting %d book(s) to %s", len(target_ids), destination)
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

        # The book's title, not the name the file arrived under. Import may have
        # repackaged the archive -- exporting a converted book as ``Vol01.cb7``
        # would name a CBZ after a format it is not. The suffix comes from the
        # artifact we actually hold, for the same reason.
        safe_name = _safe_export_file_name(
            f"{record.title}{source_path.suffix.lower()}",
            fallback=f"{record.book_uuid}{source_path.suffix.lower()}",
        )
        destination_path = _unique_destination_path(destination_dir, safe_name, reserved_names)
        reserved_names.add(destination_path.name.casefold())
        try:
            shutil.copy2(source_path, destination_path)
        except OSError as exc:
            logger.warning(
                "Export copy failed for book=%s to %s: %s",
                record.book_uuid,
                destination_path,
                exc,
            )
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
        if not record.stored_hash:
            return "Stored content hash is missing."

        actual_hash = self._hash_service.compute(source_path, record.hash_algorithm)
        if actual_hash != record.stored_hash:
            return "Stored file hash does not match JoyRead metadata."
        return None


_WINDOWS_UNSAFE_NAME_RE = re.compile(r'[<>:"|?*\x00-\x1f]')

#: Names Windows reserves for devices. Opening ``CON.cbz`` there talks to the
#: console rather than a file, so an export named after one silently writes
#: nothing -- and the name can come straight from a book title, which the user
#: edits and an archive's own metadata sidecar can supply.
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{digit}" for digit in "123456789"}
    | {f"LPT{digit}" for digit in "123456789"}
)

#: Bytes, not characters: filesystems bound the encoded name, and one CJK title
#: is three bytes a character. Well under the common 255 limit so the
#: de-duplicating suffix has room.
_MAX_EXPORT_NAME_BYTES = 200


def _safe_export_file_name(value: object, *, fallback: str) -> str:
    """A name that is safe to create in a directory the user chose.

    The input is a book title: user-editable, and also supplied by an archive's
    own metadata, so it is untrusted in the ordinary sense.
    """

    basename = str(value or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
    basename = _WINDOWS_UNSAFE_NAME_RE.sub("_", basename)
    if basename in {"", ".", ".."}:
        basename = fallback
    basename = basename.replace("/", "_").replace("\\", "_")
    basename = _bound_name_length(basename)
    stem, dot, suffix = basename.rpartition(".")
    if not dot:
        stem, suffix = basename, ""
    # Windows resolves a device name whether or not it carries an extension, so
    # the stem is what has to be checked.
    if stem.upper() in _WINDOWS_DEVICE_NAMES:
        stem = f"{stem}_"
    # Trailing dots and spaces are silently dropped by Windows when creating a
    # file, which turns "A Title ." into a name that does not match what we
    # asked for -- and can collide with a name we already reserved.
    basename = f"{stem}.{suffix}" if dot else stem
    basename = basename.rstrip(" .")
    return basename or "book"


def _bound_name_length(basename: str) -> str:
    """Trim the stem, never the suffix, to fit a filesystem's byte limit."""

    if len(basename.encode("utf-8")) <= _MAX_EXPORT_NAME_BYTES:
        return basename
    stem, dot, suffix = basename.rpartition(".")
    if not dot:
        stem, suffix = basename, ""
    tail = f".{suffix}" if dot else ""
    budget = max(1, _MAX_EXPORT_NAME_BYTES - len(tail.encode("utf-8")))
    encoded = stem.encode("utf-8")[:budget]
    # A multi-byte character can straddle the cut; drop the partial one.
    return encoded.decode("utf-8", errors="ignore") + tail


def _unique_destination_path(destination_dir: Path, file_name: str, reserved_names: set[str]) -> Path:
    candidate = destination_dir / file_name
    stem = candidate.stem
    suffix = candidate.suffix
    index = 1
    while candidate.exists() or candidate.name.casefold() in reserved_names:
        candidate = destination_dir / f"{stem} ({index}){suffix}"
        index += 1
    return candidate
