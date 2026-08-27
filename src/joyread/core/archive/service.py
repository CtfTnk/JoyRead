"""Archive-backed image page discovery and access.

This module intentionally exposes archive data as a UI-free service. Thumbnail
generation, reader rendering, and import workflows should consume this API
instead of parsing archive formats directly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from shutil import rmtree
from dataclasses import replace
from tempfile import TemporaryDirectory, mkdtemp
from threading import RLock
from time import perf_counter
from typing import Callable, Sequence
from uuid import uuid4
from zipfile import BadZipFile

from joyread.core.file_types import ARCHIVE_EXTENSIONS
from joyread.core.diagnostics import cache_identity_kind
from joyread.core.operation_context import bind_operation, create_operation


logger = logging.getLogger(__name__)

from joyread.core.archive.errors import (
    ArchiveBulkUnsupported,
    ArchiveCorruptError,
    ArchiveDependencyMissing,
    ArchiveEmptyError,
    ArchiveError,
    ArchiveOpenError,
    ArchivePasswordRejected,
    ArchivePasswordRequired,
    ArchiveReadError,
    ArchiveResourceLimitError,
    ArchiveUnsupportedFormat,
)
from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget
from joyread.core.archive.models import (
    ArchivePasswordPolicy,
    ArchivePasswordRequest,
    ArchivePasswordResponse,
    ArchiveProbeResult,
    ArchiveValidationCode,
    PasswordProvider,
)
from joyread.core.archive.inspection import (
    ArchiveImportInspection,
    ArchiveImportInspector,
    ArchiveMetadataEntry,
    ImportRejection,
)
from joyread.core.archive.backends import ExtractionBackendResolver
from joyread.core.archive.batching import plan_read_batch
from joyread.core.archive.formats.common import read_file_bounded
from joyread.core.archive.formats import (
    ArchiveFormatBackend,
    RarArchiveBackend,
    SevenZipArchiveBackend,
    ZipArchiveBackend,
)
from joyread.core.archive.records import ArchiveListing as _ArchiveListing
from joyread.core.archive.records import ArchiveSource as _ArchiveSource
from joyread.core.archive.records import PageRecord
from joyread.core.archive.canonical import (
    CanonicalWriteCancelled,
    CanonicalWriteResult,
    CanonicalWriter,
    CbzWriter,
)
from joyread.core.archive.scanner import ArchiveScanContext as _ScanContext
from joyread.core.archive.scanner import (
    IMAGE_EXTENSIONS,
    SCANNER_SCHEMA_VERSION,
    ArchiveScanner,
    ArchiveSourceSkipped as _ArchiveSourceSkipped,
)
from joyread.core.archive.session import (
    ArchiveImageSession,
    BulkExtract,
    EXPENSIVE_ARCHIVE_EXTENSIONS,
)
from joyread.core.archive.tree import (
    disambiguate_nested_archive_labels as _disambiguate_nested_archive_labels,
    flatten_archive_tree as _flatten_archive_tree,
    flatten_archive_tree_for_writing as _flatten_for_writing,
    is_junk_entry as _is_junk_entry,
    safe_entry_name as _safe_entry_name,
)
from joyread.core.services.archive_extraction_pool import ArchiveExtractionCache, ArchiveExtractionPool
from joyread.core.services.archive_cache_lease import ArchiveCacheLease, ArchiveCacheScope

try:  # pragma: no cover - exercised through dependency-missing branches.
    import py7zr
except ImportError:  # pragma: no cover
    py7zr = None

try:  # pragma: no cover - exercised through dependency-missing branches.
    import pyzipper
except ImportError:  # pragma: no cover
    pyzipper = None

try:  # pragma: no cover - exercised through dependency-missing branches.
    import rarfile
except ImportError:  # pragma: no cover
    rarfile = None


_ZIP_EXTENSIONS = frozenset({".zip", ".cbz"})
_SEVEN_ZIP_EXTENSIONS = frozenset({".7z", ".cb7"})
_RAR_EXTENSIONS = frozenset({".rar", ".cbr"})
_ZIP_BAD_FILE_ERRORS = (BadZipFile,)
_DEFAULT_NESTED_ARCHIVE_DEPTH = 2
_DEFAULT_GLOBAL_FILE_DEPTH = 100
_MAX_NESTED_ARCHIVE_DEPTH = 5
_MAX_GLOBAL_FILE_DEPTH = 1000
# ``rarfile`` stores extractor executable paths in module globals. Every
# ArchiveImageService in this process must serialize configuration and reads.
_RARFILE_LOCK = RLock()
if pyzipper is not None:
    # pyzipper vendors its own ``zipfile`` fork to support AES, and that fork
    # raises a different ``BadZipFile`` class. Without catching both, a
    # corrupt password-protected ZIP would escape the "controlled archive
    # error" contract and surface as an unhandled exception in the reader.
    _ZIP_BAD_FILE_ERRORS = (BadZipFile, pyzipper.zipfile.BadZipFile)



class _StagedPageReader:
    """Extract each container once, then hand the writer its pages from disk.

    Reading a page straight out of the archive when the writer asks for it is
    the obvious implementation and is badly wrong for a *solid* archive: every
    read has to decompress the whole solid block again to reach one member, so
    an N-page book costs N full decompressions and, with the 7-Zip executable,
    N subprocesses. Measured on a 60-page solid 7z that was 60 archive opens.

    Extracting a container in one pass and streaming from the filesystem turns
    that back into one pass, and keeps memory at one page rather than one book:
    the pages live on disk in a temporary workspace, and each file is unlinked
    as soon as the last page that needs it has been written.
    """

    def __init__(
        self,
        workspace: Path,
        placed: Sequence[tuple[str, PageRecord]],
        *,
        bulk_extract_for: Callable[[_ArchiveSource], BulkExtract | None],
        read_entries: Callable[..., dict[str, bytes]],
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
        is_cancelled: Callable[[], bool] | None,
        on_extract: Callable[[], None] | None = None,
    ) -> None:
        self._workspace = workspace
        self._on_extract = on_extract
        self._bulk_extract_for = bulk_extract_for
        self._read_entries = read_entries
        self._limits = limits
        self._budget = budget
        self._is_cancelled = is_cancelled

        self._pages_by_source: dict[int, list[PageRecord]] = {}
        self._sources: dict[int, _ArchiveSource] = {}
        # A container may legally list one member twice, which the scan turns
        # into two pages sharing a staged file. Counting uses rather than pages
        # is what makes it safe to unlink on the last one.
        self._uses: dict[tuple[int, str], int] = {}
        seen: set[tuple[int, str]] = set()
        for _prefix, page in placed:
            key = id(page.source)
            self._sources.setdefault(key, page.source)
            member = (key, page.name)
            if member not in seen:
                seen.add(member)
                self._pages_by_source.setdefault(key, []).append(page)
            self._uses[member] = self._uses.get(member, 0) + 1
        self._staged: dict[int, Path] = {}
        # Which budget a container's read-back charges. Bulk extraction runs a
        # subprocess that writes straight to disk, so nothing is charged until
        # the pages are read back; a batched read already charged them on the
        # way in. Getting this wrong counts every byte twice.
        self._readback_budgets: dict[int, ArchiveOperationBudget] = {}

    def read(self, page: PageRecord) -> bytes:
        key = id(page.source)
        root = self._staged.get(key)
        if root is None:
            # Resolved once per container, not once per page: it never changes,
            # and resolving is a filesystem walk.
            root = self._stage(key).resolve()
            self._staged[key] = root
        staged = (root / page.name).resolve()
        if not staged.is_relative_to(root) or not staged.is_file():
            raise ArchiveReadError(f"Archive entry was not extracted: {page.name}")
        # Bounded, not ``read_bytes``. Bulk extraction hands back a directory
        # tree written by a subprocess, so the per-member limit was never
        # applied to it and an entry whose header under-declared its size would
        # otherwise be loaded whole. The *budget* is a different question: see
        # ``_readback_budgets``, because charging bytes a batched read already
        # paid for would count the whole book twice.
        payload = read_file_bounded(
            staged,
            page.name,
            max_item_bytes=self._limits.max_extracted_item_bytes,
            budget=self._readback_budgets.get(key, self._budget),
        )
        self._release(key, page.name, staged)
        return payload

    def _path_backed(
        self, source: _ArchiveSource, key: int
    ) -> tuple[_ArchiveSource, BulkExtract | None]:
        """Give an in-memory container a path, when that unlocks bulk extraction.

        A nested archive is carried as bytes, and bulk extraction needs a file,
        so every page of a nested 7z was going through the pure-Python backend
        instead. Measured on a 3-container nested 7z, that was 97% of the
        conversion -- 4.3s against 1.4s for the same 60 pages in a flat archive.

        The bytes are already resident, so writing them into the workspace costs
        one transient copy on disk and nothing in memory. Only done when the
        backend actually offers bulk extraction for this format, so a nested zip
        (already cheap to read entry by entry) is left alone.
        """

        if source.data is None:
            return source, None
        candidate = replace(
            source,
            path=self._workspace / "containers" / f"{key:x}{source.suffix}",
            data=None,
        )
        bulk_extract = self._bulk_extract_for(candidate)
        if bulk_extract is None:
            return source, None
        assert candidate.path is not None
        candidate.path.parent.mkdir(parents=True, exist_ok=True)
        candidate.path.write_bytes(source.data)
        # Remembered so a later fallback read uses the file too, not the bytes.
        self._sources[key] = candidate
        return candidate, bulk_extract

    def _release(self, key: int, name: str, staged: Path) -> None:
        remaining = self._uses.get((key, name), 0) - 1
        self._uses[(key, name)] = remaining
        if remaining <= 0:
            staged.unlink(missing_ok=True)

    def _stage(self, key: int) -> Path:
        source = self._sources[key]
        pages = self._pages_by_source[key]
        root = self._workspace / f"c{key:x}"
        root.mkdir(parents=True, exist_ok=True)
        password = pages[0].password
        if self._on_extract is not None:
            # Before the work, not after. Staging a container is the longest
            # uninterrupted step in a conversion and emits nothing of its own,
            # so without this the caller's progress sits frozen on the last page
            # it wrote -- once per container, which on a nested book reads as a
            # series of hangs.
            self._on_extract()
        # Asked per source rather than pinned to the top-level one: the scanner
        # rebuilds the source object as it walks, so identity does not survive,
        # and the capability is a property of the container anyway.
        bulk_extract = self._bulk_extract_for(source)
        if bulk_extract is None:
            source, bulk_extract = self._path_backed(source, key)
        if bulk_extract is not None:
            try:
                bulk_extract(
                    source,
                    tuple(page.name for page in pages),
                    root,
                    password,
                    limits=self._limits,
                    budget=self._budget,
                    # What is *left* of the allowance, not all of it. Staging is
                    # lazy and per container, so passing the full ceiling each
                    # time lets an N-container archive write N times the limit
                    # the user configured before any of it is charged.
                    max_output_bytes=self._remaining_allowance(),
                    is_cancelled=self._is_cancelled,
                )
                # The subprocess wrote straight to disk and charged nothing, so
                # the read-back is where these bytes meet the budget.
                self._readback_budgets[key] = self._budget
                return root
            except (ArchiveBulkUnsupported, ArchiveDependencyMissing) as exc:
                # A capability gap, not a broken archive: an unrepresentable
                # member name, or an executable that went missing after the
                # probe. Fall back rather than losing the import.
                logger.info(
                    "Bulk extraction unavailable for conversion; reading in batches",
                    extra={
                        "event": "archive.canonical.bulk_unavailable",
                        "category": "archive",
                        "status": "recovered",
                        "error_type": type(exc).__name__,
                    },
                )
        self._stage_in_batches(source, pages, root)
        # Already charged on the way in. The read-back still enforces the
        # per-item limit, but against a meter nothing depends on.
        self._readback_budgets[key] = ArchiveOperationBudget(None)
        return root

    def _stage_in_batches(
        self, source: _ArchiveSource, pages: Sequence[PageRecord], root: Path
    ) -> None:
        """Read several members per call, spilling each batch to disk.

        The batching rule is ``plan_read_batch``, the same one sequential reads
        use, rather than a second one written here. It matters that it is that
        one: it isolates entries whose listing declares no size, and a declared
        size is attacker-controlled, so grouping on an assumed size is how a
        handful of under-declared pages become a whole manga held in memory at
        once -- which the archive core must never do.
        """

        remaining = list(pages)
        while remaining:
            if self._is_cancelled is not None and self._is_cancelled():
                raise CanonicalWriteCancelled()
            chosen = plan_read_batch(
                ((index, page.size) for index, page in enumerate(remaining)),
                max_declared_bytes=_CONVERSION_BATCH_BYTES,
            )
            batch = [remaining[index] for index in chosen]
            self._flush_batch(source, batch, root)
            remaining = remaining[len(batch):]

    def _flush_batch(
        self, source: _ArchiveSource, batch: Sequence[PageRecord], root: Path
    ) -> None:
        """Read one planned batch, and survive the plan being a lie.

        ``plan_read_batch`` groups on *declared* sizes, and a declared size is
        the archive's own claim about itself. Eight entries that each say
        "2 KB" and each expand toward ``max_extracted_item_bytes`` are a legal
        archive and would arrive together, bounded only by the 4 GiB operation
        budget -- a whole manga in memory, which it must never do.

        So the batch is read under a budget scoped to the batch. An archive
        that told the truth is unaffected; one that under-declared trips that
        budget instead of the operation's and is re-read a page at a time,
        where a single entry is bounded by the per-item limit and spilled to
        disk immediately. Slow, but only for an archive that lied.
        """

        if len(batch) == 1:
            self._read_into(source, batch, root, self._budget)
            return

        scoped = ArchiveOperationBudget(self._batch_allowance())
        try:
            written = self._read_into(source, batch, root, scoped)
        except ArchiveResourceLimitError as exc:
            if exc.limit != "operation_bytes":
                raise
            logger.info(
                "Archive under-declared a batch; re-reading it one page at a time",
                extra={
                    "event": "archive.canonical.batch_overran",
                    "category": "archive",
                    "status": "recovered",
                    "count": len(batch),
                },
            )
            for page in batch:
                self._read_into(source, (page,), root, self._budget)
            return
        # Charged after the fact because the call ran against the scoped budget.
        # The operation budget still gets the true total, so a book that fits
        # its batches one at a time cannot exceed the overall allowance.
        self._budget.consume(written, source.display_name)

    def _batch_allowance(self) -> int:
        """The scoped budget for one batch, never more than the operation has."""

        remaining = self._remaining_allowance()
        if remaining is None:
            return _CONVERSION_BATCH_BYTES
        return min(_CONVERSION_BATCH_BYTES, remaining)

    def _remaining_allowance(self) -> int | None:
        """Operation bytes still unspent, or ``None`` for no ceiling.

        ``None`` has to stay ``None``: a user who turned the guardrail off gets
        no cap, and substituting a number here would quietly impose one.
        """

        maximum = self._budget.maximum
        if maximum is None:
            return None
        return max(1, maximum - self._budget.used)

    def _read_into(
        self,
        source: _ArchiveSource,
        batch: Sequence[PageRecord],
        root: Path,
        budget: ArchiveOperationBudget,
    ) -> int:
        """Read *batch* and spill it to *root*. Returns the bytes written."""

        payloads = self._read_entries(
            source,
            tuple((page.name, page.password) for page in batch),
            limits=self._limits,
            budget=budget,
        )
        written = 0
        for page in batch:
            payload = payloads.get(page.name)
            if payload is None:
                raise ArchiveReadError(f"Archive entry was not extracted: {page.name}")
            target = root / page.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            written += len(payload)
        return written


#: Bytes one non-bulk batch may materialize at once. The planner aims at this
#: using declared sizes; the scoped budget in ``_flush_batch`` is what makes it
#: true when those sizes are wrong.
_CONVERSION_BATCH_BYTES = 64 * 1024 * 1024


class ArchiveImageService:
    """Create image sessions from supported comic archive files.

    For 7z and RAR families the service consults an
    :class:`ArchiveExtractionPool` instead of re-decompressing pages on every
    access. The pool is supplied by the application (so it is shared with the
    settings UI for "Clear cache" and live resizing) but tests can pass a
    ``page_cache_dir`` to keep the legacy behaviour without standing up the
    pool plumbing.
    """

    def __init__(
        self,
        page_cache_dir: str | Path | None = None,
        *,
        extraction_pool: ArchiveExtractionCache | None = None,
        backend_resolver: ExtractionBackendResolver | None = None,
    ) -> None:
        if extraction_pool is not None and page_cache_dir is not None:
            raise ValueError("Pass either extraction_pool or page_cache_dir, not both.")
        if extraction_pool is not None:
            self._page_cache = extraction_pool
        elif page_cache_dir is not None:
            # Default budget keeps existing tests behaving as if the cache is
            # unbounded; production callers always inject a configured pool.
            self._page_cache = ArchiveExtractionPool(Path(page_cache_dir), max_bytes=1 << 40)
        else:
            self._page_cache = ArchiveExtractionPool(None, max_bytes=0)
        self._backend_resolver = backend_resolver or ExtractionBackendResolver()
        # rarfile stores executable configuration in module globals.  Keep
        # configuration and a delegated read atomic across service instances.
        self._rar_lock = _RARFILE_LOCK
        self._zip_backend = ZipArchiveBackend(
            lambda: pyzipper,
            lambda: _ZIP_BAD_FILE_ERRORS,
            self._request_password,
            self._backend_resolver,
        )
        self._seven_zip_backend = SevenZipArchiveBackend(
            lambda: py7zr,
            self._request_password,
        )
        self._rar_backend = RarArchiveBackend(
            lambda: rarfile,
            self._backend_resolver,
            self._rar_lock,
            self._request_password,
        )
        self._format_backends: dict[str, ArchiveFormatBackend] = {
            **{suffix: self._zip_backend for suffix in _ZIP_EXTENSIONS},
            **{suffix: self._seven_zip_backend for suffix in _SEVEN_ZIP_EXTENSIONS},
            **{suffix: self._rar_backend for suffix in _RAR_EXTENSIONS},
        }
        self._scanner = ArchiveScanner(self._list_entries, self._read_entry)

    def probe_archive(
        self,
        archive_path: str | Path,
        *,
        limits: ArchiveOpenLimits | None = None,
    ) -> ArchiveProbeResult:
        """Cheaply establish that a container lists and holds supported content.

        Deliberately shallow: it lists the top level and nothing more. It takes
        no password arguments because it can never use one -- an encrypted
        container is a result (``PASSWORD_REQUIRED``), not a prompt -- and no
        depth arguments because it does not recurse. The import gate that does
        recurse is :meth:`inspect_for_import`; reading pages is :meth:`open`.
        """

        operation = create_operation("archive.probe", category="archive")
        started = perf_counter()
        suffix = Path(archive_path).suffix.lower()
        with bind_operation(operation):
            logger.debug(
                "Archive probe started",
                extra={
                    "event": "archive.probe.started",
                    "category": "archive",
                    "status": "started",
                    "action": suffix.lstrip("."),
                    "document_id": str(archive_path),
                },
            )
            try:
                result = self._probe_archive_bound(archive_path, limits=limits)
            except Exception as exc:
                logger.error(
                    "Archive probe failed unexpectedly",
                    exc_info=True,
                    extra={
                        "event": "archive.probe.failed",
                        "category": "archive",
                        "status": "failed",
                        "action": suffix.lstrip("."),
                        "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                        "error_type": type(exc).__name__,
                    },
                )
                raise
            logger.log(
                logging.DEBUG if result.is_valid else logging.WARNING,
                "Archive probe finished",
                extra={
                    "event": "archive.probe.finished",
                    "category": "archive",
                    "status": "finished" if result.is_valid else "rejected",
                    "action": suffix.lstrip("."),
                    "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                    "error_code": result.code.value,
                    "error_type": result.error_type,
                },
            )
            return result

    def _probe_archive_bound(
        self,
        archive_path: str | Path,
        *,
        limits: ArchiveOpenLimits | None = None,
    ) -> ArchiveProbeResult:
        path = Path(archive_path)
        suffix = path.suffix.lower()
        archive_format = suffix.lstrip(".").upper() or None
        # Only ``max_source_bytes`` is consulted below; a shallow probe has no
        # depth to bound, so the legacy depth-argument bridge is not needed.
        effective_limits = limits if limits is not None else ArchiveOpenLimits()

        if not path.exists():
            return self._probe_result(
                path,
                ArchiveValidationCode.MISSING,
                f"Archive file does not exist: {path}",
                archive_format=archive_format,
                error_type=ArchiveOpenError.__name__,
            )
        if not path.is_file():
            return self._probe_result(
                path,
                ArchiveValidationCode.NOT_FILE,
                f"Archive path is not a file: {path}",
                archive_format=archive_format,
                error_type=ArchiveOpenError.__name__,
            )
        if suffix not in ARCHIVE_EXTENSIONS:
            return self._probe_result(
                path,
                ArchiveValidationCode.UNSUPPORTED_FORMAT,
                f"Unsupported archive format: {suffix or path.name}",
                archive_format=archive_format,
                error_type=ArchiveUnsupportedFormat.__name__,
            )

        try:
            self._assert_source_size(path, effective_limits)
            source = _ArchiveSource(label=path.name, suffix=suffix, path=path)
            inspection = self._backend_for(source).probe_entries(source)
        except ArchiveError as exc:
            code = _validation_code_for_error(exc)
            return self._probe_result(
                path,
                code,
                str(exc),
                archive_format=archive_format,
                error_type=type(exc).__name__,
            )

        if inspection.is_encrypted:
            return self._probe_result(
                path,
                ArchiveValidationCode.PASSWORD_REQUIRED,
                f"Password-protected archive cannot be imported: {path}",
                archive_format=archive_format,
                is_encrypted=True,
                error_type=ArchivePasswordRequired.__name__,
            )

        has_direct_images = False
        has_nested_archives = False
        for entry in inspection.entries:
            safe_name = _safe_entry_name(entry.name)
            if safe_name is None or _is_junk_entry(safe_name):
                continue
            entry_suffix = Path(safe_name).suffix.lower()
            if entry_suffix in IMAGE_EXTENSIONS:
                has_direct_images = True
            elif entry_suffix in ARCHIVE_EXTENSIONS:
                has_nested_archives = True
            if has_direct_images and has_nested_archives:
                break

        if not has_direct_images and not has_nested_archives:
            return self._probe_result(
                path,
                ArchiveValidationCode.EMPTY,
                f"No supported image or nested archive entries found: {path}",
                archive_format=archive_format,
                error_type=ArchiveEmptyError.__name__,
            )

        return self._probe_result(
            path,
            ArchiveValidationCode.OK,
            "Archive container contains supported image content.",
            archive_format=archive_format,
            is_valid=True,
            has_direct_images=has_direct_images,
            has_nested_archive_candidates=has_nested_archives,
        )

    def inspect_for_import(
        self,
        archive_path: str | Path,
        *,
        limits: ArchiveOpenLimits | None = None,
    ) -> ArchiveImportInspection:
        """Decide whether the library may keep this archive.

        Unlike :meth:`probe_archive` this walks every nested container, and
        unlike :meth:`open` it never asks for a password -- there is no provider
        to pass. See :mod:`joyread.core.archive.inspection` for why importing
        needs a stricter answer than reading does.
        """

        operation = create_operation("archive.inspect", category="archive")
        started = perf_counter()
        path = Path(archive_path)
        suffix = path.suffix.lower()
        with bind_operation(operation):
            gate = self._import_gate_failure(path, suffix, limits)
            if gate is not None:
                return gate
            effective_limits = limits if limits is not None else ArchiveOpenLimits()
            source = _ArchiveSource(label=path.name, suffix=suffix, path=path)
            inspector = ArchiveImportInspector(
                lambda inspected: self._backend_for(inspected).probe_entries(inspected),
                self._read_entry,
            )
            result = inspector.inspect(
                source,
                limits=effective_limits,
                budget=ArchiveOperationBudget(effective_limits.max_operation_bytes),
            )
            logger.log(
                logging.DEBUG if result.accepted else logging.WARNING,
                "Archive import inspection finished",
                extra={
                    "event": "archive.inspect.finished",
                    "category": "archive",
                    "status": "finished" if result.accepted else "rejected",
                    "action": suffix.lstrip("."),
                    "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                    "error_code": result.rejection.value if result.rejection else None,
                    "count": result.image_count,
                },
            )
            return result

    def _import_gate_failure(
        self,
        path: Path,
        suffix: str,
        limits: ArchiveOpenLimits | None,
    ) -> ArchiveImportInspection | None:
        """Root-level checks that do not need a backend, mirroring the probe."""

        if not path.exists():
            return _rejected_inspection(
                ImportRejection.MALFORMED_ROOT, f"Archive file does not exist: {path}", path.name
            )
        if not path.is_file():
            return _rejected_inspection(
                ImportRejection.MALFORMED_ROOT, f"Archive path is not a file: {path}", path.name
            )
        if suffix not in ARCHIVE_EXTENSIONS:
            return _rejected_inspection(
                ImportRejection.MALFORMED_ROOT,
                f"Unsupported archive format: {suffix or path.name}",
                path.name,
            )
        try:
            self._assert_source_size(path, limits if limits is not None else ArchiveOpenLimits())
        except ArchiveResourceLimitError as exc:
            return _rejected_inspection(
                ImportRejection.LIMIT_EXCEEDED,
                (
                    f"This archive exceeds the configured {exc.limit} limit. "
                    "Raise the limit in Settings to import it."
                ),
                path.name,
            )
        except ArchiveOpenError as exc:
            return _rejected_inspection(ImportRejection.MALFORMED_ROOT, str(exc), path.name)
        return None

    def convert_to_canonical(
        self,
        archive_path: str | Path,
        destination: Path,
        *,
        limits: ArchiveOpenLimits | None = None,
        writer: CanonicalWriter | None = None,
        sidecars: tuple[ArchiveMetadataEntry, ...] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        on_page: Callable[[int, int], None] | None = None,
        on_extract: Callable[[], None] | None = None,
    ) -> CanonicalWriteResult:
        """Repackage *archive_path* into one canonical container at *destination*.

        Runs the reader's own scan, so the page order and the table of contents
        are the ones the reader will show -- rebuilt as directories rather than
        as nested containers.

        *on_extract* fires before each container is pulled out of the source,
        which is the one long step that emits no page events of its own.

        *sidecars* are the metadata entries to embed. Pass the ones the import
        inspection already selected: this method will not re-inspect to find
        them, because that means walking (and materializing) every nested
        container a second time.

        Never prompts. ``ArchivePasswordPolicy.FORBID`` is not a precaution here
        but the contract: import already refused every encrypted archive at the
        inspection gate, so a password request during conversion would mean the
        gate was bypassed.
        """

        operation = create_operation("archive.canonical", category="archive")
        started = perf_counter()
        path = Path(archive_path)
        with bind_operation(operation):
            effective_limits = limits if limits is not None else ArchiveOpenLimits()
            source = _ArchiveSource(
                label=path.name, suffix=path.suffix.lower(), path=path
            )
            context = _ScanContext(
                password_provider=None,
                password_policy=ArchivePasswordPolicy.FORBID,
                skipped_archives=set(),
                limits=effective_limits,
                budget=ArchiveOperationBudget(effective_limits.max_operation_bytes),
            )
            root = self._scanner.scan(source, context)
            _disambiguate_nested_archive_labels(root)
            placed = _flatten_for_writing(root)
            if not placed:
                raise ArchiveEmptyError(
                    f"No supported image pages found to convert: {path}"
                )

            budget = ArchiveOperationBudget(effective_limits.max_operation_bytes)
            # ``ignore_cleanup_errors`` for the same reason the 7-Zip staging
            # code passes it (seven_zip_command.py): teardown happens after
            # the artifact is written and verified, so a file still held by a
            # scanner or a slow unmap would raise here and turn a finished
            # conversion into a failed one.
            with TemporaryDirectory(
                prefix="joyread-canonical-", ignore_cleanup_errors=True
            ) as workspace:
                reader = _StagedPageReader(
                    Path(workspace),
                    placed,
                    bulk_extract_for=self._bulk_extract_for,
                    read_entries=self._read_entries,
                    limits=effective_limits,
                    budget=budget,
                    is_cancelled=is_cancelled,
                    on_extract=on_extract,
                )
                result = (writer or CbzWriter()).write(
                    destination,
                    placed,
                    sidecars or (),
                    read_page=reader.read,
                    is_cancelled=is_cancelled,
                    on_page=on_page,
                )
            logger.info(
                "Canonical archive written",
                extra={
                    "event": "archive.canonical.finished",
                    "category": "archive",
                    "status": "finished",
                    "action": path.suffix.lstrip("."),
                    "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                    "count": result.page_count,
                },
            )
            return result

    def open(
        self,
        archive_path: str | Path,
        password_provider: PasswordProvider | None = None,
        password_policy: ArchivePasswordPolicy = ArchivePasswordPolicy.ALLOW,
        max_depth: int | None = None,
        max_nested_depth: int | None = None,
        global_file_max_depth: int | None = None,
        limits: ArchiveOpenLimits | None = None,
        document_cache_key: str | None = None,
        allow_persistent_cache: bool = False,
        cache_lease: ArchiveCacheLease | None = None,
    ) -> ArchiveImageSession:
        operation = create_operation("archive.open", category="archive")
        started = perf_counter()
        suffix = Path(archive_path).suffix.lower()
        with bind_operation(operation):
            logger.info(
                "Archive open started",
                extra={
                    "event": "archive.open.started",
                    "category": "archive",
                    "status": "started",
                    "action": suffix.lstrip("."),
                    "document_id": str(archive_path),
                    "identity_kind": (
                        cache_identity_kind(cache_lease.document_cache_key)
                        if cache_lease is not None
                        else "direct"
                    ),
                },
            )
            try:
                session = self._open_bound(
                    archive_path,
                    password_provider=password_provider,
                    password_policy=password_policy,
                    max_depth=max_depth,
                    max_nested_depth=max_nested_depth,
                    global_file_max_depth=global_file_max_depth,
                    limits=limits,
                    document_cache_key=document_cache_key,
                    allow_persistent_cache=allow_persistent_cache,
                    cache_lease=cache_lease,
                )
            except (ArchivePasswordRequired, ArchivePasswordRejected) as exc:
                logger.info(
                    "Archive open requires password input",
                    extra={
                        "event": "archive.open.input_required",
                        "category": "archive",
                        "status": "input_required",
                        "action": suffix.lstrip("."),
                        "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                        "error_type": type(exc).__name__,
                    },
                )
                raise
            except ArchiveError as exc:
                logger.warning(
                    "Archive open failed with a controlled archive error",
                    extra={
                        "event": "archive.open.failed",
                        "category": "archive",
                        "status": "failed",
                        "action": suffix.lstrip("."),
                        "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                        "error_type": type(exc).__name__,
                        "error_code": getattr(exc, "limit", None),
                    },
                )
                raise
            except Exception as exc:
                logger.error(
                    "Archive open failed unexpectedly",
                    exc_info=True,
                    extra={
                        "event": "archive.open.failed",
                        "category": "archive",
                        "status": "failed",
                        "action": suffix.lstrip("."),
                        "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                        "error_type": type(exc).__name__,
                    },
                )
                raise
            logger.info(
                "Archive open finished",
                extra={
                    "event": "archive.open.finished",
                    "category": "archive",
                    "status": "finished",
                    "action": suffix.lstrip("."),
                    "duration_ms": round((perf_counter() - started) * 1000.0, 3),
                    "count": session.page_count,
                },
            )
            return session

    def _open_bound(
        self,
        archive_path: str | Path,
        password_provider: PasswordProvider | None = None,
        password_policy: ArchivePasswordPolicy = ArchivePasswordPolicy.ALLOW,
        max_depth: int | None = None,
        max_nested_depth: int | None = None,
        global_file_max_depth: int | None = None,
        limits: ArchiveOpenLimits | None = None,
        document_cache_key: str | None = None,
        allow_persistent_cache: bool = False,
        cache_lease: ArchiveCacheLease | None = None,
    ) -> ArchiveImageSession:
        if cache_lease is not None and (document_cache_key is not None or allow_persistent_cache):
            raise ValueError(
                "Pass cache_lease or legacy document_cache_key/allow_persistent_cache, not both."
            )
        effective_limits = _resolve_open_limits(
            limits,
            max_depth=max_depth,
            max_nested_depth=max_nested_depth,
            global_file_max_depth=global_file_max_depth,
        )
        path = Path(archive_path)
        suffix = path.suffix.lower()
        logger.debug(
            "Archive open: path=%s suffix=%s nested_depth=%s global_file_depth=%s policy=%s",
            path,
            suffix,
            effective_limits.nested_archive_max_depth,
            effective_limits.global_file_max_depth,
            password_policy.value if hasattr(password_policy, "value") else password_policy,
        )
        if not path.exists():
            raise ArchiveOpenError(f"Archive does not exist: {path}")
        if not path.is_file():
            raise ArchiveOpenError(f"Archive path is not a file: {path}")
        if suffix not in ARCHIVE_EXTENSIONS:
            raise ArchiveUnsupportedFormat(f"Unsupported archive format: {suffix or path.name}")
        self._assert_source_size(path, effective_limits)

        effective_lease = cache_lease
        if effective_lease is None and allow_persistent_cache and document_cache_key and self._page_cache:
            effective_lease = ArchiveCacheLease(
                self._page_cache,
                document_cache_key,
                ArchiveCacheScope.PERSISTENT,
            )

        source = _ArchiveSource(
            label=path.name,
            suffix=suffix,
            path=path,
            allow_persistent_cache=effective_lease is not None,
        )
        # Nested archives are spilled here so they have a real path, which is
        # what makes the 7-Zip helper reachable for them at all. Ownership
        # passes to the session on success; every path out before that has to
        # remove it, or an archive that fails to open leaks its nested bytes.
        spill_dir = Path(mkdtemp(prefix="joyread-nested-"))
        try:
            context = _ScanContext(
                password_provider=password_provider,
                password_policy=password_policy,
                skipped_archives=set(),
                limits=effective_limits,
                budget=ArchiveOperationBudget(effective_limits.max_operation_bytes),
                spill_dir=spill_dir,
            )
            root = self._scanner.scan(source, context)
            _disambiguate_nested_archive_labels(root)
            pages, contents = _flatten_archive_tree(root)
            if not pages:
                if context.skipped_archives:
                    raise ArchiveEmptyError("No readable images. Encrypted archives were skipped.")
                raise ArchiveEmptyError(
                    "No supported image pages found within the configured archive depth limits: "
                    f"{path}"
                )
        except BaseException:
            rmtree(spill_dir, ignore_errors=True)
            raise
        cache_signature = (
            f"archive-pages:scanner-v{SCANNER_SCHEMA_VERSION}:"
            f"{effective_limits.cache_signature()}"
        )
        return ArchiveImageSession(
            pages,
            lambda source, entries, budget: self._read_entries(
                source,
                entries,
                limits=effective_limits,
                budget=budget,
            ),
            contents,
            bulk_extract=self._bulk_extract_for(source),
            cache_lease=effective_lease,
            cache_signature=cache_signature,
            limits=effective_limits,
            spill_dir=spill_dir,
        )

    def _bulk_extract_for(self, source: _ArchiveSource) -> BulkExtract | None:
        """The one-pass whole-document extractor for this container, if any.

        Only the top-level source is offered. A session whose pages come from
        several containers, or from a nested archive held in memory, refuses
        bulk conversion on its own; the capability is bound here to the backend
        that owns the file on disk.
        """

        backend = self._format_backends.get(source.suffix)
        supports_bulk = getattr(backend, "supports_bulk_extraction", None)
        extract_members = getattr(backend, "extract_members", None)
        if not callable(supports_bulk) or not callable(extract_members):
            return None
        return extract_members if supports_bulk(source) else None

    @staticmethod
    def _assert_source_size(path: Path, limits: ArchiveOpenLimits) -> None:
        maximum = limits.max_source_bytes
        if maximum is None:
            return
        try:
            actual = path.stat().st_size
        except OSError as exc:
            raise ArchiveOpenError(f"Could not inspect archive size: {path}") from exc
        if actual > maximum:
            raise ArchiveResourceLimitError(
                "source_bytes",
                actual=actual,
                maximum=maximum,
                subject=path.name,
            )

    def _probe_result(
        self,
        path: Path,
        code: ArchiveValidationCode,
        message: str,
        *,
        archive_format: str | None,
        is_valid: bool = False,
        is_encrypted: bool = False,
        has_direct_images: bool = False,
        has_nested_archive_candidates: bool = False,
        error_type: str | None = None,
    ) -> ArchiveProbeResult:
        return ArchiveProbeResult(
            path=path,
            is_valid=is_valid,
            code=code,
            message=message,
            archive_format=archive_format,
            is_encrypted=is_encrypted,
            has_direct_images=has_direct_images,
            has_nested_archive_candidates=has_nested_archive_candidates,
            error_type=error_type,
        )

    def _list_entries(
        self,
        source: _ArchiveSource,
        context: _ScanContext,
    ) -> _ArchiveListing:
        return self._backend_for(source).list_entries(source, context)

    def _read_entry(
        self,
        source: _ArchiveSource,
        name: str,
        password: str | None,
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> bytes:
        payload = self._read_entries(source, ((name, password),), limits=limits, budget=budget).get(name)
        if payload is None:
            raise ArchiveReadError(f"Archive entry was not extracted: {name}")
        return payload

    def _read_entries(
        self,
        source: _ArchiveSource,
        entries: Sequence[tuple[str, str | None]],
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> dict[str, bytes]:
        if not entries:
            return {}
        return self._read_entries_uncached(source, entries, limits=limits, budget=budget)

    def _read_entries_uncached(
        self,
        source: _ArchiveSource,
        entries: Sequence[tuple[str, str | None]],
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> dict[str, bytes]:
        return self._backend_for(source).read_entries(
            source,
            entries,
            limits=limits,
            budget=budget,
        )

    def _backend_for(self, source: _ArchiveSource) -> ArchiveFormatBackend:
        backend = self._format_backends.get(source.suffix)
        if backend is None:
            raise ArchiveUnsupportedFormat(f"Unsupported archive format: {source.suffix}")
        return backend

    def _request_password(
        self,
        source: _ArchiveSource,
        context: _ScanContext,
        attempt: int = 1,
        reason: str | None = None,
    ) -> str:
        if context.password_policy == ArchivePasswordPolicy.FORBID:
            raise ArchivePasswordRequired(
                f"Skipped encrypted archive: {source.display_name}",
                archive_path=source.display_name,
            )
        if context.password_provider is None:
            raise ArchivePasswordRequired(
                f"Password required for archive: {source.display_name}",
                archive_path=source.display_name,
            )

        response = context.password_provider(
            ArchivePasswordRequest(
                archive_path=source.display_name,
                archive_format=source.suffix.lstrip(".").upper(),
                attempt=attempt,
                reason=reason,
            )
        )
        if isinstance(response, ArchivePasswordResponse):
            if response.skip:
                context.skipped_archives.add(source.display_name)
                raise _ArchiveSourceSkipped()
            password = response.password
        else:
            password = response
        if password is None:
            raise ArchivePasswordRequired(
                f"Password request cancelled for archive: {source.display_name}",
                archive_path=source.display_name,
            )
        return password



def _resolve_nested_depth(max_depth: object | None, max_nested_depth: object | None) -> int | None:
    if max_depth is not None and max_nested_depth is not None:
        legacy = _coerce_depth_limit(
            max_depth,
            default=_DEFAULT_NESTED_ARCHIVE_DEPTH,
            maximum=_MAX_NESTED_ARCHIVE_DEPTH,
        )
        explicit = _coerce_depth_limit(
            max_nested_depth,
            default=_DEFAULT_NESTED_ARCHIVE_DEPTH,
            maximum=_MAX_NESTED_ARCHIVE_DEPTH,
        )
        if legacy != explicit:
            raise ValueError("max_depth and max_nested_depth must match when both are provided")
        return explicit
    selected = max_nested_depth if max_nested_depth is not None else max_depth
    return _coerce_depth_limit(
        selected,
        default=_DEFAULT_NESTED_ARCHIVE_DEPTH,
        maximum=_MAX_NESTED_ARCHIVE_DEPTH,
    )


def _rejected_inspection(
    rejection: ImportRejection,
    message: str,
    at: str,
) -> ArchiveImportInspection:
    return ArchiveImportInspection(
        accepted=False, message=message, rejection=rejection, rejected_at=at
    )


def _resolve_open_limits(
    limits: ArchiveOpenLimits | None,
    *,
    max_depth: object | None,
    max_nested_depth: object | None,
    global_file_max_depth: object | None,
) -> ArchiveOpenLimits:
    """Bridge legacy depth arguments to the immutable limits contract."""

    if limits is None:
        return ArchiveOpenLimits(
            nested_archive_max_depth=_resolve_nested_depth(max_depth, max_nested_depth),
            global_file_max_depth=_coerce_depth_limit(
                global_file_max_depth,
                default=_DEFAULT_GLOBAL_FILE_DEPTH,
                maximum=_MAX_GLOBAL_FILE_DEPTH,
            ),
        )
    if max_depth is not None or max_nested_depth is not None:
        legacy_depth = _resolve_nested_depth(max_depth, max_nested_depth)
        if legacy_depth != limits.nested_archive_max_depth:
            raise ValueError("limits and legacy nested archive depth arguments must match")
    if global_file_max_depth is not None:
        legacy_global_depth = _coerce_depth_limit(
            global_file_max_depth,
            default=_DEFAULT_GLOBAL_FILE_DEPTH,
            maximum=_MAX_GLOBAL_FILE_DEPTH,
        )
        if legacy_global_depth != limits.global_file_max_depth:
            raise ValueError("limits and global_file_max_depth must match")
    return limits


def _coerce_depth_limit(value: object, *, default: int, maximum: int) -> int | None:
    if value is None:
        return default
    try:
        depth = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if depth == -1:
        return None
    return max(1, min(maximum, depth))



def _validation_code_for_error(error: ArchiveError) -> ArchiveValidationCode:
    if isinstance(error, ArchiveResourceLimitError):
        return ArchiveValidationCode.RESOURCE_LIMIT_EXCEEDED
    if isinstance(error, ArchiveUnsupportedFormat):
        return ArchiveValidationCode.UNSUPPORTED_FORMAT
    if isinstance(error, ArchiveOpenError):
        return ArchiveValidationCode.OPEN_FAILED
    if isinstance(error, ArchiveReadError):
        return ArchiveValidationCode.READ_FAILED
    if isinstance(error, ArchiveCorruptError):
        return ArchiveValidationCode.CORRUPT
    if isinstance(error, ArchiveEmptyError):
        return ArchiveValidationCode.EMPTY
    if isinstance(error, ArchivePasswordRequired):
        return ArchiveValidationCode.PASSWORD_REQUIRED
    if isinstance(error, ArchivePasswordRejected):
        return ArchiveValidationCode.PASSWORD_REJECTED
    if isinstance(error, ArchiveDependencyMissing):
        return ArchiveValidationCode.DEPENDENCY_MISSING
    return ArchiveValidationCode.UNKNOWN_ERROR
