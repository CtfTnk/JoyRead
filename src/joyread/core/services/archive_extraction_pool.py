"""Disk-backed LRU pool for extracted bytes of slow archive formats.

This pool persists across application launches. Zip-family archives support
cheap random access from their containers and do not benefit from caching
extracted bytes, so the pool is used exclusively for 7z and RAR families,
where every read otherwise re-runs the decompressor.

Layout: each source archive is mirrored by a single ``<book_key>.zip`` file
inside the configured directory, where ``book_key`` is a stable
``sha256(abspath:mtime_ns:size)`` of the source. Page entries live inside the
zip under their original entry name (sanitised to remove path separators).

LRU operates at the *book* level — the filesystem ``mtime`` of each
``<book_key>.zip`` file is the LRU clock. ``get`` touches the zip to refresh
its position, ``put`` rewrites the zip atomically (write to a sibling
``.tmp.zip`` and ``os.replace``) so the on-disk file is never in a
partially-written state.

Forced ``ZIP_STORED`` keeps the bundle from re-compressing already-compressed
JPEG/PNG bytes — the zip is purely a grouping container.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
from threading import RLock
from typing import Protocol
from zipfile import BadZipFile, ZIP_STORED, ZipFile

class ArchiveExtractionCache(Protocol):
    @property
    def directory(self) -> Path | None: ...

    @property
    def max_bytes(self) -> int: ...

    @property
    def current_bytes(self) -> int: ...

    def get(self, source_path: Path | str, entry_name: str) -> bytes | None: ...

    def put(self, source_path: Path | str, entry_name: str, data: bytes) -> None: ...

    def put_many(self, source_path: Path | str, payloads: Mapping[str, bytes]) -> None: ...

    def resize(self, max_bytes: int) -> None: ...

    def clear(self) -> None: ...


@dataclass(frozen=True)
class _PoolEntry:
    path: Path
    size: int
    mtime: float


class ArchiveExtractionPool:
    """Byte-budgeted disk LRU shared across all archive sources.

    Each source archive maps to a single ``<book_key>.zip`` file. Eviction
    happens at the book level: when the directory exceeds ``max_bytes``,
    whole bundles are deleted in mtime order until under budget.
    """

    # Stored in the zip metadata header for forward-compat sanity checks; not
    # used as the primary integrity guard (mtime+size on the source already
    # catches edits because the book_key changes).
    _ZIP_SUFFIX = ".zip"

    def __init__(self, directory: Path | None, max_bytes: int) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        self._directory = Path(directory) if directory is not None else None
        self._max_bytes = int(max_bytes)
        # Insertion order tracks mtime-ascending so the head of the dict is
        # always the least-recently-used book bundle.
        self._index: "OrderedDict[str, _PoolEntry]" = OrderedDict()
        self._current_bytes = 0
        self._lock = RLock()
        self._reconciled = False

    @property
    def directory(self) -> Path | None:
        return self._directory

    @property
    def max_bytes(self) -> int:
        with self._lock:
            return self._max_bytes

    @property
    def current_bytes(self) -> int:
        self._ensure_reconciled()
        with self._lock:
            return self._current_bytes

    def get(self, source_path: Path | str, entry_name: str) -> bytes | None:
        """Return cached bytes for ``(source_path, entry_name)`` or ``None``."""

        self._ensure_reconciled()
        if self._directory is None or not entry_name:
            return None
        source = Path(source_path)
        book_key = self._book_key_for(source)
        if book_key is None:
            return None
        with self._lock:
            entry = self._index.get(book_key)
            if entry is None or not entry.path.exists():
                if entry is not None:
                    self._forget_locked(book_key)
                return None
            try:
                with ZipFile(entry.path, "r") as archive:
                    payload = archive.read(self._safe_entry_name(entry_name))
            except KeyError:
                # The bundle exists for this source but the specific page has
                # not been cached yet; treat as a normal miss.
                return None
            except (BadZipFile, OSError):
                # Corrupted bundle (interrupted write, disk error). Drop it so
                # the next put starts clean.
                self._forget_locked(book_key)
                return None
            # Touch the bundle so it sits at the MRU end of the LRU queue.
            try:
                os.utime(entry.path, None)
                refreshed_mtime = entry.path.stat().st_mtime
                refreshed_size = entry.path.stat().st_size
            except OSError:
                refreshed_mtime = entry.mtime
                refreshed_size = entry.size
            refreshed = _PoolEntry(entry.path, refreshed_size, refreshed_mtime)
            self._index.pop(book_key, None)
            self._index[book_key] = refreshed
            # The bundle size could have shifted (rare but possible after a
            # concurrent rewrite); keep the running total accurate.
            self._current_bytes += refreshed.size - entry.size
            return payload

    def put(self, source_path: Path | str, entry_name: str, data: bytes) -> None:
        """Persist ``data`` under the bundle for ``source_path``.

        The bundle is rewritten atomically: existing entries are copied into
        a sibling ``.tmp.zip`` along with the new entry, then ``os.replace``
        swaps the file into place. This is more expensive than appending but
        avoids the corruption risk of ``ZipFile(mode='a')`` if the process is
        killed mid-write.
        """

        if self._directory is None or not entry_name:
            return
        self._ensure_reconciled()
        source = Path(source_path)
        book_key = self._book_key_for(source)
        if book_key is None:
            return
        safe_name = self._safe_entry_name(entry_name)
        bundle_path = self._directory / f"{book_key}{self._ZIP_SUFFIX}"
        tmp_path = bundle_path.with_suffix(f"{self._ZIP_SUFFIX}.tmp")
        try:
            bundle_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_bundle_locked(bundle_path, tmp_path, safe_name, data)
            stat = bundle_path.stat()
        except OSError:
            # Clean up an orphan tmp from a partial write so it does not
            # accumulate. The pool stays consistent with what is on disk.
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        entry = _PoolEntry(bundle_path, stat.st_size, stat.st_mtime)
        with self._lock:
            previous = self._index.pop(book_key, None)
            if previous is not None:
                self._current_bytes -= previous.size
            self._index[book_key] = entry
            self._current_bytes += entry.size
            self._evict_locked(protect_key=book_key)

    def put_many(self, source_path: Path | str, payloads: Mapping[str, bytes]) -> None:
        """Persist several entries with one zip rewrite.

        Reader cache warm-up extracts pages in descending chunks. Rewriting the
        zip bundle once per chunk avoids the pathological "rewrite the whole
        cache for every page" behavior while still keeping writes atomic.
        """

        if self._directory is None or not payloads:
            return
        self._ensure_reconciled()
        source = Path(source_path)
        book_key = self._book_key_for(source)
        if book_key is None:
            return
        safe_payloads = {
            self._safe_entry_name(entry_name): data
            for entry_name, data in payloads.items()
            if entry_name
        }
        if not safe_payloads:
            return
        bundle_path = self._directory / f"{book_key}{self._ZIP_SUFFIX}"
        tmp_path = bundle_path.with_suffix(f"{self._ZIP_SUFFIX}.tmp")
        try:
            bundle_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_bundle_many_locked(bundle_path, tmp_path, safe_payloads)
            stat = bundle_path.stat()
        except OSError:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        entry = _PoolEntry(bundle_path, stat.st_size, stat.st_mtime)
        with self._lock:
            previous = self._index.pop(book_key, None)
            if previous is not None:
                self._current_bytes -= previous.size
            self._index[book_key] = entry
            self._current_bytes += entry.size
            self._evict_locked(protect_key=book_key)

    def resize(self, max_bytes: int) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        self._ensure_reconciled()
        with self._lock:
            self._max_bytes = int(max_bytes)
            self._evict_locked()

    def clear(self) -> None:
        """Drop every cached bundle and remove the on-disk files."""

        self._ensure_reconciled()
        with self._lock:
            for entry in list(self._index.values()):
                try:
                    entry.path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._index.clear()
            self._current_bytes = 0
        if self._directory is not None:
            try:
                for path in self._directory.iterdir():
                    if path.is_file():
                        path.unlink(missing_ok=True)
            except OSError:
                pass

    def _ensure_reconciled(self) -> None:
        """Lazily index the cache directory on first use.

        Done lazily (not in ``__init__``) so application startup is not
        blocked by directory scans even when the cache contains many bundles;
        the very first archive read pays a single ``stat`` per file.
        """

        with self._lock:
            if self._reconciled:
                return
            self._reconciled = True
            if self._directory is None or not self._directory.exists():
                return
            scanned: list[tuple[str, _PoolEntry]] = []
            try:
                entries = list(self._directory.iterdir())
            except OSError:
                return
            for path in entries:
                if not path.is_file():
                    continue
                if path.name.endswith(".tmp.zip") or path.suffix == ".tmp":
                    # Orphan ``.tmp`` files come from an interrupted write —
                    # they were never indexed, so remove them on startup.
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                if path.suffix != self._ZIP_SUFFIX:
                    # Pre-existing files from the legacy per-page layout (or
                    # any other stray) are not part of the new index; remove
                    # them so the directory stays consistent with our LRU
                    # view.
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                book_key = path.stem
                scanned.append((book_key, _PoolEntry(path, stat.st_size, stat.st_mtime)))
            scanned.sort(key=lambda item: item[1].mtime)
            for book_key, entry in scanned:
                self._index[book_key] = entry
                self._current_bytes += entry.size
            self._evict_locked()

    def _book_key_for(self, source: Path) -> str | None:
        try:
            stat = source.stat()
        except OSError:
            return None
        # The book key fingerprints the source file itself (path + mtime +
        # size). Editing the source produces a different key, so stale
        # bundles age out of the LRU naturally.
        digest_source = f"{source.resolve()}:{stat.st_mtime_ns}:{stat.st_size}"
        return hashlib.sha256(digest_source.encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_entry_name(entry_name: str) -> str:
        # Zip allows ``/`` but disallows backslashes and parent traversal in
        # well-behaved tools. Normalize so the bundle never carries
        # platform-specific separators.
        return PurePosixPath(entry_name.replace("\\", "/")).as_posix().lstrip("/")

    def _write_bundle_locked(
        self,
        bundle_path: Path,
        tmp_path: Path,
        entry_name: str,
        data: bytes,
    ) -> None:
        """Rewrite ``bundle_path`` with all existing entries plus ``entry_name``.

        Using a temp file + ``os.replace`` is overkill for one page but
        cheap for the manga sizes we expect (tens to hundreds of small
        pages per book). The simplicity buys atomic correctness without a
        per-bundle lock file.
        """

        self._write_bundle_many_locked(bundle_path, tmp_path, {entry_name: data})

    def _write_bundle_many_locked(
        self,
        bundle_path: Path,
        tmp_path: Path,
        payloads: Mapping[str, bytes],
    ) -> None:
        existing: dict[str, bytes] = {}
        if bundle_path.exists():
            try:
                with ZipFile(bundle_path, "r") as archive:
                    for info in archive.infolist():
                        if info.filename in payloads:
                            continue
                        existing[info.filename] = archive.read(info)
            except (BadZipFile, OSError):
                # Corrupted bundle: start over. Subsequent reads for other
                # pages of this book will miss and re-extract, which is the
                # acceptable fallback.
                existing = {}
        try:
            with ZipFile(tmp_path, "w", compression=ZIP_STORED) as archive:
                for name, payload in existing.items():
                    archive.writestr(name, payload)
                for name, payload in payloads.items():
                    archive.writestr(name, payload)
        except OSError:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        os.replace(tmp_path, bundle_path)

    def _forget_locked(self, book_key: str) -> None:
        entry = self._index.pop(book_key, None)
        if entry is None:
            return
        self._current_bytes -= entry.size
        try:
            entry.path.unlink(missing_ok=True)
        except OSError:
            pass

    def _evict_locked(self, *, protect_key: str | None = None) -> None:
        while self._current_bytes > self._max_bytes and self._index:
            oldest_key = next(iter(self._index))
            if oldest_key == protect_key and len(self._index) == 1:
                # A single oversized bundle that was just written: keep it
                # (the caller asked for it) but evict it on the next put.
                return
            self._forget_locked(oldest_key)


class HiddenImageExtractionPool:
    """Disk LRU that stores extracted page bytes as hidden app cache files.

    The payload files deliberately use opaque hashed names and a non-image
    extension under a hidden folder. This is only cache hygiene: it discourages
    casual media indexing and accidental opening by other software, but it is
    not encryption or access control.
    """

    _PAGE_SUFFIX = ".jrcache"

    def __init__(self, directory: Path | None, max_bytes: int) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        self._directory = Path(directory) if directory is not None else None
        self._max_bytes = int(max_bytes)
        self._index: "OrderedDict[tuple[str, str], _PoolEntry]" = OrderedDict()
        self._current_bytes = 0
        self._lock = RLock()
        self._reconciled = False

    @property
    def directory(self) -> Path | None:
        return self._directory

    @property
    def max_bytes(self) -> int:
        with self._lock:
            return self._max_bytes

    @property
    def current_bytes(self) -> int:
        self._ensure_reconciled()
        with self._lock:
            return self._current_bytes

    def get(self, source_path: Path | str, entry_name: str) -> bytes | None:
        self._ensure_reconciled()
        if self._directory is None or not entry_name:
            return None
        source = Path(source_path)
        book_key = _book_key_for_source(source)
        if book_key is None:
            return None
        entry_key = self._entry_key_for(entry_name)
        with self._lock:
            entry = self._index.get((book_key, entry_key))
            if entry is None or not entry.path.exists():
                if entry is not None:
                    self._forget_locked((book_key, entry_key))
                return None
            try:
                payload = entry.path.read_bytes()
                os.utime(entry.path, None)
                stat = entry.path.stat()
            except OSError:
                self._forget_locked((book_key, entry_key))
                return None
            refreshed = _PoolEntry(entry.path, stat.st_size, stat.st_mtime)
            self._index.pop((book_key, entry_key), None)
            self._index[(book_key, entry_key)] = refreshed
            self._current_bytes += refreshed.size - entry.size
            return payload

    def put(self, source_path: Path | str, entry_name: str, data: bytes) -> None:
        self.put_many(source_path, {entry_name: data})

    def put_many(self, source_path: Path | str, payloads: Mapping[str, bytes]) -> None:
        if self._directory is None or not payloads:
            return
        self._ensure_reconciled()
        source = Path(source_path)
        book_key = _book_key_for_source(source)
        if book_key is None:
            return
        written: list[tuple[tuple[str, str], _PoolEntry]] = []
        for entry_name, data in payloads.items():
            if not entry_name:
                continue
            entry_key = self._entry_key_for(entry_name)
            final_path = self._entry_path(book_key, entry_key)
            tmp_path = final_path.with_suffix(f"{self._PAGE_SUFFIX}.tmp")
            try:
                final_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path.write_bytes(data)
                os.replace(tmp_path, final_path)
                stat = final_path.stat()
            except OSError:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            written.append(((book_key, entry_key), _PoolEntry(final_path, stat.st_size, stat.st_mtime)))
        if not written:
            return
        protect_key = written[-1][0]
        with self._lock:
            for key, entry in written:
                previous = self._index.pop(key, None)
                if previous is not None:
                    self._current_bytes -= previous.size
                self._index[key] = entry
                self._current_bytes += entry.size
            self._evict_locked(protect_key=protect_key)

    def resize(self, max_bytes: int) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        self._ensure_reconciled()
        with self._lock:
            self._max_bytes = int(max_bytes)
            self._evict_locked()

    def clear(self) -> None:
        self._ensure_reconciled()
        with self._lock:
            for entry in list(self._index.values()):
                try:
                    entry.path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._index.clear()
            self._current_bytes = 0
        if self._directory is not None:
            try:
                shutil.rmtree(self._directory, ignore_errors=True)
            except OSError:
                pass

    def _ensure_reconciled(self) -> None:
        with self._lock:
            if self._reconciled:
                return
            self._reconciled = True
            if self._directory is None or not self._directory.exists():
                return
            scanned: list[tuple[tuple[str, str], _PoolEntry]] = []
            try:
                book_dirs = list(self._directory.iterdir())
            except OSError:
                return
            for book_dir in book_dirs:
                if not book_dir.is_dir():
                    try:
                        book_dir.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                try:
                    entries = list(book_dir.iterdir())
                except OSError:
                    continue
                for path in entries:
                    if path.name.endswith(".tmp") or path.suffix != self._PAGE_SUFFIX:
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    scanned.append(((book_dir.name, path.stem), _PoolEntry(path, stat.st_size, stat.st_mtime)))
            scanned.sort(key=lambda item: item[1].mtime)
            for key, entry in scanned:
                self._index[key] = entry
                self._current_bytes += entry.size
            self._evict_locked()

    def _entry_path(self, book_key: str, entry_key: str) -> Path:
        assert self._directory is not None
        return self._directory / book_key / f"{entry_key}{self._PAGE_SUFFIX}"

    @staticmethod
    def _entry_key_for(entry_name: str) -> str:
        safe_name = PurePosixPath(entry_name.replace("\\", "/")).as_posix().lstrip("/")
        return hashlib.sha256(safe_name.encode("utf-8")).hexdigest()

    def _forget_locked(self, key: tuple[str, str]) -> None:
        entry = self._index.pop(key, None)
        if entry is None:
            return
        self._current_bytes -= entry.size
        try:
            entry.path.unlink(missing_ok=True)
        except OSError:
            pass

    def _evict_locked(self, *, protect_key: tuple[str, str] | None = None) -> None:
        while self._current_bytes > self._max_bytes and self._index:
            oldest_key = next(iter(self._index))
            if oldest_key == protect_key and len(self._index) == 1:
                return
            self._forget_locked(oldest_key)


def _book_key_for_source(source: Path) -> str | None:
    try:
        stat = source.stat()
    except OSError:
        return None
    digest_source = f"{source.resolve()}:{stat.st_mtime_ns}:{stat.st_size}"
    return hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
