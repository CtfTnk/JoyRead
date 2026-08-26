"""Repackage an archive into JoyRead's canonical container.

Why this exists: a `.cbr`/`.7z`, and especially a *nested* archive, is expensive
to read page by page. `ArchiveImageSession` can bulk-convert a slow archive into
the cache, but only when the source is a single path-backed file
(`session.py`) -- so a nested tree falls back to sequential warmup on every open,
forever. Converting once at import time is the only way those books stop paying
that cost.

Two properties make the result trustworthy, and both are tested:

**Deterministic.** The same source produces byte-identical output: fixed
timestamps, fixed ordering, fixed per-entry flags, and no host-dependent fields.
Without this `stored_hash` would be meaningless as an integrity baseline.

**Sanitized by construction.** The writer only ever writes pages the scanner
already accepted plus the metadata sidecars the inspection collected. Traversal
names, symlinks, `__MACOSX` noise, executables, and anything else non-image are
excluded because they never become pages in the first place -- not because a
second blocklist here repeats the check. A blocklist that has to be kept in sync
with the scanner's filter is a blocklist that will eventually drift out of sync.

The table of contents survives because nesting is rebuilt as directories: see
:func:`joyread.core.archive.tree.flatten_archive_tree_for_writing`.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol
import zipfile

from joyread.core.archive.inspection import ArchiveMetadataEntry
from joyread.core.archive.records import PageRecord


logger = logging.getLogger(__name__)

#: The container JoyRead writes today. A field on the writer rather than a
#: constant at the call site, so a future owned format is a new writer and not
#: an edit to every caller.
CBZ_SUFFIX = ".cbz"

#: Every entry gets this timestamp. Zip stores local time with no offset, so
#: real mtimes make output depend on the machine and the season; this is the
#: earliest value the format can represent.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

#: MS-DOS, chosen explicitly. ``ZipInfo`` otherwise records the *host* system,
#: which would make the same source hash differently on macOS and Windows.
_CREATE_SYSTEM_MSDOS = 0

#: ``rw-r--r--`` in the high half, as zip stores unix modes.
_ENTRY_MODE = 0o644 << 16


class CanonicalWriteCancelled(Exception):
    """The caller's ``is_cancelled`` returned true partway through a write."""


@dataclass(frozen=True)
class CanonicalWriteResult:
    """What a completed write produced."""

    page_count: int
    #: Entry names in write order. Chiefly a test surface: determinism is a
    #: property of these names as much as of the bytes.
    entry_names: tuple[str, ...]
    sidecar_names: tuple[str, ...]


class CanonicalWriter(Protocol):
    """A container JoyRead can write. Today CBZ; later an owned format."""

    suffix: str

    def write(
        self,
        destination: Path,
        placed_pages: list[tuple[str, PageRecord]],
        sidecars: tuple[ArchiveMetadataEntry, ...],
        *,
        read_page: Callable[[PageRecord], bytes],
        is_cancelled: Callable[[], bool] | None = None,
        on_page: Callable[[int, int], None] | None = None,
    ) -> CanonicalWriteResult: ...


class CbzWriter:
    """Write a plain, deterministic CBZ: a zip of images plus their sidecars."""

    suffix = CBZ_SUFFIX

    def write(
        self,
        destination: Path,
        placed_pages: list[tuple[str, PageRecord]],
        sidecars: tuple[ArchiveMetadataEntry, ...] = (),
        *,
        read_page: Callable[[PageRecord], bytes],
        is_cancelled: Callable[[], bool] | None = None,
        on_page: Callable[[int, int], None] | None = None,
    ) -> CanonicalWriteResult:
        total = len(placed_pages)
        used: set[str] = set()
        written: list[str] = []
        sidecar_names: list[str] = []

        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(destination, "w", allowZip64=True) as archive:
                for index, (prefix, page) in enumerate(placed_pages):
                    if is_cancelled is not None and is_cancelled():
                        raise CanonicalWriteCancelled()
                    name = _unique(f"{prefix}{PurePosixPath(page.display_path).name}", used)
                    # Stored, not deflated. Every page is already a compressed
                    # image format, so deflating spends CPU on both write and
                    # every later read for a gain measured in fractions of a
                    # percent -- and this container exists to make reads cheap.
                    _write_entry(archive, name, read_page(page), zipfile.ZIP_STORED)
                    written.append(name)
                    if on_page is not None:
                        on_page(index + 1, total)

                for sidecar in sidecars:
                    name = _unique(sidecar.name, used)
                    # Text, and small: deflate earns its keep here.
                    _write_entry(archive, name, sidecar.data, zipfile.ZIP_DEFLATED)
                    sidecar_names.append(name)
        except BaseException:
            # A partial artifact must never survive to be hashed and recorded.
            destination.unlink(missing_ok=True)
            raise

        return CanonicalWriteResult(
            page_count=len(written),
            entry_names=tuple(written),
            sidecar_names=tuple(sidecar_names),
        )


def _write_entry(archive: zipfile.ZipFile, name: str, data: bytes, method: int) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
    info.compress_type = method
    info.create_system = _CREATE_SYSTEM_MSDOS
    info.external_attr = _ENTRY_MODE
    archive.writestr(info, data)


def _unique(name: str, used: set[str]) -> str:
    """Keep every entry name distinct.

    Label disambiguation should already have separated siblings, so a clash here
    means the tree surprised us. Renaming rather than raising is deliberate: a
    duplicate name yields an archive whose entries shadow each other, while a
    raise would cost the user the whole import over one oddly-named file.
    """

    if name not in used:
        used.add(name)
        return name
    stem = PurePosixPath(name)
    for counter in range(1, 10_000):
        candidate = f"{stem.with_suffix('')}-{counter}{stem.suffix}"
        if candidate not in used:
            logger.warning(
                "Renamed a duplicate entry while writing a canonical archive",
                extra={
                    "event": "archive.canonical.duplicate_entry",
                    "category": "archive",
                    "status": "recovered",
                },
            )
            used.add(candidate)
            return candidate
    raise ValueError(f"Could not find a free entry name for {name!r}")


__all__ = [
    "CBZ_SUFFIX",
    "CanonicalWriteCancelled",
    "CanonicalWriteResult",
    "CanonicalWriter",
    "CbzWriter",
]
