"""7Z/CB7 listing and bounded entry reads."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from io import BytesIO

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
        try:
            factory = _BudgetedBytesFactory(limits, budget)
            with module.SevenZipFile(source.open_arg(), "r", password=password) as archive:
                archive.extract(targets=targets, factory=factory)
            payloads: dict[str, bytes] = {}
            for name in targets:
                product = factory.products.get(name)
                if product is None:
                    continue
                product.seek(0)
                payload = product.read()
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

    def __init__(self, limits: ArchiveOpenLimits, budget: ArchiveOperationBudget) -> None:
        self._limits = limits
        self._budget = budget
        self.products: dict[str, _BudgetedBytesIO] = {}

    def create(self, filename: str) -> "_BudgetedBytesIO":
        product = _BudgetedBytesIO(filename, self._limits, self._budget)
        self.products[filename] = product
        return product


class _BudgetedBytesIO:
    """Minimal ``py7zr.io.Py7zIO``-compatible, budget-aware in-memory sink."""

    def __init__(self, filename: str, limits: ArchiveOpenLimits, budget: ArchiveOperationBudget) -> None:
        self._filename = filename
        self._limits = limits
        self._budget = budget
        self._buffer = BytesIO()

    def write(self, data: bytes | bytearray) -> int:
        payload = bytes(data)
        projected_size = max(self.size(), self._buffer.tell() + len(payload))
        ensure_item_size(projected_size, self._limits.max_extracted_item_bytes, self._filename)
        self._budget.consume(len(payload), self._filename)
        return self._buffer.write(payload)

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

    def size(self) -> int:
        current = self._buffer.tell()
        self._buffer.seek(0, 2)
        size = self._buffer.tell()
        self._buffer.seek(current)
        return size
