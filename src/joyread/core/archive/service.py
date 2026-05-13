"""Archive-backed image page discovery and access.

This module intentionally exposes archive data as a UI-free service. Thumbnail
generation, reader rendering, and import workflows should consume this API
instead of parsing archive formats directly.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
import re
import subprocess
from tempfile import TemporaryDirectory
from typing import Iterable, Sequence
from zipfile import BadZipFile

from PIL import Image, UnidentifiedImageError

from joyread.core.archive.errors import (
    ArchiveCorruptError,
    ArchiveDependencyMissing,
    ArchiveEmptyError,
    ArchiveError,
    ArchiveOpenError,
    ArchivePasswordRejected,
    ArchivePasswordRequired,
    ArchiveReadError,
    ArchiveUnsupportedFormat,
)
from joyread.core.archive.models import (
    ArchivePage,
    ArchivePasswordRequest,
    ArchiveValidationCode,
    ArchiveValidationResult,
    PasswordProvider,
)
from joyread.core.archive.backends import ExtractionBackendResolver
from joyread.core.services.archive_extraction_pool import ArchiveExtractionCache, ArchiveExtractionPool

try:  # pragma: no cover - exercised through dependency-missing branches.
    import py7zr
    from py7zr.io import BytesIOFactory
except ImportError:  # pragma: no cover
    py7zr = None
    BytesIOFactory = None

try:  # pragma: no cover - exercised through dependency-missing branches.
    import pyzipper
except ImportError:  # pragma: no cover
    pyzipper = None

try:  # pragma: no cover - exercised through dependency-missing branches.
    import rarfile
except ImportError:  # pragma: no cover
    rarfile = None


IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"})
ARCHIVE_EXTENSIONS = frozenset({".zip", ".cbz", ".7z", ".cb7", ".rar", ".cbr"})
_ZIP_EXTENSIONS = frozenset({".zip", ".cbz"})
_SEVEN_ZIP_EXTENSIONS = frozenset({".7z", ".cb7"})
_RAR_EXTENSIONS = frozenset({".rar", ".cbr"})
_NATURAL_PART_RE = re.compile(r"(\d+)")
_SEVEN_ZIP_READ_LIMIT = 512 * 1024 * 1024
_ZIP_BAD_FILE_ERRORS = (BadZipFile,)
_EXPENSIVE_CACHE_EXTENSIONS = _SEVEN_ZIP_EXTENSIONS | _RAR_EXTENSIONS
EXPENSIVE_ARCHIVE_EXTENSIONS = _EXPENSIVE_CACHE_EXTENSIONS
if pyzipper is not None:  # pyzipper uses its own BadZipFile class.
    _ZIP_BAD_FILE_ERRORS = (BadZipFile, pyzipper.zipfile.BadZipFile)


@dataclass(frozen=True)
class _ArchiveSource:
    label: str
    suffix: str
    path: Path | None = None
    data: bytes | None = None

    @property
    def display_name(self) -> str:
        return str(self.path) if self.path is not None else self.label

    def open_arg(self) -> str | BytesIO:
        if self.data is not None:
            return BytesIO(self.data)
        if self.path is None:
            raise ArchiveOpenError(f"Archive source has no path: {self.label}")
        return str(self.path)


@dataclass(frozen=True)
class _ArchiveEntry:
    name: str
    size: int | None
    password: str | None


@dataclass
class _PageRecord:
    display_path: str
    source: _ArchiveSource
    name: str
    password: str | None
    dimensions: tuple[int, int] | None = None


class ArchiveImageSession:
    """Bounded access to image pages discovered inside one archive."""

    def __init__(
        self,
        pages: Iterable[_PageRecord],
        read_entries: Callable[[_ArchiveSource, Sequence[tuple[str, str | None]]], dict[str, bytes]],
    ) -> None:
        self._pages = list(pages)
        self._read_entries = read_entries
        self.current_index = 0

    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def index_range(self) -> range:
        return range(0, self.page_count)

    def is_not_empty(self) -> bool:
        return self.page_count > 0

    def is_valid_index(self, index: int) -> bool:
        return 0 <= index < self.page_count

    def has_next(self, index: int | None = None) -> bool:
        checked_index = self.current_index if index is None else index
        return self.is_valid_index(checked_index + 1)

    def has_previous(self, index: int | None = None) -> bool:
        checked_index = self.current_index if index is None else index
        return self.is_valid_index(checked_index - 1)

    def get_image(self, index: int) -> bytes | None:
        page = self.get_page(index)
        if page is None:
            return None
        return page.image_bytes

    def get_images(self, start: int, count: int) -> list[bytes | None]:
        if count <= 0:
            return []
        return [page.image_bytes if page is not None else None for page in self.get_pages(range(start, start + count))]

    def get_dimensions(self, index: int) -> tuple[int, int] | None:
        if not self.is_valid_index(index):
            return None
        record = self._pages[index]
        if record.dimensions is not None:
            return record.dimensions
        payload = self._read_entries(record.source, ((record.name, record.password),)).get(record.name)
        if payload is None:
            return None
        dimensions = _dimensions_from_bytes(payload)
        if dimensions is not None:
            record.dimensions = dimensions
        return dimensions

    def get_page(self, index: int) -> ArchivePage | None:
        return self.get_pages((index,))[0]

    def get_pages(self, indices: Iterable[int]) -> list[ArchivePage | None]:
        requested = list(indices)
        results: list[ArchivePage | None] = [None] * len(requested)
        missing: list[tuple[int, int, _PageRecord]] = []

        for result_index, page_index in enumerate(requested):
            if not self.is_valid_index(page_index):
                continue
            record = self._pages[page_index]
            missing.append((result_index, page_index, record))

        groups: OrderedDict[tuple[int, str | None], list[tuple[int, int, _PageRecord]]] = OrderedDict()
        for item in missing:
            record = item[2]
            groups.setdefault((id(record.source), record.password), []).append(item)

        for group in groups.values():
            source = group[0][2].source
            requests = [(record.name, record.password) for _result_index, _page_index, record in group]
            payloads = self._read_entries(source, requests)
            for result_index, page_index, record in group:
                payload = payloads.get(record.name)
                if payload is None:
                    continue
                page = _archive_page_from_bytes(page_index, record, payload)
                if page is None:
                    continue
                record.dimensions = page.dimensions
                results[result_index] = page

        return results

    def get_aspect_ratio(self, index: int) -> tuple[float, float] | None:
        dimensions = self.get_dimensions(index)
        if dimensions is None:
            return None
        width, height = dimensions
        if height == 0:
            return None
        return (float(width) / float(height), 1.0)

    def get_horizontal_aspect_ratio(self, indices: Iterable[int]) -> tuple[float, float] | None:
        ratios: list[tuple[float, float]] = []
        for index in indices:
            ratio = self.get_aspect_ratio(index)
            if ratio is None:
                return None
            ratios.append(ratio)
        if not ratios:
            return None
        return (sum(width for width, _height in ratios), 1.0)

    def current(self) -> bytes | None:
        return self.get_image(self.current_index)

    def seek(self, index: int) -> bool:
        if not self.is_valid_index(index):
            return False
        self.current_index = index
        return True

    def next(self) -> bytes | None:
        if not self.has_next():
            return None
        self.current_index += 1
        return self.current()

    def previous(self) -> bytes | None:
        if not self.has_previous():
            return None
        self.current_index -= 1
        return self.current()


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

    def validate_archive(
        self,
        archive_path: str | Path,
        password_provider: PasswordProvider | None = None,
        max_depth: int = 2,
        max_nested_depth: int | None = None,
    ) -> ArchiveValidationResult:
        """Return structured feedback without raising controlled archive errors.

        Use this for import/preflight/UI paths. Reader code that needs page
        access should still call `open()` and keep the returned session alive.
        """

        path = Path(archive_path)
        suffix = path.suffix.lower()
        archive_format = suffix.lstrip(".").upper() or None
        effective_max_depth = _coerce_depth(max_nested_depth if max_nested_depth is not None else max_depth)

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
                max_depth=effective_max_depth,
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
        max_depth: int = 2,
        max_nested_depth: int | None = None,
    ) -> ArchiveImageSession:
        effective_max_depth = _coerce_depth(max_nested_depth if max_nested_depth is not None else max_depth)
        path = Path(archive_path)
        suffix = path.suffix.lower()
        if not path.exists():
            raise ArchiveOpenError(f"Archive does not exist: {path}")
        if not path.is_file():
            raise ArchiveOpenError(f"Archive path is not a file: {path}")
        if suffix not in ARCHIVE_EXTENSIONS:
            raise ArchiveUnsupportedFormat(f"Unsupported archive format: {suffix or path.name}")

        source = _ArchiveSource(label=path.name, suffix=suffix, path=path)
        pages = self._scan_archive(
            source,
            password_provider,
            max_depth=effective_max_depth,
            archive_level=0,
        )
        if not pages:
            raise ArchiveEmptyError(
                f"No supported image pages found in archive within archive depth {effective_max_depth}: {path}"
            )
        return ArchiveImageSession(pages, self._read_entries)

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

    def _scan_archive(
        self,
        source: _ArchiveSource,
        password_provider: PasswordProvider | None,
        max_depth: int,
        archive_level: int,
    ) -> list[_PageRecord]:
        entries = self._list_entries(source, password_provider)
        entry_prefix = _transparent_single_root_prefix(entries)
        root_group: list[_PageRecord] = []
        group_by_parent: OrderedDict[str, list[_PageRecord]] = OrderedDict({"": root_group})
        segments: list[tuple[str, list[_PageRecord]]] = []

        for entry in entries:
            safe_name = _safe_entry_name(entry.name)
            if safe_name is None or _is_metadata_entry(safe_name):
                continue

            logical_name = _strip_transparent_prefix(safe_name, entry_prefix)
            if logical_name is None:
                continue
            suffix = PurePosixPath(logical_name).suffix.lower()
            entry_depth = _entry_depth(logical_name)
            if entry_depth > max_depth:
                continue
            if suffix in IMAGE_EXTENSIONS:
                page = self._page_record(source, safe_name, logical_name, entry.password)
                parent = _parent_group(logical_name)
                if parent == "":
                    root_group.append(page)
                    continue

                if parent not in group_by_parent:
                    group_by_parent[parent] = []
                    segments.append(("group", group_by_parent[parent]))
                group_by_parent[parent].append(page)
                continue

            if suffix in ARCHIVE_EXTENSIONS and archive_level < max_depth:
                nested_data = self._read_entry(source, safe_name, entry.password)
                nested_source = _ArchiveSource(
                    label=f"{source.label}::{logical_name}",
                    suffix=suffix,
                    data=nested_data,
                )
                nested_pages = self._scan_archive(
                    nested_source,
                    password_provider,
                    max_depth=max_depth,
                    archive_level=archive_level + 1,
                )
                if nested_pages:
                    segments.append(("nested", nested_pages))

        ordered_pages = _sort_group(root_group)
        for segment_type, segment_pages in segments:
            if segment_type == "group":
                ordered_pages.extend(_sort_group(segment_pages))
            else:
                ordered_pages.extend(segment_pages)
        return ordered_pages

    def _page_record(self, source: _ArchiveSource, name: str, logical_name: str, password: str | None) -> _PageRecord:
        display_path = f"{source.label}/{logical_name}"
        return _PageRecord(
            display_path=display_path,
            source=source,
            name=name,
            password=password,
        )

    def _list_entries(
        self,
        source: _ArchiveSource,
        password_provider: PasswordProvider | None,
    ) -> list[_ArchiveEntry]:
        if source.suffix in _ZIP_EXTENSIONS:
            return self._list_zip_entries(source, password_provider)
        if source.suffix in _SEVEN_ZIP_EXTENSIONS:
            return self._list_7z_entries(source, password_provider)
        if source.suffix in _RAR_EXTENSIONS:
            self._configure_rarfile_tools()
            return self._list_rar_entries(source, password_provider)
        raise ArchiveUnsupportedFormat(f"Unsupported nested archive format: {source.suffix}")

    def _read_entry(self, source: _ArchiveSource, name: str, password: str | None) -> bytes:
        payload = self._read_entries(source, ((name, password),)).get(name)
        if payload is None:
            raise ArchiveReadError(f"Archive entry was not extracted: {name}")
        return payload

    def _read_entries(
        self,
        source: _ArchiveSource,
        entries: Sequence[tuple[str, str | None]],
    ) -> dict[str, bytes]:
        if not entries:
            return {}

        if self._should_cache_extracted_entries(source, entries):
            assert source.path is not None  # narrowed by _should_cache_extracted_entries
            cached: dict[str, bytes] = {}
            missing: list[tuple[str, str | None]] = []
            for name, password in entries:
                page = self._page_cache.get(source.path, name)
                if page is None:
                    missing.append((name, password))
                else:
                    cached[name] = page
            if missing:
                extracted = self._read_entries_uncached(source, missing)
                self._page_cache.put_many(source.path, extracted)
                cached.update(extracted)
            return cached

        return self._read_entries_uncached(source, entries)

    def _read_entries_uncached(
        self,
        source: _ArchiveSource,
        entries: Sequence[tuple[str, str | None]],
    ) -> dict[str, bytes]:
        if source.suffix in _ZIP_EXTENSIONS:
            return self._read_zip_entries(source, entries)
        if source.suffix in _SEVEN_ZIP_EXTENSIONS:
            return self._read_7z_entries(source, entries)
        if source.suffix in _RAR_EXTENSIONS:
            return {name: self._read_rar_entry(source, name, password) for name, password in entries}
        raise ArchiveUnsupportedFormat(f"Unsupported archive format: {source.suffix}")

    def _should_cache_extracted_entries(
        self,
        source: _ArchiveSource,
        entries: Sequence[tuple[str, str | None]],
    ) -> bool:
        return (
            source.suffix in _EXPENSIVE_CACHE_EXTENSIONS
            and source.path is not None
            and all(password is None for _name, password in entries)
        )

    def _list_zip_entries(
        self,
        source: _ArchiveSource,
        password_provider: PasswordProvider | None,
    ) -> list[_ArchiveEntry]:
        if pyzipper is None:
            raise ArchiveDependencyMissing("pyzipper is required for ZIP/CBZ archives.")

        try:
            with pyzipper.AESZipFile(source.open_arg(), "r") as archive:
                infos = archive.infolist()
                encrypted = [info for info in infos if not info.is_dir() and info.flag_bits & 0x1]
                password = None
                if encrypted:
                    password = self._request_password(source, password_provider, reason="zip archive is encrypted")
                    self._verify_zip_password(source, encrypted[0].filename, password)
                return [
                    _ArchiveEntry(info.filename, getattr(info, "file_size", None), password)
                    for info in infos
                    if not info.is_dir()
                ]
        except _ZIP_BAD_FILE_ERRORS as exc:
            raise ArchiveCorruptError(f"Corrupt ZIP archive: {source.display_name}") from exc
        except OSError as exc:
            raise ArchiveOpenError(f"Could not open ZIP archive: {source.display_name}") from exc

    def _read_zip_entry(self, source: _ArchiveSource, name: str, password: str | None) -> bytes:
        payload = self._read_zip_entries(source, ((name, password),)).get(name)
        if payload is None:
            raise ArchiveReadError(f"ZIP entry was not extracted: {name}")
        return payload

    def _read_zip_entries(
        self,
        source: _ArchiveSource,
        entries: Sequence[tuple[str, str | None]],
    ) -> dict[str, bytes]:
        if pyzipper is None:
            raise ArchiveDependencyMissing("pyzipper is required for ZIP/CBZ archives.")

        try:
            payloads: dict[str, bytes] = {}
            with pyzipper.AESZipFile(source.open_arg(), "r") as archive:
                for name, password in entries:
                    pwd = password.encode("utf-8") if password is not None else None
                    payloads[name] = archive.read(name, pwd=pwd)
            return payloads
        except RuntimeError as exc:
            if _looks_like_password_error(exc):
                raise ArchivePasswordRejected(f"Password rejected for ZIP entry: {entries[0][0]}") from exc
            raise ArchiveReadError(f"Could not read ZIP entry: {entries[0][0]}") from exc
        except (*_ZIP_BAD_FILE_ERRORS, KeyError) as exc:
            raise ArchiveReadError(f"Could not read ZIP entry: {entries[0][0]}") from exc

    def _verify_zip_password(self, source: _ArchiveSource, name: str, password: str | None) -> None:
        self._read_zip_entry(source, name, password)

    def _list_7z_entries(
        self,
        source: _ArchiveSource,
        password_provider: PasswordProvider | None,
    ) -> list[_ArchiveEntry]:
        if py7zr is None:
            raise ArchiveDependencyMissing("py7zr is required for 7Z/CB7 archives.")

        password = None
        for attempt in range(1, 4):
            try:
                with py7zr.SevenZipFile(source.open_arg(), "r", password=password) as archive:
                    if archive.needs_password() and password is None:
                        password = self._request_password(
                            source,
                            password_provider,
                            attempt=attempt,
                            reason="7z archive is encrypted",
                        )
                        continue
                    return [
                        _ArchiveEntry(info.filename, getattr(info, "uncompressed", None), password)
                        for info in archive.list()
                        if getattr(info, "is_file", False)
                    ]
            except py7zr.PasswordRequired as exc:
                password = self._request_password(
                    source,
                    password_provider,
                    attempt=attempt,
                    reason="7z archive requires a password",
                )
                continue
            except py7zr.Bad7zFile as exc:
                if password is not None:
                    raise ArchivePasswordRejected(f"Password rejected for 7Z archive: {source.display_name}") from exc
                raise ArchiveCorruptError(f"Corrupt 7Z archive: {source.display_name}") from exc
            except OSError as exc:
                raise ArchiveOpenError(f"Could not open 7Z archive: {source.display_name}") from exc

        raise ArchivePasswordRejected(f"Password rejected for 7Z archive: {source.display_name}")

    def _read_7z_entry(self, source: _ArchiveSource, name: str, password: str | None) -> bytes:
        payload = self._read_7z_entries(source, ((name, password),)).get(name)
        if payload is None:
            raise ArchiveReadError(f"7Z entry was not extracted: {name}")
        return payload

    def _read_7z_entries(
        self,
        source: _ArchiveSource,
        entries: Sequence[tuple[str, str | None]],
    ) -> dict[str, bytes]:
        if py7zr is None or BytesIOFactory is None:
            raise ArchiveDependencyMissing("py7zr is required for 7Z/CB7 archives.")

        targets = [name for name, _password in entries]
        password = entries[0][1]
        try:
            factory = BytesIOFactory(_SEVEN_ZIP_READ_LIMIT)
            with py7zr.SevenZipFile(source.open_arg(), "r", password=password) as archive:
                archive.extract(targets=targets, factory=factory)
            payloads: dict[str, bytes] = {}
            for name in targets:
                product = factory.products.get(name)
                if product is None:
                    continue
                product.seek(0)
                payloads[name] = product.read()
            missing = [name for name in targets if name not in payloads]
            if missing:
                raise ArchiveReadError(f"7Z entries were not extracted: {', '.join(missing[:3])}")
            return payloads
        except py7zr.PasswordRequired as exc:
            raise ArchivePasswordRequired(f"Password required for 7Z entry: {targets[0]}") from exc
        except py7zr.DecompressionError as exc:
            if password is not None:
                raise ArchivePasswordRejected(f"Password rejected for 7Z entry: {targets[0]}") from exc
            raise ArchiveReadError(f"Could not decompress 7Z entry: {targets[0]}") from exc
        except py7zr.Bad7zFile as exc:
            raise ArchiveReadError(f"Could not read 7Z entry: {targets[0]}") from exc

    def _list_rar_entries(
        self,
        source: _ArchiveSource,
        password_provider: PasswordProvider | None,
    ) -> list[_ArchiveEntry]:
        if rarfile is None or not hasattr(rarfile, "RarFile"):
            raise ArchiveDependencyMissing("rarfile is required for RAR/CBR archives.")

        password = None
        try:
            with rarfile.RarFile(source.open_arg(), "r") as archive:
                if archive.needs_password():
                    password = self._request_password(source, password_provider, reason="rar archive is encrypted")
                return [
                    _ArchiveEntry(info.filename, getattr(info, "file_size", None), password)
                    for info in archive.infolist()
                    if not info.isdir()
                ]
        except rarfile.RarCannotExec as exc:
            raise ArchiveDependencyMissing(self._backend_resolver.missing_message(encrypted=password is not None)) from exc
        except rarfile.NeedFirstVolume as exc:
            raise ArchiveUnsupportedFormat("Multi-volume RAR archives are not supported yet.") from exc
        except rarfile.BadRarFile as exc:
            raise ArchiveCorruptError(f"Corrupt RAR archive: {source.display_name}") from exc
        except OSError as exc:
            raise ArchiveOpenError(f"Could not open RAR archive: {source.display_name}") from exc

    def _read_rar_entry(self, source: _ArchiveSource, name: str, password: str | None) -> bytes:
        rar_failure: Exception | None = None
        try:
            if rarfile is not None:
                self._configure_rarfile_tools()
                self._ensure_rar_backend()
                with rarfile.RarFile(source.open_arg(), "r") as archive:
                    return archive.read(name, pwd=password)
        except rarfile.PasswordRequired as exc:
            raise ArchivePasswordRequired(f"Password required for RAR entry: {name}") from exc
        except rarfile.RarWrongPassword as exc:
            raise ArchivePasswordRejected(f"Password rejected for RAR entry: {name}") from exc
        except (rarfile.RarCannotExec, rarfile.BadRarFile, OSError) as exc:
            rar_failure = exc

        try:
            return self._read_rar_entry_external(source, name, password)
        except ArchiveDependencyMissing:
            if rar_failure is not None:
                raise ArchiveDependencyMissing(
                    self._backend_resolver.missing_message(encrypted=password is not None)
                ) from rar_failure
            raise
        except ArchiveReadError as exc:
            if rar_failure is not None:
                raise ArchiveReadError(f"Could not read RAR entry with any backend: {name}") from rar_failure
            raise exc

    def _ensure_rar_backend(self) -> None:
        if rarfile is None:
            raise ArchiveDependencyMissing("rarfile is required for RAR/CBR archives.")
        self._configure_rarfile_tools()
        try:
            rarfile.tool_setup()
        except rarfile.RarCannotExec as exc:
            raise ArchiveDependencyMissing(self._backend_resolver.missing_message()) from exc

    def _configure_rarfile_tools(self) -> None:
        if rarfile is None:
            return
        seven_zip = self._backend_resolver.seven_zip()
        if seven_zip is not None:
            if hasattr(rarfile, "SEVENZIP_TOOL"):
                rarfile.SEVENZIP_TOOL = seven_zip.executable
            if hasattr(rarfile, "SEVENZIP2_TOOL"):
                rarfile.SEVENZIP2_TOOL = seven_zip.executable
        unar = self._backend_resolver.unar()
        if unar is not None and hasattr(rarfile, "UNAR_TOOL"):
            rarfile.UNAR_TOOL = unar.executable
        bsdtar = self._backend_resolver.bsdtar()
        if bsdtar is not None and hasattr(rarfile, "BSDTAR_TOOL"):
            rarfile.BSDTAR_TOOL = bsdtar.executable

    def _read_rar_entry_external(self, source: _ArchiveSource, name: str, password: str | None) -> bytes:
        dependency_errors: list[str] = []
        read_errors: list[str] = []
        for reader in (
            self._read_rar_with_7zip,
            self._read_rar_with_unar,
            self._read_rar_with_bsdtar,
        ):
            try:
                return reader(source, name, password)
            except ArchiveDependencyMissing as exc:
                dependency_errors.append(str(exc))
                continue
            except ArchiveReadError as exc:
                read_errors.append(str(exc))
                continue
        if read_errors:
            raise ArchiveReadError("; ".join(read_errors))
        message = "; ".join(dependency_errors)
        raise ArchiveDependencyMissing(
            message or self._backend_resolver.missing_message(encrypted=password is not None)
        )

    def _read_rar_with_bsdtar(self, source: _ArchiveSource, name: str, password: str | None) -> bytes:
        if source.path is None:
            raise ArchiveDependencyMissing("bsdtar fallback requires a filesystem archive path.")
        if password is not None:
            raise ArchiveDependencyMissing("bsdtar password-protected RAR fallback is not enabled.")
        backend = self._backend_resolver.bsdtar()
        if backend is None:
            raise ArchiveDependencyMissing("bsdtar is not installed.")
        return _run_archive_stdout_command([backend.executable, "-xOf", str(source.path), name], name)

    def _read_rar_with_7zip(self, source: _ArchiveSource, name: str, password: str | None) -> bytes:
        if source.path is None:
            raise ArchiveDependencyMissing("7z fallback requires a filesystem archive path.")
        backend = self._backend_resolver.seven_zip()
        if backend is None:
            raise ArchiveDependencyMissing("7zz/7z is not installed.")
        command = [backend.executable, "x", "-so", "-y"]
        if password is not None:
            command.append(f"-p{password}")
        command.extend([str(source.path), name])
        return _run_archive_stdout_command(command, name, password=password)

    def _read_rar_with_unar(self, source: _ArchiveSource, name: str, password: str | None) -> bytes:
        if source.path is None:
            raise ArchiveDependencyMissing("unar fallback requires a filesystem archive path.")
        backend = self._backend_resolver.unar()
        if backend is None:
            raise ArchiveDependencyMissing("unar is not installed.")
        with TemporaryDirectory(prefix="joyread-rar-") as temp_dir:
            command = [backend.executable, "-quiet", "-force-overwrite", "-output-directory", temp_dir]
            if password is not None:
                command.extend(["-password", password])
            command.extend([str(source.path), name])
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                if password is not None and _looks_like_password_error_text(stderr):
                    raise ArchivePasswordRejected(f"Password rejected for RAR entry: {name}")
                raise ArchiveReadError(f"unar could not extract {name}: {stderr or result.returncode}")
            extracted = Path(temp_dir) / name
            if not extracted.exists():
                matches = list(Path(temp_dir).rglob(PurePosixPath(name).name))
                extracted = matches[0] if matches else extracted
            if not extracted.exists() or not extracted.is_file():
                raise ArchiveReadError(f"unar did not produce expected entry: {name}")
            return extracted.read_bytes()

    def _request_password(
        self,
        source: _ArchiveSource,
        password_provider: PasswordProvider | None,
        attempt: int = 1,
        reason: str | None = None,
    ) -> str:
        if password_provider is None:
            raise ArchivePasswordRequired(f"Password required for archive: {source.display_name}")

        password = password_provider(
            ArchivePasswordRequest(
                archive_path=source.display_name,
                archive_format=source.suffix.lstrip(".").upper(),
                attempt=attempt,
                reason=reason,
            )
        )
        if password is None:
            raise ArchivePasswordRequired(f"Password request cancelled for archive: {source.display_name}")
        return password


def _safe_entry_name(name: str) -> str | None:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts:
        return None
    if any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _is_metadata_entry(name: str) -> bool:
    parts = PurePosixPath(name).parts
    if not parts:
        return True
    if parts[0] == "__MACOSX":
        return True
    return any(part == ".DS_Store" or part.startswith("._") for part in parts)


def _transparent_single_root_prefix(entries: Iterable[_ArchiveEntry]) -> str | None:
    roots: set[str] = set()
    for entry in entries:
        safe_name = _safe_entry_name(entry.name)
        if safe_name is None or _is_metadata_entry(safe_name):
            continue
        parts = PurePosixPath(safe_name).parts
        if len(parts) <= 1:
            return None
        roots.add(parts[0])
        if len(roots) > 1:
            return None
    if len(roots) == 1:
        return next(iter(roots))
    return None


def _strip_transparent_prefix(name: str, prefix: str | None) -> str | None:
    if prefix is None:
        return name
    parts = PurePosixPath(name).parts
    if len(parts) <= 1 or parts[0] != prefix:
        return name
    stripped = PurePosixPath(*parts[1:]).as_posix()
    return stripped or None


def _entry_depth(name: str) -> int:
    return len(PurePosixPath(name).parts)


def _parent_group(name: str) -> str:
    parent = PurePosixPath(name).parent
    if parent == PurePosixPath("."):
        return ""
    return parent.as_posix()


def _sort_group(pages: list[_PageRecord]) -> list[_PageRecord]:
    if not pages:
        return []
    stems = [PurePosixPath(page.display_path).stem for page in pages]
    if all(stem.isdigit() for stem in stems):
        return sorted(pages, key=lambda page: (int(PurePosixPath(page.display_path).stem), page.display_path.lower()))
    return sorted(pages, key=lambda page: _natural_key(PurePosixPath(page.display_path).name))


def _natural_key(value: str) -> tuple[object, ...]:
    parts: list[tuple[int, object]] = []
    for part in _NATURAL_PART_RE.split(value.lower()):
        if part.isdigit():
            parts.append((0, int(part)))
        elif part:
            parts.append((1, part))
    return tuple(parts)


def _coerce_depth(value: object) -> int:
    try:
        depth = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 2
    return max(1, min(5, depth))


def _looks_like_password_error(exc: Exception) -> bool:
    return _looks_like_password_error_text(str(exc))


def _looks_like_password_error_text(text: str) -> bool:
    normalized = text.lower()
    direct_markers = (
        "password",
        "encrypted",
        "bad decrypt",
        "wrong pass",
        "incorrect pass",
    )
    if any(marker in normalized for marker in direct_markers):
        return True
    if "data error" in normalized and ("encrypted" in normalized or "wrong" in normalized):
        return True
    if "crc failed" in normalized and ("password" in normalized or "encrypted" in normalized):
        return True
    return False


def _archive_page_from_bytes(index: int, record: _PageRecord, payload: bytes) -> ArchivePage | None:
    dimensions = _dimensions_from_bytes(payload)
    if dimensions is None:
        return None
    return ArchivePage(
        index=index,
        image_bytes=payload,
        dimensions=dimensions,
        display_path=record.display_path,
    )


def _dimensions_from_bytes(payload: bytes) -> tuple[int, int] | None:
    try:
        with Image.open(BytesIO(payload)) as image:
            return (int(image.width), int(image.height))
    except (OSError, UnidentifiedImageError):
        return None


def _run_archive_stdout_command(command: Sequence[str], entry_name: str, *, password: str | None = None) -> bytes:
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        raise ArchiveDependencyMissing(f"Could not start archive backend: {command[0]}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        if password is not None and _looks_like_password_error_text(stderr):
            raise ArchivePasswordRejected(f"Password rejected for archive entry: {entry_name}")
        raise ArchiveReadError(f"{command[0]} could not extract {entry_name}: {stderr or result.returncode}")
    if not result.stdout:
        raise ArchiveReadError(f"{command[0]} returned no data for {entry_name}")
    return result.stdout


def _validation_code_for_error(error: ArchiveError) -> ArchiveValidationCode:
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
