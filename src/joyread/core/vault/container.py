"""The ``.jrl`` sealed container, format version 1.

A ``.jrl`` file holds one piece of content -- a canonical CBZ, a PDF, or a PNG
cover -- encrypted to a vault's public key. Sealing needs only that public key,
so hiding a book never asks for the passphrase. Reading needs the unlocked
private key. ``docs/jrl-format.md`` is the normative description; this
docstring is the summary a maintainer needs before changing anything here.

Layout (integers big-endian)::

    header, 104 bytes, plaintext
         0   4  magic "JRL1"
         4   1  suite (1 = X25519 / HKDF-SHA256 / ChaCha20-Poly1305)
         5   1  log2 of the chunk size (16 = 64 KiB)
         6   1  flags (0)
         7   1  reserved (0)
         8  16  vault key id
        24  16  file nonce
        40  32  HPKE encapsulated key
        72  32  16-byte file key sealed with HPKE, plus its tag
    payload
        STREAM chunks: every plaintext chunk is chunk-size bytes except the
        last, each sealed with ChaCha20-Poly1305 under the payload key and the
        nonce counter(11) || last(1)

The plaintext is ``content || zero padding || 32-byte trailer``, padded so
its length is the Padmé size of content plus trailer.

Why it is built this way -- each point is easy to undo by accident:

* **One stream, not one record per page.** Per-page sizes would show in the
  file, and that sequence of sizes can identify a known release.
* **Chunks authenticate independently.** Reading a page decrypts only the
  chunks it covers, and a damaged byte fails only the reads that touch it.
* **The last-chunk flag is in the nonce.** A file cut at a chunk boundary
  fails, because its new final chunk was never sealed as final.
* **Every header byte is bound.** HPKE authenticates the bytes before ``enc``
  and the payload key hashes the whole header, so no field can be swapped
  without the payload failing to decrypt.
* **The trailer is at the end.** The writer streams: nothing needs the content
  length before the content has been written.
* **Reads are positional.** No shared file offset means concurrent page reads
  need no lock, and decryption runs outside the GIL.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from enum import IntEnum
import hashlib
import io
import os
from pathlib import Path
import secrets
import struct
import threading
from typing import BinaryIO, Callable, Iterator

from Cryptodome.Cipher import ChaCha20_Poly1305
from Cryptodome.Hash import SHA256
from Cryptodome.Protocol import HPKE
from Cryptodome.Protocol.KDF import HKDF

from joyread.core.vault.errors import (
    VaultFormatError,
    VaultIntegrityError,
    VaultKeyMismatchError,
    VaultOperationCancelled,
)
from joyread.core.vault.keys import KEY_ID_BYTES, VaultPrivateKey, VaultPublicKey


MAGIC = b"JRL1"
SUITE_X25519_CHACHA20 = 1
HEADER_BYTES = 104
TAG_BYTES = 16
TRAILER_BYTES = 32
DEFAULT_CHUNK_LOG2 = 16
#: Readers accept a range so a future writer can tune the chunk size within
#: version 1. The ceiling also bounds the memory a hostile file can demand.
MIN_CHUNK_LOG2 = 10
MAX_CHUNK_LOG2 = 24

FILE_KEY_BYTES = 16
FILE_NONCE_BYTES = 16
_PREFIX_BYTES = 8 + KEY_ID_BYTES + FILE_NONCE_BYTES
_ENC_BYTES = 32
_SEALED_KEY_BYTES = FILE_KEY_BYTES + TAG_BYTES
_HPKE_INFO = b"joyread-jrl-v1"
_PAYLOAD_INFO = b"joyread-jrl-v1 payload"
_TRAILER_MAGIC = b"JRLT"
_TRAILER_VERSION = 1
_TRAILER_FIELDS = struct.Struct(">BBHQ")
_MAX_CHUNK_INDEX = (1 << 88) - 1
_HAS_PREAD = hasattr(os, "pread")


class ContentType(IntEnum):
    """What the sealed plaintext is. Stored in the encrypted trailer only."""

    CBZ = 1
    PDF = 2
    PNG = 3


def padme(length: int) -> int:
    """Round *length* up to its Padmé size (Nikitin et al., PETS 2019).

    Leaks only O(log log n) bits of the length and costs at most about 12%,
    falling below 2% for book-sized files. Idempotent: a padded length pads to
    itself.
    """

    if length < 2:
        return length
    exponent = length.bit_length() - 1
    significant = exponent.bit_length()
    mask = (1 << (exponent - significant)) - 1
    return (length + mask) & ~mask


@dataclass(frozen=True)
class SealedHeader:
    """The plaintext header. Carries nothing about the book itself."""

    suite: int
    chunk_log2: int
    key_id: bytes
    file_nonce: bytes
    enc: bytes
    sealed_file_key: bytes
    flags: int = 0

    @property
    def chunk_size(self) -> int:
        return 1 << self.chunk_log2

    @classmethod
    def parse(cls, raw: bytes) -> SealedHeader:
        if len(raw) < HEADER_BYTES or raw[:4] != MAGIC:
            raise VaultFormatError("Not a JoyRead sealed file.")
        suite, chunk_log2, flags, reserved = raw[4], raw[5], raw[6], raw[7]
        if suite != SUITE_X25519_CHACHA20:
            raise VaultFormatError(f"Unsupported sealed-file suite {suite}.")
        if not MIN_CHUNK_LOG2 <= chunk_log2 <= MAX_CHUNK_LOG2:
            raise VaultFormatError(f"Unsupported sealed-file chunk size 2^{chunk_log2}.")
        if flags or reserved:
            # Unknown flags may change how the payload is read. Refusing is the
            # only safe answer; guessing would misread a newer file.
            raise VaultFormatError("Sealed file uses flags this version does not know.")
        key_end = 8 + KEY_ID_BYTES
        nonce_end = key_end + FILE_NONCE_BYTES
        enc_end = nonce_end + _ENC_BYTES
        return cls(
            suite=suite,
            chunk_log2=chunk_log2,
            flags=flags,
            key_id=bytes(raw[8:key_end]),
            file_nonce=bytes(raw[key_end:nonce_end]),
            enc=bytes(raw[nonce_end:enc_end]),
            sealed_file_key=bytes(raw[enc_end:HEADER_BYTES]),
        )


@dataclass(frozen=True)
class SealResult:
    """What a finished seal wrote."""

    content_type: ContentType
    content_length: int
    plaintext_length: int
    chunk_count: int
    #: Bytes written to the sink, header included.
    sealed_length: int
    #: SHA-256 of the content as it was written. Compare it with a read-back
    #: through :meth:`SealedWriter.reopen` to prove the bytes on disk decrypt.
    content_sha256: bytes


class SealedWriter:
    """Encrypt a stream of content into a sealed file object.

    Write-only and deliberately not seekable. ``zipfile`` then writes data
    descriptors instead of seeking back to patch local headers, which is what
    lets ``CbzWriter`` stream straight into it: no plaintext CBZ ever exists
    on disk. Call :meth:`finish` exactly once. If anything fails before then,
    the caller owns deleting the partial output.
    """

    def __init__(
        self,
        sink: BinaryIO,
        recipient: VaultPublicKey,
        content_type: ContentType,
        *,
        chunk_log2: int = DEFAULT_CHUNK_LOG2,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        on_ciphertext: Callable[[bytes], None] | None = None,
    ) -> None:
        if not MIN_CHUNK_LOG2 <= chunk_log2 <= MAX_CHUNK_LOG2:
            raise ValueError(f"Chunk size 2^{chunk_log2} is outside the supported range.")
        file_key = random_bytes(FILE_KEY_BYTES)
        file_nonce = random_bytes(FILE_NONCE_BYTES)
        if len(file_key) != FILE_KEY_BYTES or len(file_nonce) != FILE_NONCE_BYTES:
            raise ValueError("random_bytes returned the wrong number of bytes.")

        self._sink = sink
        self._on_ciphertext = on_ciphertext
        self._content_type = ContentType(content_type)
        self._chunk_size = 1 << chunk_log2
        prefix = (
            MAGIC
            + bytes((SUITE_X25519_CHACHA20, chunk_log2, 0, 0))
            + recipient.key_id
            + file_nonce
        )
        sender = HPKE.new(
            receiver_key=recipient.ecc_key(),
            aead_id=HPKE.AEAD.CHACHA20_POLY1305,
            info=_HPKE_INFO,
        )
        header = prefix + sender.enc + sender.seal(file_key, prefix)
        if len(header) != HEADER_BYTES:  # pragma: no cover - fixed by the suite.
            raise RuntimeError("Sealed header has an unexpected length.")
        self._header = header
        self._key = _payload_key(file_key, header)
        self._buffer = bytearray()
        self._index = 0
        self._content_length = 0
        self._written = 0
        self._digest = hashlib.sha256()
        self._result: SealResult | None = None
        self._write_raw(header)

    # -- file-object surface used by zipfile --------------------------------

    def write(self, data: bytes | bytearray | memoryview) -> int:
        if self._result is not None:
            raise ValueError("This sealed stream is already finished.")
        chunk = bytes(data)
        self._digest.update(chunk)
        self._content_length += len(chunk)
        self._append(chunk)
        return len(chunk)

    def tell(self) -> int:
        return self._content_length

    def seek(self, *_args: object) -> int:
        raise io.UnsupportedOperation("A sealed stream cannot seek.")

    def seekable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def flush(self) -> None:
        flush = getattr(self._sink, "flush", None)
        if callable(flush):
            flush()

    # -- sealing --------------------------------------------------------------

    def finish(self) -> SealResult:
        """Pad, append the trailer, and seal the final chunk as final."""

        if self._result is not None:
            raise ValueError("This sealed stream is already finished.")
        total = padme(self._content_length + TRAILER_BYTES)
        remaining = total - self._content_length - TRAILER_BYTES
        zeros = bytes(min(remaining, self._chunk_size))
        while remaining:
            step = min(remaining, len(zeros))
            self._append(zeros[:step])
            remaining -= step
        self._append(_trailer(self._content_type, self._content_length))
        # ``_append`` always keeps between 1 and chunk-size bytes back, and the
        # trailer alone is 32 bytes, so the final chunk is never empty.
        self._emit(bytes(self._buffer), last=True)
        self._buffer.clear()
        self.flush()
        self._result = SealResult(
            content_type=self._content_type,
            content_length=self._content_length,
            plaintext_length=total,
            chunk_count=self._index,
            sealed_length=self._written,
            content_sha256=self._digest.digest(),
        )
        return self._result

    def reopen(self, path: Path | str) -> SealedFile:
        """Open the finished output with this writer's own payload key.

        Proves the bytes that reached the disk decrypt, without needing the
        vault private key -- sealing usually runs while the vault is locked.
        """

        if self._result is None:
            raise ValueError("Finish the sealed stream before reopening it.")
        source = _PositionalFile(Path(path))
        try:
            if source.read_at(0, HEADER_BYTES) != self._header:
                raise VaultIntegrityError("The sealed header on disk differs from the one written.")
            return SealedFile(source, SealedHeader.parse(self._header), self._key)
        except BaseException:
            source.close()
            raise

    def _append(self, data: bytes) -> None:
        self._buffer += data
        # Hold the newest chunk back: STREAM must flag the final chunk, and a
        # chunk is only known to be final when ``finish`` runs.
        surplus = len(self._buffer) - 1
        if surplus < self._chunk_size:
            return
        count = surplus // self._chunk_size
        size = self._chunk_size
        for index in range(count):
            self._emit(bytes(self._buffer[index * size : (index + 1) * size]), last=False)
        del self._buffer[: count * size]

    def _emit(self, chunk: bytes, *, last: bool) -> None:
        cipher = ChaCha20_Poly1305.new(key=self._key, nonce=_chunk_nonce(self._index, last))
        body, tag = cipher.encrypt_and_digest(chunk)
        self._write_raw(body)
        self._write_raw(tag)
        self._index += 1

    def _write_raw(self, data: bytes) -> None:
        self._sink.write(data)
        self._written += len(data)
        if self._on_ciphertext is not None:
            self._on_ciphertext(data)


class SealedFile:
    """A random-access, thread-safe reader over one sealed file.

    Holds the derived payload key, never the vault private key, so a long-lived
    reader does not keep the vault key reachable. Content offsets are relative
    to the content; padding and the trailer are never returned.
    """

    def __init__(self, source: _Source, header: SealedHeader, payload_key: bytes) -> None:
        self.header = header
        self._source = source
        self._key = payload_key
        self._chunk_size = header.chunk_size
        self._stride = self._chunk_size + TAG_BYTES
        body = source.size() - HEADER_BYTES
        if body <= TAG_BYTES:
            raise VaultFormatError("Sealed file is too short to hold any content.")
        self._chunks = -(-body // self._stride)
        self._last_stride = body - (self._chunks - 1) * self._stride
        if self._last_stride <= TAG_BYTES:
            raise VaultIntegrityError("Sealed file ends inside a chunk tag.")
        self.plaintext_length = body - self._chunks * TAG_BYTES
        if self.plaintext_length < TRAILER_BYTES:
            raise VaultFormatError("Sealed file is too short to hold its trailer.")
        trailer = self._read_plain(self.plaintext_length - TRAILER_BYTES, TRAILER_BYTES)
        self.content_type, self.content_length = _parse_trailer(trailer, self.plaintext_length)

    @classmethod
    def open(cls, path: Path | str, private_key: VaultPrivateKey) -> SealedFile:
        source = _PositionalFile(Path(path))
        try:
            return cls._open_source(source, private_key)
        except BaseException:
            source.close()
            raise

    @classmethod
    def from_bytes(cls, data: bytes, private_key: VaultPrivateKey) -> SealedFile:
        return cls._open_source(_BytesSource(bytes(data)), private_key)

    @classmethod
    def _open_source(cls, source: _Source, private_key: VaultPrivateKey) -> SealedFile:
        header_bytes = source.read_at(0, HEADER_BYTES)
        header = SealedHeader.parse(header_bytes)
        if header.key_id != private_key.public_key.key_id:
            raise VaultKeyMismatchError("This file was sealed to another vault.")
        try:
            receiver = HPKE.new(
                receiver_key=private_key.ecc_key(),
                aead_id=HPKE.AEAD.CHACHA20_POLY1305,
                enc=header.enc,
                info=_HPKE_INFO,
            )
            file_key = receiver.unseal(header.sealed_file_key, header_bytes[:_PREFIX_BYTES])
        except ValueError:
            # The key id already matched this vault, so a wrap that does not
            # open means the header was damaged, not that the file belongs
            # elsewhere. Raised without the original: its text can describe
            # key material.
            raise VaultIntegrityError("The sealed header failed authentication.") from None
        return cls(source, header, _payload_key(file_key, header_bytes))

    # -- reading -------------------------------------------------------------

    def read(self, offset: int, length: int) -> bytes:
        """Return up to *length* content bytes starting at *offset*."""

        if offset < 0 or length < 0:
            raise ValueError("Offset and length must not be negative.")
        end = min(offset + length, self.content_length)
        if offset >= end:
            return b""
        return self._read_plain(offset, end - offset)

    def iter_content(
        self,
        *,
        block_chunks: int = 16,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[bytes]:
        """Yield the content front to back in chunk-aligned blocks."""

        step = max(1, block_chunks) * self._chunk_size
        position = 0
        while position < self.content_length:
            if is_cancelled is not None and is_cancelled():
                raise VaultOperationCancelled("Reading the sealed content was cancelled.")
            length = min(step, self.content_length - position)
            yield self._read_plain(position, length)
            position += length

    def read_all(self) -> bytes:
        return b"".join(self.iter_content())

    def open_content(self) -> SealedContentView:
        return SealedContentView(self)

    def close(self) -> None:
        self._source.close()

    def __enter__(self) -> SealedFile:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _read_plain(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        if offset < 0 or offset + length > self.plaintext_length:
            raise ValueError("Read outside the sealed plaintext.")
        first = offset // self._chunk_size
        last = (offset + length - 1) // self._chunk_size
        blob = self._source.read_at(
            HEADER_BYTES + first * self._stride,
            (last - first + 1) * self._stride,
        )
        parts = [
            self._decrypt_chunk(index, blob[position * self._stride : (position + 1) * self._stride])
            for position, index in enumerate(range(first, last + 1))
        ]
        start = offset - first * self._chunk_size
        return b"".join(parts)[start : start + length]

    def _decrypt_chunk(self, index: int, piece: bytes) -> bytes:
        final = index == self._chunks - 1
        if len(piece) != (self._last_stride if final else self._stride):
            raise VaultIntegrityError(f"Sealed chunk {index} is truncated.")
        cipher = ChaCha20_Poly1305.new(key=self._key, nonce=_chunk_nonce(index, final))
        try:
            return cipher.decrypt_and_verify(piece[:-TAG_BYTES], piece[-TAG_BYTES:])
        except ValueError:
            raise VaultIntegrityError(f"Sealed chunk {index} failed authentication.") from None


class SealedContentView(io.RawIOBase):
    """A seekable, read-only file object over the sealed content.

    For consumers that read in small pieces -- ``zipfile`` walking a central
    directory, PDFium through a ``QIODevice`` -- it keeps the last few
    decrypted chunks so each chunk is decrypted once rather than once per
    read. Not thread-safe: give each consumer its own view. The
    :class:`SealedFile` underneath is shared safely.
    """

    _CACHED_CHUNKS = 4

    def __init__(self, sealed: SealedFile) -> None:
        super().__init__()
        self._sealed = sealed
        self._position = 0
        self._cache: OrderedDict[int, bytes] = OrderedDict()

    @property
    def size(self) -> int:
        return self._sealed.content_length

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._position + offset
        elif whence == io.SEEK_END:
            target = self._sealed.content_length + offset
        else:
            raise ValueError(f"Unsupported whence {whence}.")
        if target < 0:
            raise ValueError("Negative seek position.")
        self._position = target
        return target

    def readinto(self, buffer: bytearray | memoryview) -> int:
        view = memoryview(buffer).cast("B")
        size = min(len(view), max(0, self._sealed.content_length - self._position))
        if size <= 0:
            return 0
        view[:size] = self._read_cached(self._position, size)
        self._position += size
        return size

    def _read_cached(self, offset: int, length: int) -> bytes:
        chunk_size = self._sealed.header.chunk_size
        first = offset // chunk_size
        last = (offset + length - 1) // chunk_size
        if last - first + 1 > self._CACHED_CHUNKS:
            # A large read gains nothing from the cache and would flush it.
            return self._sealed.read(offset, length)
        joined = b"".join(self._chunk(index) for index in range(first, last + 1))
        start = offset - first * chunk_size
        return joined[start : start + length]

    def _chunk(self, index: int) -> bytes:
        cached = self._cache.get(index)
        if cached is not None:
            self._cache.move_to_end(index)
            return cached
        chunk_size = self._sealed.header.chunk_size
        start = index * chunk_size
        data = self._sealed._read_plain(start, min(chunk_size, self._sealed.plaintext_length - start))
        self._cache[index] = data
        if len(self._cache) > self._CACHED_CHUNKS:
            self._cache.popitem(last=False)
        return data


def read_header(path: Path | str) -> SealedHeader:
    """Parse a sealed file's header without any key.

    Enough to tell a sealed file from anything else and to see which vault it
    belongs to, so library maintenance can work while the vault is locked.
    """

    with Path(path).open("rb") as handle:
        return SealedHeader.parse(handle.read(HEADER_BYTES))


def seal_bytes(
    data: bytes,
    recipient: VaultPublicKey,
    content_type: ContentType,
    *,
    chunk_log2: int = DEFAULT_CHUNK_LOG2,
) -> bytes:
    """Seal a small in-memory payload, such as a cover, to bytes."""

    sink = io.BytesIO()
    writer = SealedWriter(sink, recipient, content_type, chunk_log2=chunk_log2)
    writer.write(data)
    writer.finish()
    return sink.getvalue()


def seal_stream(
    source: BinaryIO,
    sink: BinaryIO,
    recipient: VaultPublicKey,
    content_type: ContentType,
    *,
    block_size: int = 1 << 20,
    is_cancelled: Callable[[], bool] | None = None,
    on_ciphertext: Callable[[bytes], None] | None = None,
) -> tuple[SealedWriter, SealResult]:
    """Seal *source* byte for byte, for content that needs no conversion.

    Returns the writer as well, so the caller can :meth:`SealedWriter.reopen`
    the output and verify it before trusting it.
    """

    writer = SealedWriter(sink, recipient, content_type, on_ciphertext=on_ciphertext)
    while True:
        if is_cancelled is not None and is_cancelled():
            raise VaultOperationCancelled("Sealing was cancelled.")
        block = source.read(block_size)
        if not block:
            break
        writer.write(block)
    return writer, writer.finish()


# -- internals ----------------------------------------------------------------


def _payload_key(file_key: bytes, header: bytes) -> bytes:
    file_nonce = header[8 + KEY_ID_BYTES : _PREFIX_BYTES]
    return HKDF(
        file_key,
        32,
        file_nonce,
        SHA256,
        context=_PAYLOAD_INFO + hashlib.sha256(header).digest(),
    )


def _chunk_nonce(index: int, last: bool) -> bytes:
    if index > _MAX_CHUNK_INDEX:  # pragma: no cover - 2^88 chunks of 1 KiB is beyond any disk.
        raise VaultFormatError("Sealed file has too many chunks.")
    return index.to_bytes(11, "big") + (b"\x01" if last else b"\x00")


def _trailer(content_type: ContentType, content_length: int) -> bytes:
    return (
        _TRAILER_MAGIC
        + _TRAILER_FIELDS.pack(_TRAILER_VERSION, int(content_type), 0, content_length)
        + bytes(TRAILER_BYTES - 4 - _TRAILER_FIELDS.size)
    )


def _parse_trailer(raw: bytes, plaintext_length: int) -> tuple[ContentType, int]:
    if len(raw) != TRAILER_BYTES or raw[:4] != _TRAILER_MAGIC:
        raise VaultFormatError("Sealed file has no valid trailer.")
    end = 4 + _TRAILER_FIELDS.size
    version, kind, reserved, content_length = _TRAILER_FIELDS.unpack(raw[4:end])
    if version != _TRAILER_VERSION or reserved or any(raw[end:]):
        raise VaultFormatError("Sealed file trailer uses a version this build does not know.")
    try:
        content_type = ContentType(kind)
    except ValueError:
        raise VaultFormatError(f"Unknown sealed content type {kind}.") from None
    if content_length > plaintext_length - TRAILER_BYTES:
        raise VaultFormatError("Sealed content length exceeds the file.")
    return content_type, content_length


class _Source:
    """Positional byte access: the only thing :class:`SealedFile` needs."""

    def size(self) -> int:  # pragma: no cover - protocol
        raise NotImplementedError

    def read_at(self, offset: int, size: int) -> bytes:  # pragma: no cover - protocol
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - protocol
        raise NotImplementedError


class _BytesSource(_Source):
    def __init__(self, data: bytes) -> None:
        self._data = data

    def size(self) -> int:
        return len(self._data)

    def read_at(self, offset: int, size: int) -> bytes:
        return self._data[offset : offset + size]

    def close(self) -> None:
        return None


class _PositionalFile(_Source):
    """Read at an offset without a shared file position.

    POSIX keeps one descriptor and uses ``os.pread``, which never moves a
    shared offset, so concurrent page reads need no lock. Windows has no
    ``pread``: a short-lived handle per read gives the same independence and
    never holds the file open between reads.

    ``close`` waits for reads in flight before releasing the descriptor.
    Closing it under a running ``pread`` could let the OS hand the same
    number to another file, and the read would return that file's bytes.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._active = 0
        self._closed = False
        self._fd: int | None = None
        if _HAS_PREAD:
            self._fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        else:
            # Fail at open, not on the first page, if the file is unreadable.
            with path.open("rb"):
                pass

    def size(self) -> int:
        with self._lock:
            if self._fd is not None:
                return os.fstat(self._fd).st_size
        return self._path.stat().st_size

    def read_at(self, offset: int, size: int) -> bytes:
        with self._lock:
            if self._closed:
                raise ValueError("The sealed file is closed.")
            self._active += 1
            fd = self._fd
        try:
            if fd is None:
                with self._path.open("rb") as handle:
                    handle.seek(offset)
                    return handle.read(size)
            parts: list[bytes] = []
            position, remaining = offset, size
            while remaining > 0:
                piece = os.pread(fd, remaining, position)
                if not piece:
                    break
                parts.append(piece)
                position += len(piece)
                remaining -= len(piece)
            return b"".join(parts)
        finally:
            with self._lock:
                self._active -= 1
                self._release_if_idle()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._release_if_idle()

    def _release_if_idle(self) -> None:
        if self._closed and self._active == 0 and self._fd is not None:
            os.close(self._fd)
            self._fd = None


__all__ = [
    "ContentType",
    "DEFAULT_CHUNK_LOG2",
    "HEADER_BYTES",
    "MAGIC",
    "SealResult",
    "SealedContentView",
    "SealedFile",
    "SealedHeader",
    "SealedWriter",
    "TRAILER_BYTES",
    "padme",
    "read_header",
    "seal_bytes",
    "seal_stream",
]
