"""Write a book's sealed ``.jrl`` without its plaintext ever reaching ``Books/``.

Two ways in, one result:

* :class:`SealedCanonicalWriter` is a ``CanonicalWriter``. Handed to
  ``ArchiveImageService.convert_to_canonical``, it streams the canonical CBZ
  straight through the STREAM sealer, so the converted book exists on disk
  only as ciphertext.
* :func:`seal_verbatim` seals bytes that need no conversion -- a canonical CBZ
  or a PDF -- and checks them against the hash the Library recorded.

Both return a :class:`SealedOutput`, which can prove the file on disk decrypts
to exactly what was written. The writer holds the file key, so that check works
while the vault is locked, which is when most sealing runs.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Any, BinaryIO, Callable

from joyread.core.archive.canonical import CanonicalWriteResult, CbzWriter
from joyread.core.archive.inspection import ArchiveMetadataEntry
from joyread.core.archive.records import PageRecord
from joyread.core.file_types import SEALED_EXTENSION
from joyread.core.vault import (
    ContentType,
    SealedWriter,
    SealResult,
    VaultIntegrityError,
    VaultPublicKey,
    seal_stream,
)


#: Read size when sealing or verifying: large enough that per-call overhead
#: disappears, small enough to stay out of the way of the Reader's memory.
_BLOCK_BYTES = 1 << 20

DigestFactory = Callable[[], Any]


class SourceChangedError(Exception):
    """The stored file no longer hashes to what the Library recorded."""


@dataclass(frozen=True)
class SealedOutput:
    """A finished ``.jrl`` and what it takes to trust it."""

    writer: SealedWriter
    result: SealResult
    #: The ciphertext's hash under the Library's algorithm: the new
    #: ``book_files.stored_hash``, checkable while the vault is locked.
    stored_hash: str

    def verify(self, path: Path, *, is_cancelled: Callable[[], bool] | None = None) -> None:
        """Decrypt *path* with the writer's own key and compare it to what was written."""

        digest = hashlib.sha256()
        length = 0
        with self.writer.reopen(path) as sealed:
            if (sealed.content_type, sealed.content_length) != (
                self.result.content_type,
                self.result.content_length,
            ):
                raise VaultIntegrityError("The sealed file describes different content than was written.")
            for block in sealed.iter_content(is_cancelled=is_cancelled):
                digest.update(block)
                length += len(block)
        if length != self.result.content_length or digest.digest() != self.result.content_sha256:
            raise VaultIntegrityError("The sealed file does not decrypt to what was written.")


class SealedCanonicalWriter:
    """A ``CanonicalWriter`` whose output is a sealed canonical CBZ."""

    suffix = SEALED_EXTENSION

    def __init__(self, recipient: VaultPublicKey, digest_factory: DigestFactory) -> None:
        self._recipient = recipient
        self._digest_factory = digest_factory
        #: Set by a successful :meth:`write`; the caller reads it back.
        self.output: SealedOutput | None = None

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
        self.output = None
        digest = self._digest_factory()
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination.open("wb") as sink:
                writer = SealedWriter(sink, self._recipient, ContentType.CBZ, on_ciphertext=digest.update)
                result = CbzWriter().write_stream(
                    writer,
                    placed_pages,
                    sidecars,
                    read_page=read_page,
                    is_cancelled=is_cancelled,
                    on_page=on_page,
                )
                seal = writer.finish()
                fsync_file(sink)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        self.output = SealedOutput(writer, seal, digest.hexdigest())
        return result


def seal_verbatim(
    source: Path,
    destination: Path,
    recipient: VaultPublicKey,
    content_type: ContentType,
    *,
    digest_factory: DigestFactory,
    expected_source_hash: str,
    is_cancelled: Callable[[], bool] | None = None,
) -> SealedOutput:
    """Seal *source* byte for byte into *destination*.

    The source is hashed as it streams, and must match *expected_source_hash*:
    sealing deletes the plaintext afterwards, so sealing bytes the Library does
    not recognise would quietly replace a book with something else. Unsealing
    a PDF depends on it too -- the decrypted copy has to be the PDF whose hash
    names it.
    """

    source_digest = digest_factory()
    ciphertext_digest = digest_factory()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.open("rb") as handle, destination.open("wb") as sink:
            writer, seal = seal_stream(
                _HashingReader(handle, source_digest),
                sink,
                recipient,
                content_type,
                block_size=_BLOCK_BYTES,
                is_cancelled=is_cancelled,
                on_ciphertext=ciphertext_digest.update,
            )
            fsync_file(sink)
        if source_digest.hexdigest() != expected_source_hash:
            raise SourceChangedError("The stored file changed since the Library recorded it.")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return SealedOutput(writer, seal, ciphertext_digest.hexdigest())


def fsync_file(handle: BinaryIO) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def fsync_directory(path: Path) -> None:
    """Make a rename inside *path* durable. Best effort: Windows has no directory fsync."""

    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class _HashingReader:
    """A read-only stream that feeds every byte it returns to a digest."""

    def __init__(self, handle: BinaryIO, digest: Any) -> None:
        self._handle = handle
        self._digest = digest

    def read(self, size: int = -1) -> bytes:
        block = self._handle.read(size)
        self._digest.update(block)
        return block


__all__ = [
    "SealedCanonicalWriter",
    "SealedOutput",
    "SourceChangedError",
    "fsync_directory",
    "fsync_file",
    "seal_verbatim",
]
