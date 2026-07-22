"""Archive-backed image page discovery and access.

This module intentionally exposes archive data as a UI-free service. Thumbnail
generation, reader rendering, and import workflows should consume this API
instead of parsing archive formats directly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import RLock
from typing import Sequence
from zipfile import BadZipFile

from joyread.core.file_types import ARCHIVE_EXTENSIONS


logger = logging.getLogger(__name__)

from joyread.core.archive.errors import (
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
    ArchiveValidationCode,
    ArchiveValidationResult,
    PasswordProvider,
)
from joyread.core.archive.backends import ExtractionBackendResolver
from joyread.core.archive.formats import (
    ArchiveFormatBackend,
    RarArchiveBackend,
    SevenZipArchiveBackend,
    ZipArchiveBackend,
)
from joyread.core.archive.records import ArchiveEntry as _ArchiveEntry
from joyread.core.archive.records import ArchiveSource as _ArchiveSource
from joyread.core.archive.scanner import ArchiveScanContext as _ScanContext
from joyread.core.archive.scanner import (
    SCANNER_SCHEMA_VERSION,
    ArchiveScanner,
    ArchiveSourceSkipped as _ArchiveSourceSkipped,
)
from joyread.core.archive.session import ArchiveImageSession, EXPENSIVE_ARCHIVE_EXTENSIONS
from joyread.core.archive.tree import (
    disambiguate_nested_archive_labels as _disambiguate_nested_archive_labels,
    flatten_archive_tree as _flatten_archive_tree,
)
from joyread.core.services.archive_extraction_pool import ArchiveExtractionCache, ArchiveExtractionPool

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

    def validate_archive(
        self,
        archive_path: str | Path,
        password_provider: PasswordProvider | None = None,
        password_policy: ArchivePasswordPolicy = ArchivePasswordPolicy.ALLOW,
        max_depth: int | None = None,
        max_nested_depth: int | None = None,
        global_file_max_depth: int | None = None,
        limits: ArchiveOpenLimits | None = None,
    ) -> ArchiveValidationResult:
        """Return structured feedback without raising controlled archive errors.

        Use this for import/preflight/UI paths. Reader code that needs page
        access should still call `open()` and keep the returned session alive.
        """

        path = Path(archive_path)
        suffix = path.suffix.lower()
        archive_format = suffix.lstrip(".").upper() or None
        effective_limits = _resolve_open_limits(
            limits,
            max_depth=max_depth,
            max_nested_depth=max_nested_depth,
            global_file_max_depth=global_file_max_depth,
        )

        if not path.exists():
            return self._validation_result(
                path,
                ArchiveValidationCode.MISSING,
                f"Archive file does not exist: {path}",
                archive_format=archive_format,
                error_type=ArchiveOpenError.__name__,
            )
        if not path.is_file():
            return self._validation_result(
                path,
                ArchiveValidationCode.NOT_FILE,
                f"Archive path is not a file: {path}",
                archive_format=archive_format,
                error_type=ArchiveOpenError.__name__,
            )
        if suffix not in ARCHIVE_EXTENSIONS:
            return self._validation_result(
                path,
                ArchiveValidationCode.UNSUPPORTED_FORMAT,
                f"Unsupported archive format: {suffix or path.name}",
                archive_format=archive_format,
                error_type=ArchiveUnsupportedFormat.__name__,
            )

        try:
            session = self.open(
                path,
                password_provider=password_provider,
                password_policy=password_policy,
                limits=effective_limits,
            )
            first_page = session.get_page(0)
        except ArchiveError as exc:
            code = _validation_code_for_error(exc)
            return self._validation_result(
                path,
                code,
                str(exc),
                archive_format=archive_format,
                error_type=type(exc).__name__,
            )

        if first_page is None:
            return self._validation_result(
                path,
                ArchiveValidationCode.READ_FAILED,
                f"Archive pages were listed but the first image could not be decoded: {path}",
                archive_format=archive_format,
                page_count=session.page_count,
                error_type=ArchiveReadError.__name__,
            )

        return self._validation_result(
            path,
            ArchiveValidationCode.OK,
            f"Archive is readable with {session.page_count} image page(s).",
            archive_format=archive_format,
            page_count=session.page_count,
            is_valid=True,
        )

    def open(
        self,
        archive_path: str | Path,
        password_provider: PasswordProvider | None = None,
        password_policy: ArchivePasswordPolicy = ArchivePasswordPolicy.ALLOW,
        max_depth: int | None = None,
        max_nested_depth: int | None = None,
        global_file_max_depth: int | None = None,
        limits: ArchiveOpenLimits | None = None,
    ) -> ArchiveImageSession:
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

        source = _ArchiveSource(label=path.name, suffix=suffix, path=path)
        context = _ScanContext(
            password_provider=password_provider,
            password_policy=password_policy,
            skipped_archives=set(),
            limits=effective_limits,
            budget=ArchiveOperationBudget(effective_limits.max_operation_bytes),
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
        logger.info("Archive opened: %s pages=%d", path.name, len(pages))
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
            document_path=path,
            extraction_cache=self._page_cache,
            cache_signature=cache_signature,
            limits=effective_limits,
        )

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

    def _validation_result(
        self,
        path: Path,
        code: ArchiveValidationCode,
        message: str,
        *,
        archive_format: str | None,
        page_count: int | None = None,
        is_valid: bool = False,
        error_type: str | None = None,
    ) -> ArchiveValidationResult:
        file_size: int | None = None
        mtime_ns: int | None = None
        try:
            if path.is_file():
                stat = path.stat()
                file_size = stat.st_size
                mtime_ns = stat.st_mtime_ns
        except OSError:
            # Validation must be safe for UI/import scans; stat failures are
            # reported through the main validation code instead of bubbling up.
            pass
        return ArchiveValidationResult(
            path=path,
            is_valid=is_valid,
            code=code,
            message=message,
            archive_format=archive_format,
            page_count=page_count,
            file_size=file_size,
            mtime_ns=mtime_ns,
            error_type=error_type,
        )

    def _list_entries(
        self,
        source: _ArchiveSource,
        context: _ScanContext,
    ) -> list[_ArchiveEntry]:
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
