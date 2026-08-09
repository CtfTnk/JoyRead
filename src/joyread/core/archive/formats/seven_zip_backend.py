"""7Z/CB7 listing and bounded entry reads."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from io import BytesIO
from time import perf_counter

from joyread.core.archive.errors import (
    ArchiveCorruptError,
    ArchiveDependencyMissing,
    ArchiveOpenError,
    ArchivePasswordRejected,
    ArchivePasswordRequired,
    ArchiveReadError,
    ArchiveResourceLimitError,
)
from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget, ensure_item_size
from joyread.core.archive.records import ArchiveContainerProbe, ArchiveEntry, ArchiveListing, ArchiveSource
from joyread.core.archive.scanner import ArchiveScanContext
from joyread.core.diagnostics import reader_perf_enabled, reader_perf_event


class SevenZipArchiveBackend:
    """7Z implementation with one password request per archive scan."""

    def __init__(
        self,
        module_getter: Callable[[], object | None],
        request_password: Callable[..., str],
    ) -> None:
        self._module_getter = module_getter
        self._request_password = request_password

    def probe_entries(self, source: ArchiveSource) -> ArchiveContainerProbe:
        """Inspect 7Z metadata without retrying or asking for a password."""

        module = self._module_getter()
        if module is None:
            raise ArchiveDependencyMissing("py7zr is required for 7Z/CB7 archives.")
        try:
            with module.SevenZipFile(source.open_arg(), "r") as archive:
                if archive.needs_password():
                    return ArchiveContainerProbe((), is_encrypted=True)
                return ArchiveContainerProbe(
                    tuple(
                        ArchiveEntry(info.filename, getattr(info, "uncompressed", None), None)
                        for info in archive.list()
                        if getattr(info, "is_file", False)
                    )
                )
        except module.PasswordRequired:
            # Header-encrypted 7z files cannot expose names until a password
            # is supplied. Import must reject them without starting UI input.
            return ArchiveContainerProbe((), is_encrypted=True)
        except module.Bad7zFile as exc:
            raise ArchiveCorruptError(f"Corrupt 7Z archive: {source.display_name}") from exc
        except OSError as exc:
            raise ArchiveOpenError(f"Could not open 7Z archive: {source.display_name}") from exc

    def list_entries(self, source: ArchiveSource, context: ArchiveScanContext) -> ArchiveListing:
        module = self._module_getter()
        if module is None:
            raise ArchiveDependencyMissing("py7zr is required for 7Z/CB7 archives.")

        password: str | None = None
        needs_password = False
        try:
            with module.SevenZipFile(source.open_arg(), "r") as archive:
                needs_password = archive.needs_password()
                if not needs_password:
                    return _listing_from_archive(archive, password=None)
        except module.PasswordRequired:
            needs_password = True
        except module.Bad7zFile as exc:
            raise ArchiveCorruptError(f"Corrupt 7Z archive: {source.display_name}") from exc
        except OSError as exc:
            raise ArchiveOpenError(f"Could not open 7Z archive: {source.display_name}") from exc

        if needs_password:
            password = self._request_password(source, context, reason="7z archive is encrypted")
        try:
            with module.SevenZipFile(source.open_arg(), "r", password=password) as archive:
                return _listing_from_archive(archive, password=password)
        except module.PasswordRequired as exc:
            raise ArchivePasswordRejected(
                f"Password rejected for 7Z archive: {source.display_name}",
                archive_path=source.display_name,
            ) from exc
        except module.Bad7zFile as exc:
            if password is not None:
                raise ArchivePasswordRejected(
                    f"Password rejected for 7Z archive: {source.display_name}",
                    archive_path=source.display_name,
                ) from exc
            raise ArchiveCorruptError(f"Corrupt 7Z archive: {source.display_name}") from exc
        except OSError as exc:
            raise ArchiveOpenError(f"Could not open 7Z archive: {source.display_name}") from exc

    def read_entries(
        self,
        source: ArchiveSource,
        entries: Sequence[tuple[str, str | None]],
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> dict[str, bytes]:
        module = self._module_getter()
        if module is None:
            raise ArchiveDependencyMissing("py7zr is required for 7Z/CB7 archives.")

        targets = [name for name, _password in entries]
        password = entries[0][1]
        perf_enabled = reader_perf_enabled()
        started = perf_counter() if perf_enabled else 0.0
        factory: _BudgetedBytesFactory | None = None
        try:
            factory = _BudgetedBytesFactory(
                limits,
                budget,
                track_buffered_bytes=perf_enabled,
            )
            with module.SevenZipFile(source.open_arg(), "r", password=password) as archive:
                archive.extract(targets=targets, factory=factory)
            payloads: dict[str, bytes] = {}
            for name in targets:
                product = factory.products.pop(name, None)
                if product is None:
                    continue
                payload = product.take_bytes()
                ensure_item_size(len(payload), limits.max_extracted_item_bytes, name)
                payloads[name] = payload
            missing = [name for name in targets if name not in payloads]
            if missing:
                raise ArchiveReadError(f"7Z entries were not extracted: {', '.join(missing[:3])}")
            return payloads
        except ArchiveResourceLimitError:
            raise
        except module.PasswordRequired as exc:
            raise ArchivePasswordRequired(
                f"Password required for 7Z archive: {source.display_name}",
                archive_path=source.display_name,
            ) from exc
        except module.DecompressionError as exc:
            if password is not None:
                raise ArchivePasswordRejected(
                    f"Password rejected for 7Z archive: {source.display_name}",
                    archive_path=source.display_name,
                ) from exc
            raise ArchiveReadError(f"Could not decompress 7Z entry: {targets[0]}") from exc
        except module.Bad7zFile as exc:
            raise ArchiveReadError(f"Could not read 7Z entry: {targets[0]}") from exc
        finally:
            if perf_enabled:
                reader_perf_event(
                    "archive.7z.extract",
                    targets=len(targets),
                    buffered_bytes=factory.peak_buffered_bytes if factory is not None else 0,
                    elapsed_ms=round((perf_counter() - started) * 1000.0, 3),
                )


def _listing_from_archive(archive, *, password: str | None) -> ArchiveListing:  # noqa: ANN001
    """Build a listing while the 7z handle can still expose solid metadata.

    ``archiveinfo()`` is the public API for path-backed archives. py7zr cannot
    currently use it for a BytesIO-backed nested archive because it calls
    ``os.stat``; its private predicate is the only metadata-only fallback in
    that case. Unknown capability is handled conservatively as sequential.
    """

    solid: bool | None = None
    try:
        solid = bool(archive.archiveinfo().solid)
    except (AssertionError, OSError, TypeError, ValueError):
        predicate = getattr(archive, "_is_solid", None)
        if callable(predicate):
            try:
                solid = bool(predicate())
            except (AttributeError, TypeError, ValueError):
                solid = None
    return ArchiveListing(
        tuple(
            ArchiveEntry(info.filename, getattr(info, "uncompressed", None), password)
            for info in archive.list()
            if getattr(info, "is_file", False)
        ),
        requires_sequential_warmup=solid is not False,
    )


class _BudgetedBytesFactory:
    """py7zr writer factory that rejects data before it becomes unbounded RAM."""

    def __init__(
        self,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
        *,
        track_buffered_bytes: bool = False,
    ) -> None:
        self._limits = limits
        self._budget = budget
        self._track_buffered_bytes = track_buffered_bytes
        self.products: dict[str, _BudgetedBytesIO] = {}
        self.buffered_bytes = 0
        self.peak_buffered_bytes = 0

    def create(self, filename: str) -> "_BudgetedBytesIO":
        on_write = self._record_write if self._track_buffered_bytes else None
        product = _BudgetedBytesIO(filename, self._limits, self._budget, on_write)
        self.products[filename] = product
        return product

    def _record_write(self, byte_count: int) -> None:
        self.buffered_bytes += max(0, int(byte_count))
        self.peak_buffered_bytes = max(self.peak_buffered_bytes, self.buffered_bytes)


class _BudgetedBytesIO:
    """Minimal ``py7zr.io.Py7zIO``-compatible, budget-aware in-memory sink."""

    def __init__(
        self,
        filename: str,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
        on_write: Callable[[int], None] | None = None,
    ) -> None:
        self._filename = filename
        self._limits = limits
        self._budget = budget
        self._buffer = BytesIO()
        self._on_write = on_write

    def write(self, data: bytes | bytearray) -> int:
        data_size = len(data)
        previous_size = self.size()
        write_offset = self._buffer.tell()
        projected_size = max(previous_size, write_offset + data_size)
        ensure_item_size(projected_size, self._limits.max_extracted_item_bytes, self._filename)
        self._budget.consume(data_size, self._filename)
        written = self._buffer.write(data)
        if self._on_write is not None:
            resulting_size = max(previous_size, write_offset + written)
            self._on_write(max(0, resulting_size - previous_size))
        return written

    def read(self, size: int | None = None) -> bytes:
        return self._buffer.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._buffer.seek(offset, whence)

    def flush(self) -> None:
        self._buffer.flush()

    def close(self) -> None:
        # py7zr calls this after each member. Keep the buffer available for
        # the backend to collect after ``extract()`` returns.
        return None

    def take_bytes(self) -> bytes:
        payload = self._buffer.getvalue()
        self._buffer.close()
        return payload

    def size(self) -> int:
        current = self._buffer.tell()
        self._buffer.seek(0, 2)
        size = self._buffer.tell()
        self._buffer.seek(current)
        return size
