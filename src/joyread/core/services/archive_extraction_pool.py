"""Disk-backed LRU pool for extracted bytes of slow archive formats.

This pool persists across application launches. Zip-family archives support
cheap random access from their containers and do not benefit from caching
extracted bytes, so the pool is used exclusively for 7z and RAR families,
where every read otherwise re-runs the decompressor.

Layout: each source archive is built in a resumable
``<book_key>.partial.zip`` bundle, then atomically published as
``<book_key>.zip`` after its ready manifest is written. ``book_key`` is a
stable hash of the caller-supplied document cache key. Page entries live
inside the zip under their original entry name (sanitised to remove path
separators).

LRU operates at the *book* level — the filesystem ``mtime`` of each bundle is
the LRU clock. ``get`` touches the bundle to refresh its position. Chunk writes
append missing entries to one resumable partial bundle; the ready manifest is
written last and the bundle is then atomically published.

Forced ``ZIP_STORED`` keeps the bundle from re-compressing already-compressed
JPEG/PNG bytes — the zip is purely a grouping container.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
from threading import RLock
from typing import Protocol
from uuid import uuid4
from zipfile import BadZipFile, ZIP_STORED, ZipFile

from joyread.core.diagnostics import cache_identity_kind, reader_perf_event


logger = logging.getLogger(__name__)
_MANIFEST_SCHEMA_VERSION = 3


class ArchiveExtractionCache(Protocol):
    @property
    def directory(self) -> Path | None: ...

    @property
    def max_bytes(self) -> int: ...

    @property
    def current_bytes(self) -> int: ...

    @property
    def active_lease_count(self) -> int: ...

    def get(self, document_cache_key: str, entry_name: str) -> bytes | None: ...

    def put(self, document_cache_key: str, entry_name: str, data: bytes) -> None: ...

    def put_many(self, document_cache_key: str, payloads: Mapping[str, bytes]) -> None: ...

    def contains(self, document_cache_key: str, entry_name: str) -> bool: ...

    def get_many(self, document_cache_key: str, entry_names: tuple[str, ...]) -> dict[str, bytes]: ...

    def is_complete(self, document_cache_key: str, page_count: int, signature: str) -> bool: ...

    def mark_complete(self, document_cache_key: str, page_count: int, signature: str) -> None: ...

    def purge(self, document_cache_key: str) -> None: ...

    def promote(self, source_cache_key: str, target_cache_key: str) -> bool: ...

    def acquire(self, document_cache_key: str) -> None: ...

    def release(self, document_cache_key: str) -> None: ...

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

    _ZIP_SUFFIX = ".zip"
    _MANIFEST_ENTRY = "__joyread_ready_manifest__.json"
    _SCHEMA_MARKER = ".joyread-archive-cache-schema"
    _SCHEMA_VERSION = "3"

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
        self._active: dict[str, int] = {}
        self._strict_eviction_pending = False

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

    @property
    def active_lease_count(self) -> int:
        self._ensure_reconciled()
        with self._lock:
            return sum(self._active.values())

    def get(self, document_cache_key: str, entry_name: str) -> bytes | None:
        """Return cached bytes for ``(document_cache_key, entry_name)``."""

        self._ensure_reconciled()
        if self._directory is None or not entry_name:
            return None
        book_key = self._book_key_for(document_cache_key)
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
            except (BadZipFile, OSError) as exc:
                # Corrupted bundle (interrupted write, disk error). Drop it so
                # the next put starts clean.
                logger.warning(
                    "Dropping corrupt archive bundle %s for entry %r: %s",
                    entry.path,
                    entry_name,
                    exc,
                )
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

    def contains(self, document_cache_key: str, entry_name: str) -> bool:
        return self.get(document_cache_key, entry_name) is not None

    def get_many(self, document_cache_key: str, entry_names: tuple[str, ...]) -> dict[str, bytes]:
        payloads: dict[str, bytes] = {}
        for entry_name in entry_names:
            payload = self.get(document_cache_key, entry_name)
            if payload is not None:
                payloads[entry_name] = payload
        return payloads

    def is_complete(self, document_cache_key: str, page_count: int, signature: str) -> bool:
        payload = self.get(document_cache_key, self._MANIFEST_ENTRY)
        return _manifest_matches(
            payload,
            page_count,
            signature,
            _identity_kind(document_cache_key),
        )

    def mark_complete(self, document_cache_key: str, page_count: int, signature: str) -> None:
        self.put(
            document_cache_key,
            self._MANIFEST_ENTRY,
            _manifest_bytes(page_count, signature, _identity_kind(document_cache_key)),
        )
        book_key = self._book_key_for(document_cache_key)
        if book_key is None or self._directory is None:
            return
        with self._lock:
            entry = self._index.get(book_key)
            if entry is None or not entry.path.name.endswith(".partial.zip"):
                return
            final_path = self._directory / f"{book_key}{self._ZIP_SUFFIX}"
            try:
                os.replace(entry.path, final_path)
                stat = final_path.stat()
            except OSError as exc:
                logger.warning("Archive cache publish failed for key=%s: %s", book_key, exc)
                return
            self._index[book_key] = _PoolEntry(final_path, stat.st_size, stat.st_mtime)

    def purge(self, document_cache_key: str) -> None:
        """Remove all extracted bytes for one immutable document identity."""

        self._ensure_reconciled()
        book_key = self._book_key_for(document_cache_key)
        if book_key is None:
            return
        with self._lock:
            self._forget_locked(book_key)
            if self._directory is None:
                return
            for suffix in (self._ZIP_SUFFIX, f".partial{self._ZIP_SUFFIX}"):
                candidate = self._directory / f"{book_key}{suffix}"
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass

    def promote(self, source_cache_key: str, target_cache_key: str) -> bool:
        """Atomically adopt an ephemeral bundle under a persistent identity."""

        self._ensure_reconciled()
        source_key = self._book_key_for(source_cache_key)
        target_key = self._book_key_for(target_cache_key)
        if source_key is None or target_key is None:
            return False
        if source_key == target_key:
            return True
        with self._lock:
            source = self._index.get(source_key)
            if source is None or not source.path.exists():
                self._move_active_locked(source_key, target_key)
                return True
            target = self._index.get(target_key)
            target_is_ready = bool(
                target is not None
                and target.path.exists()
                and _bundle_has_publishable_manifest(target.path, target_key, self._MANIFEST_ENTRY)
            )
            suffix = self._ZIP_SUFFIX if target_is_ready else f".partial{self._ZIP_SUFFIX}"
            assert self._directory is not None
            target_path = self._directory / f"{target_key}{suffix}"
            if _is_symlink(target_path) or (target is not None and _is_symlink(target.path)):
                logger.warning("Archive cache promotion rejected a symlink target")
                return False
            if target is None:
                try:
                    os.replace(source.path, target_path)
                    stat = target_path.stat()
                except OSError as exc:
                    logger.warning("Archive cache promotion failed: %s", exc)
                    return False
            else:
                tmp_path = target_path.with_suffix(f".{uuid4().hex}.tmp")
                try:
                    self._merge_bundles_locked(
                        target.path,
                        source.path,
                        tmp_path,
                        preserve_target_manifest=target_is_ready,
                    )
                    os.replace(tmp_path, target_path)
                    stat = target_path.stat()
                except (BadZipFile, OSError) as exc:
                    logger.warning("Archive cache promotion merge failed: %s", exc)
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return False
                if target.path != target_path:
                    try:
                        target.path.unlink(missing_ok=True)
                    except OSError:
                        pass
                try:
                    source.path.unlink(missing_ok=True)
                except OSError:
                    pass
            previous_source = self._index.pop(source_key, None)
            previous_target = self._index.pop(target_key, None)
            if previous_source is not None:
                self._current_bytes -= previous_source.size
            if previous_target is not None:
                self._current_bytes -= previous_target.size
            self._index[target_key] = _PoolEntry(target_path, stat.st_size, stat.st_mtime)
            self._current_bytes += stat.st_size
            self._move_active_locked(source_key, target_key)
            self._evict_locked(protect_key=target_key)
            return True

    def acquire(self, document_cache_key: str) -> None:
        """Pin one document identity while a live session owns its lease."""

        self._ensure_reconciled()
        book_key = self._book_key_for(document_cache_key)
        if book_key is None:
            return
        with self._lock:
            self._active[book_key] = self._active.get(book_key, 0) + 1

    def release(self, document_cache_key: str) -> None:
        """Release a live pin without evicting a normal soft-budget overage."""

        self._ensure_reconciled()
        book_key = self._book_key_for(document_cache_key)
        if book_key is None:
            return
        with self._lock:
            count = self._active.get(book_key, 0)
            if count <= 1:
                self._active.pop(book_key, None)
            else:
                self._active[book_key] = count - 1
            if self._strict_eviction_pending:
                self._evict_locked()
                self._strict_eviction_pending = self._current_bytes > self._max_bytes
            reader_perf_event(
                "archive.pool.release",
                strategy="zip_bundle",
                identity_kind=cache_identity_kind(document_cache_key),
                active=self._active.get(book_key, 0),
                current_bytes=self._current_bytes,
                budget_bytes=self._max_bytes,
                strict_pending=self._strict_eviction_pending,
            )

    def put(self, document_cache_key: str, entry_name: str, data: bytes) -> None:
        self.put_many(document_cache_key, {entry_name: data})

    def put_many(self, document_cache_key: str, payloads: Mapping[str, bytes]) -> None:
        """Append several entries to the unique resumable staging bundle."""

        if self._directory is None or not payloads:
            return
        self._ensure_reconciled()
        book_key = self._book_key_for(document_cache_key)
        if book_key is None:
            return
        safe_payloads = {
            self._safe_entry_name(entry_name): data
            for entry_name, data in payloads.items()
            if entry_name
        }
        if not safe_payloads:
            return
        with self._lock:
            existing = self._index.get(book_key)
            bundle_path = (
                existing.path
                if existing is not None
                else self._directory / f"{book_key}.partial{self._ZIP_SUFFIX}"
            )
            try:
                bundle_path.parent.mkdir(parents=True, exist_ok=True)
                self._append_bundle_locked(bundle_path, safe_payloads)
                stat = bundle_path.stat()
            except (BadZipFile, OSError) as exc:
                logger.warning(
                    "Archive bundle batch write failed for %s (%d entries): %s",
                    bundle_path,
                    len(safe_payloads),
                    exc,
                )
                return
            entry = _PoolEntry(bundle_path, stat.st_size, stat.st_mtime)
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
            self._strict_eviction_pending = self._current_bytes > self._max_bytes

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
            self._active.clear()
            self._current_bytes = 0
            self._strict_eviction_pending = False
        if self._directory is not None:
            try:
                for path in self._directory.iterdir():
                    if not path.is_symlink() and path.is_file():
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
            if self._directory is None:
                return
            self._directory.mkdir(parents=True, exist_ok=True)
            marker = self._directory / self._SCHEMA_MARKER
            try:
                marker_version = marker.read_text(encoding="ascii").strip() if marker.exists() else ""
            except OSError:
                marker_version = ""
            if marker_version != self._SCHEMA_VERSION:
                # Cache storage keys and manifests changed in v3 to encode
                # managed, external-content, and ephemeral identity kinds.
                for path in self._directory.iterdir():
                    if path == marker or path.is_symlink() or not path.is_file():
                        continue
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                try:
                    marker.write_text(self._SCHEMA_VERSION, encoding="ascii")
                except OSError:
                    pass
            scanned: list[tuple[str, _PoolEntry]] = []
            try:
                entries = list(self._directory.iterdir())
            except OSError as exc:
                logger.warning(
                    "Archive pool reconcile failed to list %s: %s", self._directory, exc
                )
                return
            for path in entries:
                if not path.is_file():
                    continue
                if path.name == self._SCHEMA_MARKER:
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
                if path.name.startswith("e-"):
                    # Ephemeral leases are process-lifetime working data. A
                    # surviving file means the process ended before close().
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                manifest_payload: bytes | None = None
                try:
                    with ZipFile(path, "r") as archive:
                        archive.infolist()
                        if path.name.endswith(".partial.zip"):
                            try:
                                manifest_payload = archive.read(self._MANIFEST_ENTRY)
                            except KeyError:
                                pass
                except (BadZipFile, OSError):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                book_key = (
                    path.name[: -len(".partial.zip")]
                    if path.name.endswith(".partial.zip")
                    else path.stem
                )
                if (
                    path.name.endswith(".partial.zip")
                    and _ready_manifest_is_publishable(manifest_payload, book_key)
                ):
                    final_path = self._directory / f"{book_key}{self._ZIP_SUFFIX}"
                    if not final_path.exists():
                        try:
                            os.replace(path, final_path)
                            path = final_path
                        except OSError:
                            pass
                try:
                    stat = path.stat()
                except OSError:
                    continue
                previous = next((item for item in scanned if item[0] == book_key), None)
                if previous is not None:
                    # A crash between publish and cleanup can leave both. Keep
                    # the newest snapshot and discard the stale sibling.
                    if previous[1].mtime >= stat.st_mtime:
                        path.unlink(missing_ok=True)
                        continue
                    previous[1].path.unlink(missing_ok=True)
                    scanned.remove(previous)
                scanned.append((book_key, _PoolEntry(path, stat.st_size, stat.st_mtime)))
            scanned.sort(key=lambda item: item[1].mtime)
            for book_key, entry in scanned:
                self._index[book_key] = entry
                self._current_bytes += entry.size
            newest_key = scanned[-1][0] if scanned else None
            self._evict_locked(protect_key=newest_key)

    def _book_key_for(self, document_cache_key: str) -> str | None:
        key = str(document_cache_key or "").strip()
        if not key:
            return None
        return archive_cache_storage_key(key)

    @staticmethod
    def _safe_entry_name(entry_name: str) -> str:
        # Zip allows ``/`` but disallows backslashes and parent traversal in
        # well-behaved tools. Normalize so the bundle never carries
        # platform-specific separators.
        return PurePosixPath(entry_name.replace("\\", "/")).as_posix().lstrip("/")

    def _append_bundle_locked(
        self,
        bundle_path: Path,
        payloads: Mapping[str, bytes],
    ) -> None:
        mode = "a" if bundle_path.exists() else "w"
        existing_names: set[str] = set()
        if bundle_path.exists():
            try:
                with ZipFile(bundle_path, "r") as archive:
                    existing_names = set(archive.namelist())
            except (BadZipFile, OSError):
                bundle_path.unlink(missing_ok=True)
                mode = "w"
        safe_payloads = dict(payloads)
        if (
            self._MANIFEST_ENTRY in safe_payloads
            and self._MANIFEST_ENTRY in existing_names
        ):
            self._rewrite_manifest_locked(bundle_path, safe_payloads)
            return
        with ZipFile(bundle_path, mode, compression=ZIP_STORED) as archive:
            for name, payload in safe_payloads.items():
                if name not in existing_names:
                    archive.writestr(name, payload)

    def _rewrite_manifest_locked(self, bundle_path: Path, payloads: Mapping[str, bytes]) -> None:
        tmp_path = bundle_path.with_suffix(f".{uuid4().hex}.tmp")
        try:
            with ZipFile(bundle_path, "r") as source, ZipFile(
                tmp_path,
                "w",
                compression=ZIP_STORED,
            ) as output:
                for info in source.infolist():
                    if info.filename == self._MANIFEST_ENTRY:
                        continue
                    with source.open(info, "r") as reader, output.open(info.filename, "w") as writer:
                        shutil.copyfileobj(reader, writer, length=1024 * 1024)
                for name, payload in payloads.items():
                    output.writestr(name, payload)
            os.replace(tmp_path, bundle_path)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    @classmethod
    def _merge_bundles_locked(
        cls,
        target_path: Path,
        source_path: Path,
        output_path: Path,
        *,
        preserve_target_manifest: bool,
    ) -> None:
        with ZipFile(target_path, "r") as target, ZipFile(source_path, "r") as source:
            target_names = set(target.namelist())
            with ZipFile(output_path, "w", compression=ZIP_STORED) as output:
                for archive in (target, source):
                    for info in archive.infolist():
                        if info.filename == cls._MANIFEST_ENTRY and (
                            archive is source or not preserve_target_manifest
                        ):
                            continue
                        if archive is source and info.filename in target_names:
                            continue
                        with archive.open(info, "r") as reader, output.open(info.filename, "w") as writer:
                            shutil.copyfileobj(reader, writer, length=1024 * 1024)

    def _forget_locked(self, book_key: str) -> None:
        entry = self._index.pop(book_key, None)
        if entry is None:
            return
        self._current_bytes -= entry.size
        try:
            entry.path.unlink(missing_ok=True)
        except OSError:
            pass

    def _move_active_locked(self, source_key: str, target_key: str) -> None:
        count = self._active.pop(source_key, 0)
        if count:
            self._active[target_key] = self._active.get(target_key, 0) + count

    def _evict_locked(self, *, protect_key: str | None = None) -> None:
        before = self._current_bytes
        evicted = 0
        while self._current_bytes > self._max_bytes and self._index:
            oldest_key = next(
                (
                    key
                    for key in self._index
                    if key != protect_key and self._active.get(key, 0) == 0
                ),
                None,
            )
            if oldest_key is None:
                break
            self._forget_locked(oldest_key)
            evicted += 1
        if evicted or self._current_bytes > self._max_bytes:
            reader_perf_event(
                "archive.pool.evict",
                strategy="zip_bundle",
                before_bytes=before,
                after_bytes=self._current_bytes,
                budget_bytes=self._max_bytes,
                evicted_bundles=evicted,
                protected=protect_key is not None,
            )


class HiddenImageExtractionPool:
    """Disk LRU that stores extracted page bytes as hidden app cache files.

    The payload files deliberately use opaque hashed names and a non-image
    extension under a hidden folder. This is only cache hygiene: it discourages
    casual media indexing and accidental opening by other software, but it is
    not encryption or access control.
    """

    _PAGE_SUFFIX = ".jrcache"
    _MANIFEST_ENTRY = "__joyread_ready_manifest__.json"
    _SCHEMA_MARKER = ".joyread-archive-cache-schema"
    _SCHEMA_VERSION = "3"

    def __init__(self, directory: Path | None, max_bytes: int) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        self._directory = Path(directory) if directory is not None else None
        self._max_bytes = int(max_bytes)
        self._index: "OrderedDict[tuple[str, str], _PoolEntry]" = OrderedDict()
        self._current_bytes = 0
        self._lock = RLock()
        self._reconciled = False
        self._active: dict[str, int] = {}
        self._strict_eviction_pending = False

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

    @property
    def active_lease_count(self) -> int:
        self._ensure_reconciled()
        with self._lock:
            return sum(self._active.values())

    def get(self, document_cache_key: str, entry_name: str) -> bytes | None:
        self._ensure_reconciled()
        if self._directory is None or not entry_name:
            return None
        book_key = _book_key_for_document_cache_key(document_cache_key)
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
            except OSError as exc:
                logger.warning(
                    "Hidden image cache read failed for %s: %s", entry.path, exc
                )
                self._forget_locked((book_key, entry_key))
                return None
            refreshed = _PoolEntry(entry.path, stat.st_size, stat.st_mtime)
            self._index.pop((book_key, entry_key), None)
            self._index[(book_key, entry_key)] = refreshed
            self._current_bytes += refreshed.size - entry.size
            return payload

    def contains(self, document_cache_key: str, entry_name: str) -> bool:
        return self.get(document_cache_key, entry_name) is not None

    def get_many(self, document_cache_key: str, entry_names: tuple[str, ...]) -> dict[str, bytes]:
        payloads: dict[str, bytes] = {}
        for entry_name in entry_names:
            payload = self.get(document_cache_key, entry_name)
            if payload is not None:
                payloads[entry_name] = payload
        return payloads

    def is_complete(self, document_cache_key: str, page_count: int, signature: str) -> bool:
        payload = self.get(document_cache_key, self._MANIFEST_ENTRY)
        return _manifest_matches(
            payload,
            page_count,
            signature,
            _identity_kind(document_cache_key),
        )

    def mark_complete(self, document_cache_key: str, page_count: int, signature: str) -> None:
        self.put(
            document_cache_key,
            self._MANIFEST_ENTRY,
            _manifest_bytes(page_count, signature, _identity_kind(document_cache_key)),
        )

    def purge(self, document_cache_key: str) -> None:
        """Remove all hidden-cache entries for one immutable document identity."""

        self._ensure_reconciled()
        book_key = _book_key_for_document_cache_key(document_cache_key)
        if book_key is None:
            return
        with self._lock:
            self._forget_book_locked(book_key)

    def promote(self, source_cache_key: str, target_cache_key: str) -> bool:
        self._ensure_reconciled()
        source_key = _book_key_for_document_cache_key(source_cache_key)
        target_key = _book_key_for_document_cache_key(target_cache_key)
        if source_key is None or target_key is None:
            return False
        if source_key == target_key:
            return True
        with self._lock:
            if self._directory is None:
                return False
            source_dir = self._directory / source_key
            target_dir = self._directory / target_key
            if not source_dir.is_dir() or source_dir.is_symlink():
                self._move_active_locked(source_key, target_key)
                return True
            if _is_symlink(target_dir):
                logger.warning("Hidden archive cache promotion rejected a symlink target")
                return False
            try:
                if target_dir.exists():
                    target_dir.mkdir(parents=True, exist_ok=True)
                    for path in source_dir.iterdir():
                        if path.is_file() and not path.is_symlink():
                            destination = target_dir / path.name
                            if not destination.exists():
                                os.replace(path, destination)
                    shutil.rmtree(source_dir, ignore_errors=True)
                else:
                    os.replace(source_dir, target_dir)
            except OSError as exc:
                logger.warning("Hidden archive cache promotion failed: %s", exc)
                return False

            for key in tuple(self._index):
                if key[0] in {source_key, target_key}:
                    entry = self._index.pop(key)
                    self._current_bytes -= entry.size
            try:
                paths = tuple(target_dir.iterdir())
            except OSError:
                paths = ()
            refreshed: list[tuple[tuple[str, str], _PoolEntry]] = []
            for path in paths:
                if path.suffix != self._PAGE_SUFFIX or path.is_symlink() or not path.is_file():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                refreshed.append(((target_key, path.stem), _PoolEntry(path, stat.st_size, stat.st_mtime)))
            refreshed.sort(key=lambda item: item[1].mtime)
            for key, entry in refreshed:
                self._index[key] = entry
                self._current_bytes += entry.size
            self._move_active_locked(source_key, target_key)
            if refreshed:
                self._evict_locked(protect_key=refreshed[-1][0])
            return True

    def acquire(self, document_cache_key: str) -> None:
        self._ensure_reconciled()
        book_key = _book_key_for_document_cache_key(document_cache_key)
        if book_key is None:
            return
        with self._lock:
            self._active[book_key] = self._active.get(book_key, 0) + 1

    def release(self, document_cache_key: str) -> None:
        self._ensure_reconciled()
        book_key = _book_key_for_document_cache_key(document_cache_key)
        if book_key is None:
            return
        with self._lock:
            count = self._active.get(book_key, 0)
            if count <= 1:
                self._active.pop(book_key, None)
            else:
                self._active[book_key] = count - 1
            if self._strict_eviction_pending:
                self._evict_locked()
                self._strict_eviction_pending = self._current_bytes > self._max_bytes
            reader_perf_event(
                "archive.pool.release",
                strategy="hidden_pages",
                identity_kind=cache_identity_kind(document_cache_key),
                active=self._active.get(book_key, 0),
                current_bytes=self._current_bytes,
                budget_bytes=self._max_bytes,
                strict_pending=self._strict_eviction_pending,
            )

    def put(self, document_cache_key: str, entry_name: str, data: bytes) -> None:
        self.put_many(document_cache_key, {entry_name: data})

    def put_many(self, document_cache_key: str, payloads: Mapping[str, bytes]) -> None:
        if self._directory is None or not payloads:
            return
        self._ensure_reconciled()
        book_key = _book_key_for_document_cache_key(document_cache_key)
        if book_key is None:
            return
        written: list[tuple[tuple[str, str], _PoolEntry]] = []
        for entry_name, data in payloads.items():
            if not entry_name:
                continue
            entry_key = self._entry_key_for(entry_name)
            final_path = self._entry_path(book_key, entry_key)
            tmp_path = final_path.with_suffix(f".{uuid4().hex}.tmp")
            try:
                final_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path.write_bytes(data)
                os.replace(tmp_path, final_path)
                stat = final_path.stat()
            except OSError as exc:
                logger.warning(
                    "Hidden image cache write failed for %s: %s", final_path, exc
                )
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
            self._strict_eviction_pending = self._current_bytes > self._max_bytes

    def clear(self) -> None:
        self._ensure_reconciled()
        with self._lock:
            for entry in list(self._index.values()):
                try:
                    entry.path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._index.clear()
            self._active.clear()
            self._current_bytes = 0
            self._strict_eviction_pending = False
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
            if self._directory is None:
                return
            try:
                self._directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                return
            marker = self._directory / self._SCHEMA_MARKER
            try:
                marker_version = marker.read_text(encoding="ascii").strip() if marker.exists() else ""
            except OSError:
                marker_version = ""
            if marker_version != self._SCHEMA_VERSION:
                # v3 storage keys encode managed, external-content, and
                # ephemeral identity kinds.
                try:
                    children = tuple(self._directory.iterdir())
                except OSError:
                    children = ()
                for child in children:
                    if child == marker or child.is_symlink():
                        continue
                    try:
                        if child.is_dir():
                            shutil.rmtree(child)
                        elif child.is_file():
                            child.unlink(missing_ok=True)
                    except OSError:
                        pass
                try:
                    marker.write_text(self._SCHEMA_VERSION, encoding="ascii")
                except OSError:
                    pass
            scanned: list[tuple[tuple[str, str], _PoolEntry]] = []
            try:
                book_dirs = list(self._directory.iterdir())
            except OSError:
                return
            for book_dir in book_dirs:
                if book_dir.name == self._SCHEMA_MARKER:
                    continue
                if not book_dir.is_dir():
                    try:
                        book_dir.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                if book_dir.name.startswith("e-"):
                    try:
                        shutil.rmtree(book_dir)
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
            newest_key = scanned[-1][0] if scanned else None
            self._evict_locked(protect_key=newest_key)

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
        manifest_key = (key[0], self._entry_key_for(self._MANIFEST_ENTRY))
        if key != manifest_key:
            manifest = self._index.pop(manifest_key, None)
            if manifest is not None:
                self._current_bytes -= manifest.size
                try:
                    manifest.path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _forget_book_locked(self, book_key: str) -> None:
        """Evict one document as a unit, including its ready manifest."""

        for key in tuple(self._index):
            if key[0] == book_key:
                self._forget_locked(key)
        if self._directory is None:
            return
        book_dir = self._directory / book_key
        if book_dir.is_symlink() or not book_dir.is_dir():
            return
        try:
            shutil.rmtree(book_dir)
        except OSError:
            pass

    def _move_active_locked(self, source_key: str, target_key: str) -> None:
        count = self._active.pop(source_key, 0)
        if count:
            self._active[target_key] = self._active.get(target_key, 0) + count

    def _evict_locked(self, *, protect_key: tuple[str, str] | None = None) -> None:
        protect_book_key = protect_key[0] if protect_key is not None else None
        before = self._current_bytes
        evicted_books = 0
        while self._current_bytes > self._max_bytes and self._index:
            oldest_key = next(
                (
                    key
                    for key in self._index
                    if key[0] != protect_book_key and self._active.get(key[0], 0) == 0
                ),
                None,
            )
            if oldest_key is None:
                break
            self._forget_book_locked(oldest_key[0])
            evicted_books += 1
        if evicted_books or self._current_bytes > self._max_bytes:
            reader_perf_event(
                "archive.pool.evict",
                strategy="hidden_pages",
                before_bytes=before,
                after_bytes=self._current_bytes,
                budget_bytes=self._max_bytes,
                evicted_bundles=evicted_books,
                protected=protect_book_key is not None,
            )


def _book_key_for_document_cache_key(document_cache_key: str) -> str | None:
    key = str(document_cache_key or "").strip()
    if not key:
        return None
    return archive_cache_storage_key(key)


def _is_symlink(path: Path) -> bool:
    """Inspect the directory entry without following a cache-path symlink."""

    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except FileNotFoundError:
        return False
    except OSError:
        # An unreadable target is not safe to adopt during promotion.
        return True


def archive_cache_storage_key(document_cache_key: str) -> str:
    """Return the opaque v3 disk key while retaining the identity kind."""

    key = str(document_cache_key).strip()
    kind = _identity_kind(key)
    prefix = {
        "ephemeral": "e",
        "external": "x",
        "managed": "m",
    }[kind]
    digest = hashlib.sha256(f"archive-cache-v3:{key}".encode("utf-8")).hexdigest()
    return f"{prefix}-{digest}"


def _identity_kind(document_cache_key: str) -> str:
    key = str(document_cache_key).strip()
    if key.startswith("session:"):
        return "ephemeral"
    if key.startswith("external:sha256:"):
        return "external"
    return "managed"


def _manifest_bytes(page_count: int, signature: str, identity_kind: str) -> bytes:
    return json.dumps(
        {
            "schema": _MANIFEST_SCHEMA_VERSION,
            "page_count": max(0, int(page_count)),
            "signature": str(signature),
            "identity_kind": str(identity_kind),
            "build_state": "ready",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _manifest_matches(
    payload: bytes | None,
    page_count: int,
    signature: str,
    identity_kind: str,
) -> bool:
    if payload is None:
        return False
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return False
    return (
        manifest.get("schema") == _MANIFEST_SCHEMA_VERSION
        and manifest.get("page_count") == max(0, int(page_count))
        and manifest.get("signature") == str(signature)
        and manifest.get("identity_kind") == str(identity_kind)
        and manifest.get("build_state") == "ready"
    )


def _ready_manifest_is_publishable(payload: bytes | None, storage_key: str) -> bool:
    """Recognize a completed staging bundle without knowing current limits."""

    if payload is None:
        return False
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return False
    identity_kind = {
        "e": "ephemeral",
        "x": "external",
        "m": "managed",
    }.get(storage_key.partition("-")[0])
    return (
        identity_kind is not None
        and manifest.get("schema") == _MANIFEST_SCHEMA_VERSION
        and isinstance(manifest.get("page_count"), int)
        and manifest.get("page_count", -1) >= 0
        and isinstance(manifest.get("signature"), str)
        and bool(manifest.get("signature"))
        and manifest.get("identity_kind") == identity_kind
        and manifest.get("build_state") == "ready"
    )


def _bundle_has_publishable_manifest(
    bundle_path: Path,
    storage_key: str,
    manifest_entry: str,
) -> bool:
    try:
        with ZipFile(bundle_path, "r") as archive:
            payload = archive.read(manifest_entry)
    except (BadZipFile, KeyError, OSError):
        return False
    return _ready_manifest_is_publishable(payload, storage_key)
