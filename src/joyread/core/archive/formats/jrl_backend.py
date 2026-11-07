"""Pages of a sealed Hidden Space book, read straight out of its ``.jrl`` file.

A sealed book is a canonical CBZ encrypted in 64 KiB chunks (see
``joyread.core.vault.container``). Nothing here ever writes it out: the central
directory is read once, through a decrypting view, when the book opens, and
each page after that is one positional read of the chunks it covers. So a page
costs a decrypt of roughly its own size, two pages decrypt in parallel outside
the GIL, and no plaintext exists anywhere but in memory.

The backend is ``confidential``. The archive service keeps everything it lists
away from the extraction pool, spill directories and bulk extraction, and the
scanner will not recurse into a nested archive it contains.

Errors are mapped onto the archive taxonomy so the archive code handles them as
controlled failures. They name no entry: inside a sealed book even the page
names are the user's private content.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import struct
from pathlib import Path
import zipfile
import zlib

from joyread.core.archive.errors import (
    ArchiveCorruptError,
    ArchiveError,
    ArchiveOpenError,
    ArchiveReadError,
    ArchiveUnsupportedFormat,
    ArchiveVaultLocked,
)
from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget, ensure_item_size
from joyread.core.archive.records import ArchiveContainerProbe, ArchiveEntry, ArchiveListing, ArchiveSource
from joyread.core.archive.scanner import ArchiveScanContext
from joyread.core.vault import (
    ContentType,
    SealedFile,
    VaultError,
    VaultKeyMismatchError,
    VaultKeyring,
    VaultLockedError,
)


#: A zip local file header (PKWARE APPNOTE 4.3.7): signature, then fixed
#: fields ending in the name and extra-field lengths at indexes 10 and 11.
_LOCAL_HEADER = struct.Struct("<4s2B4HL2L2H")
_LOCAL_HEADER_MAGIC = b"PK\x03\x04"
_LOCAL_NAME_LENGTH = 10
_LOCAL_EXTRA_LENGTH = 11
_ENCRYPTED_FLAG = 0x1


@dataclass(frozen=True)
class _Member:
    header_offset: int
    compress_type: int
    compress_size: int
    file_size: int
    crc: int
    flag_bits: int


class SealedCbzIndex:
    """An open sealed CBZ: the decrypting reader plus its central directory.

    The session that listed it owns it (``ArchiveSource.handle``) and closes it
    once reads drain. Reads are thread-safe: :class:`SealedFile` reads are
    positional, and the only shared mutable state is a memo of data offsets,
    where a race at worst computes one twice.
    """

    def __init__(self, sealed: SealedFile, members: dict[str, _Member]) -> None:
        self._sealed = sealed
        self._members = members
        self._data_offsets: dict[str, int] = {}

    @classmethod
    def open(cls, keyring: VaultKeyring, path: Path) -> SealedCbzIndex:
        try:
            sealed = keyring.open_sealed(path)
        except VaultError as exc:
            raise _archive_error(exc) from exc
        except OSError as exc:
            raise ArchiveOpenError("Could not open a sealed book.") from exc
        try:
            if sealed.content_type is not ContentType.CBZ:
                raise ArchiveUnsupportedFormat("This sealed book does not hold an archive.")
            members = _read_central_directory(sealed)
        except BaseException:
            sealed.close()
            raise
        return cls(sealed, members)

    @property
    def entries(self) -> tuple[ArchiveEntry, ...]:
        return tuple(
            ArchiveEntry(name, member.file_size, None) for name, member in self._members.items()
        )

    def read_member(
        self,
        name: str,
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> bytes:
        member = self._members.get(name)
        if member is None:
            raise ArchiveReadError("A sealed book entry is missing.")
        if member.flag_bits & _ENCRYPTED_FLAG:
            # JoyRead never writes one. The seal is the encryption.
            raise ArchiveReadError("A sealed book entry is itself encrypted.")
        ensure_item_size(member.file_size, limits.max_extracted_item_bytes, name)
        try:
            start = self._data_offsets.get(name)
            if start is None:
                start = self._locate(member)
                self._data_offsets[name] = start
            raw = self._sealed.read(start, member.compress_size)
        except VaultError as exc:
            raise _archive_error(exc) from exc
        except OSError as exc:
            raise ArchiveReadError("Could not read a sealed book.") from exc
        if len(raw) != member.compress_size:
            raise ArchiveCorruptError("A sealed book entry is truncated.")
        data = _decompress(raw, member, limits)
        if zlib.crc32(data) & 0xFFFFFFFF != member.crc:
            raise ArchiveCorruptError("A sealed book entry failed its checksum.")
        budget.consume(len(data), name)
        return data

    def close(self) -> None:
        self._sealed.close()

    def _locate(self, member: _Member) -> int:
        """Where the entry's data starts: past its local header and fields.

        The local header repeats the name but may carry a different extra
        field than the central directory, so its own lengths are the ones
        that count.
        """

        header = self._sealed.read(member.header_offset, _LOCAL_HEADER.size)
        if len(header) != _LOCAL_HEADER.size:
            raise ArchiveCorruptError("A sealed book entry header is truncated.")
        fields = _LOCAL_HEADER.unpack(header)
        if fields[0] != _LOCAL_HEADER_MAGIC:
            raise ArchiveCorruptError("A sealed book entry header is malformed.")
        start = (
            member.header_offset
            + _LOCAL_HEADER.size
            + fields[_LOCAL_NAME_LENGTH]
            + fields[_LOCAL_EXTRA_LENGTH]
        )
        if start + member.compress_size > self._sealed.content_length:
            raise ArchiveCorruptError("A sealed book entry runs past the end of the book.")
        return start


class JrlArchiveBackend:
    """Format backend for ``.jrl`` files sealed to the unlocked vault."""

    #: Read by ``ArchiveBackendRegistry``: everything listed here stays in memory.
    confidential = True

    def __init__(self, keyring: VaultKeyring) -> None:
        self._keyring = keyring

    def probe_entries(self, source: ArchiveSource) -> ArchiveContainerProbe:
        # Probing is for import and maintenance, which must work while the
        # vault is locked and must never read a sealed book's content.
        raise ArchiveUnsupportedFormat("Sealed books are not probed.")

    def list_entries(self, source: ArchiveSource, context: ArchiveScanContext) -> ArchiveListing:
        del context  # A sealed book has no password and no nested containers.
        if source.path is None or source.data is not None:
            raise ArchiveOpenError("A sealed book is read from its own file.")
        index = SealedCbzIndex.open(self._keyring, source.path)
        return ArchiveListing(
            index.entries,
            random_access_confirmed=True,
            # Decryption and the positional reads under it release the GIL;
            # measured, two concurrent 2.5 MB pages take 6.4 ms against 5.5 ms
            # for one.
            supports_concurrent_reads=True,
            handle=index,
        )

    def read_entries(
        self,
        source: ArchiveSource,
        entries: Sequence[tuple[str, str | None]],
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> dict[str, bytes]:
        index = source.handle
        if not isinstance(index, SealedCbzIndex):
            raise ArchiveReadError("This sealed book is not open.")
        return {
            name: index.read_member(name, limits=limits, budget=budget)
            for name, _password in entries
        }


def _read_central_directory(sealed: SealedFile) -> dict[str, _Member]:
    try:
        with zipfile.ZipFile(sealed.open_content(), "r") as archive:
            infos = archive.infolist()
    except VaultError as exc:
        raise _archive_error(exc) from exc
    except (zipfile.BadZipFile, zipfile.LargeZipFile, struct.error, EOFError, ValueError) as exc:
        raise ArchiveCorruptError("A sealed book's archive is malformed.") from exc
    # Last one wins for a repeated name, matching what ``ZipFile.open`` reads.
    return {
        info.filename: _Member(
            header_offset=info.header_offset,
            compress_type=info.compress_type,
            compress_size=info.compress_size,
            file_size=info.file_size,
            crc=info.CRC,
            flag_bits=info.flag_bits,
        )
        for info in infos
        if not info.is_dir()
    }


def _decompress(raw: bytes, member: _Member, limits: ArchiveOpenLimits) -> bytes:
    if member.compress_type == zipfile.ZIP_STORED:
        if len(raw) != member.file_size:
            raise ArchiveCorruptError("A sealed book entry has the wrong size.")
        return raw
    if member.compress_type != zipfile.ZIP_DEFLATED:
        # Canonical CBZs store pages and deflate sidecars, nothing else.
        raise ArchiveReadError("A sealed book entry uses an unsupported compression.")
    # Bounded by the declared size, which the central directory states and
    # the CRC then confirms: an entry that inflates past it is malformed, and
    # must not be allowed to allocate its way to the end of the stream first.
    ceiling = member.file_size
    if limits.max_extracted_item_bytes is not None:
        ceiling = min(ceiling, limits.max_extracted_item_bytes)
    inflater = zlib.decompressobj(-zlib.MAX_WBITS)
    try:
        data = inflater.decompress(raw, ceiling + 1)
    except zlib.error as exc:
        raise ArchiveCorruptError("A sealed book entry does not inflate.") from exc
    if len(data) != member.file_size or inflater.unconsumed_tail:
        raise ArchiveCorruptError("A sealed book entry has the wrong size.")
    return data


def _archive_error(error: VaultError) -> ArchiveError:
    if isinstance(error, VaultLockedError):
        return ArchiveVaultLocked()
    if isinstance(error, VaultKeyMismatchError):
        return ArchiveOpenError("This sealed book belongs to another Library's Hidden Space.")
    # Integrity and format failures: the file is not what JoyRead sealed.
    return ArchiveCorruptError("This sealed book is damaged.")


__all__ = ["JrlArchiveBackend", "SealedCbzIndex"]
