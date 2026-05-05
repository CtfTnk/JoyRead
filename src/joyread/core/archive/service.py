"""Archive-backed image page discovery and access.

This module intentionally exposes archive data as a UI-free service. Thumbnail
generation, reader rendering, and import workflows should consume this API
instead of parsing archive formats directly.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
import re
from typing import Callable, Iterable
from zipfile import BadZipFile

from PIL import Image, UnidentifiedImageError

from joyread.core.archive.errors import (
    ArchiveCorruptError,
    ArchiveDependencyMissing,
    ArchiveEmptyError,
    ArchiveOpenError,
    ArchivePasswordRejected,
    ArchivePasswordRequired,
    ArchiveUnsupportedFormat,
)
from joyread.core.archive.models import ArchivePasswordRequest, PasswordProvider

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
    read_bytes: Callable[[], bytes]
    _dimensions: tuple[int, int] | None = None
    _dimensions_loaded: bool = False

    def dimensions(self) -> tuple[int, int] | None:
        if self._dimensions_loaded:
            return self._dimensions

        self._dimensions_loaded = True
        try:
            with Image.open(BytesIO(self.read_bytes())) as image:
                self._dimensions = (int(image.width), int(image.height))
        except (ArchiveCorruptError, ArchivePasswordRejected, ArchivePasswordRequired):
            raise
        except (OSError, UnidentifiedImageError):
            self._dimensions = None
        return self._dimensions


class ArchiveImageSession:
    """Bounded access to image pages discovered inside one archive."""

    def __init__(self, pages: Iterable[_PageRecord]) -> None:
        self._pages = list(pages)
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
        if not self.is_valid_index(index):
            return None
        return self._pages[index].read_bytes()

    def get_images(self, start: int, count: int) -> list[bytes | None]:
        if count <= 0:
            return []
        return [self.get_image(index) for index in range(start, start + count)]

    def get_dimensions(self, index: int) -> tuple[int, int] | None:
        if not self.is_valid_index(index):
            return None
        return self._pages[index].dimensions()

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
    """Create image sessions from supported comic archive files."""

    def open(
        self,
        archive_path: str | Path,
        password_provider: PasswordProvider | None = None,
        max_nested_depth: int = 5,
    ) -> ArchiveImageSession:
        path = Path(archive_path)
        suffix = path.suffix.lower()
        if suffix not in ARCHIVE_EXTENSIONS:
            raise ArchiveUnsupportedFormat(f"Unsupported archive format: {suffix or path.name}")
        if not path.exists():
            raise ArchiveOpenError(f"Archive does not exist: {path}")

        source = _ArchiveSource(label=path.name, suffix=suffix, path=path)
        pages = self._scan_archive(source, password_provider, depth=0, max_nested_depth=max_nested_depth)
        if not pages:
            raise ArchiveEmptyError(f"No supported image pages found in archive: {path}")
        return ArchiveImageSession(pages)

    def _scan_archive(
        self,
        source: _ArchiveSource,
        password_provider: PasswordProvider | None,
        depth: int,
        max_nested_depth: int,
    ) -> list[_PageRecord]:
        entries = self._list_entries(source, password_provider)
        root_group: list[_PageRecord] = []
        group_by_parent: OrderedDict[str, list[_PageRecord]] = OrderedDict({"": root_group})
        segments: list[tuple[str, list[_PageRecord]]] = []

        for entry in entries:
            safe_name = _safe_entry_name(entry.name)
            if safe_name is None:
                continue

            suffix = PurePosixPath(safe_name).suffix.lower()
            if suffix in IMAGE_EXTENSIONS:
                page = self._page_record(source, safe_name, entry.password)
                parent = _parent_group(safe_name)
                if parent == "":
                    root_group.append(page)
                    continue

                if parent not in group_by_parent:
                    group_by_parent[parent] = []
                    segments.append(("group", group_by_parent[parent]))
                group_by_parent[parent].append(page)
                continue

            if suffix in ARCHIVE_EXTENSIONS and depth < max_nested_depth:
                nested_data = self._read_entry(source, safe_name, entry.password)
                nested_source = _ArchiveSource(
                    label=f"{source.label}::{safe_name}",
                    suffix=suffix,
                    data=nested_data,
                )
                nested_pages = self._scan_archive(
                    nested_source,
                    password_provider,
                    depth=depth + 1,
                    max_nested_depth=max_nested_depth,
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

    def _page_record(self, source: _ArchiveSource, name: str, password: str | None) -> _PageRecord:
        display_path = f"{source.label}/{name}"
        return _PageRecord(
            display_path=display_path,
            read_bytes=lambda source=source, name=name, password=password: self._read_entry(source, name, password),
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
            return self._list_rar_entries(source, password_provider)
        raise ArchiveUnsupportedFormat(f"Unsupported nested archive format: {source.suffix}")

    def _read_entry(self, source: _ArchiveSource, name: str, password: str | None) -> bytes:
        if source.suffix in _ZIP_EXTENSIONS:
            return self._read_zip_entry(source, name, password)
        if source.suffix in _SEVEN_ZIP_EXTENSIONS:
            return self._read_7z_entry(source, name, password)
        if source.suffix in _RAR_EXTENSIONS:
            return self._read_rar_entry(source, name, password)
        raise ArchiveUnsupportedFormat(f"Unsupported archive format: {source.suffix}")

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
        if pyzipper is None:
            raise ArchiveDependencyMissing("pyzipper is required for ZIP/CBZ archives.")

        try:
            with pyzipper.AESZipFile(source.open_arg(), "r") as archive:
                pwd = password.encode("utf-8") if password is not None else None
                return archive.read(name, pwd=pwd)
        except RuntimeError as exc:
            if _looks_like_password_error(exc):
                raise ArchivePasswordRejected(f"Password rejected for ZIP entry: {name}") from exc
            raise ArchiveCorruptError(f"Could not read ZIP entry: {name}") from exc
        except (*_ZIP_BAD_FILE_ERRORS, KeyError) as exc:
            raise ArchiveCorruptError(f"Could not read ZIP entry: {name}") from exc

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
        if py7zr is None or BytesIOFactory is None:
            raise ArchiveDependencyMissing("py7zr is required for 7Z/CB7 archives.")

        try:
            factory = BytesIOFactory(_SEVEN_ZIP_READ_LIMIT)
            with py7zr.SevenZipFile(source.open_arg(), "r", password=password) as archive:
                archive.extract(targets=[name], factory=factory)
            product = factory.products.get(name)
            if product is None and factory.products:
                product = next(iter(factory.products.values()))
            if product is None:
                raise ArchiveCorruptError(f"7Z entry was not extracted: {name}")
            product.seek(0)
            return product.read()
        except py7zr.PasswordRequired as exc:
            raise ArchivePasswordRequired(f"Password required for 7Z entry: {name}") from exc
        except py7zr.DecompressionError as exc:
            if password is not None:
                raise ArchivePasswordRejected(f"Password rejected for 7Z entry: {name}") from exc
            raise ArchiveCorruptError(f"Could not decompress 7Z entry: {name}") from exc
        except py7zr.Bad7zFile as exc:
            raise ArchiveCorruptError(f"Could not read 7Z entry: {name}") from exc

    def _list_rar_entries(
        self,
        source: _ArchiveSource,
        password_provider: PasswordProvider | None,
    ) -> list[_ArchiveEntry]:
        self._ensure_rar_backend()

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
            raise ArchiveDependencyMissing("RAR/CBR requires an installed unar, unrar, bsdtar, or 7z backend.") from exc
        except rarfile.NeedFirstVolume as exc:
            raise ArchiveUnsupportedFormat("Multi-volume RAR archives are not supported yet.") from exc
        except rarfile.BadRarFile as exc:
            raise ArchiveCorruptError(f"Corrupt RAR archive: {source.display_name}") from exc
        except OSError as exc:
            raise ArchiveOpenError(f"Could not open RAR archive: {source.display_name}") from exc

    def _read_rar_entry(self, source: _ArchiveSource, name: str, password: str | None) -> bytes:
        self._ensure_rar_backend()

        try:
            with rarfile.RarFile(source.open_arg(), "r") as archive:
                return archive.read(name, pwd=password)
        except rarfile.RarCannotExec as exc:
            raise ArchiveDependencyMissing("RAR/CBR requires an installed unar, unrar, bsdtar, or 7z backend.") from exc
        except rarfile.PasswordRequired as exc:
            raise ArchivePasswordRequired(f"Password required for RAR entry: {name}") from exc
        except rarfile.RarWrongPassword as exc:
            raise ArchivePasswordRejected(f"Password rejected for RAR entry: {name}") from exc
        except rarfile.BadRarFile as exc:
            raise ArchiveCorruptError(f"Could not read RAR entry: {name}") from exc

    def _ensure_rar_backend(self) -> None:
        if rarfile is None:
            raise ArchiveDependencyMissing("rarfile is required for RAR/CBR archives.")
        try:
            rarfile.tool_setup()
        except rarfile.RarCannotExec as exc:
            raise ArchiveDependencyMissing("RAR/CBR requires an installed unar, unrar, bsdtar, or 7z backend.") from exc

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
    parts: list[object] = []
    for part in _NATURAL_PART_RE.split(value.lower()):
        if part.isdigit():
            parts.append(int(part))
        elif part:
            parts.append(part)
    return tuple(parts)


def _looks_like_password_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "password" in text or "encrypted" in text or "bad decrypt" in text
