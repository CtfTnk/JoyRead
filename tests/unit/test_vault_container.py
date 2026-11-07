"""The .jrl container: what it guarantees, and what it refuses.

Most cases use 1 KiB chunks so multi-chunk geometry costs a few kilobytes
instead of megabytes; the code paths are the ones the 64 KiB default takes.
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import threading
import zipfile

import pytest

from joyread.core.vault import (
    ContentType,
    SealedFile,
    SealedWriter,
    VaultFormatError,
    VaultIntegrityError,
    VaultKeyMismatchError,
    VaultOperationCancelled,
    VaultPrivateKey,
    padme,
    read_header,
    seal_bytes,
    seal_stream,
)
from joyread.core.vault.container import HEADER_BYTES, TAG_BYTES, TRAILER_BYTES

from tests.support import vault_golden


CHUNK_LOG2 = 10
CHUNK = 1 << CHUNK_LOG2
STRIDE = CHUNK + TAG_BYTES


@pytest.fixture(scope="module")
def key() -> VaultPrivateKey:
    return VaultPrivateKey.generate()


def _seal(content: bytes, key: VaultPrivateKey, content_type: ContentType = ContentType.CBZ) -> bytes:
    return seal_bytes(content, key.public_key, content_type, chunk_log2=CHUNK_LOG2)


@pytest.mark.parametrize(
    "size",
    [0, 1, 31, 32, 33, CHUNK - 33, CHUNK - 32, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK + 5, 20_000],
)
def test_round_trip_at_every_chunk_boundary(size: int, key: VaultPrivateKey) -> None:
    content = os.urandom(size)
    blob = _seal(content, key)

    with SealedFile.from_bytes(blob, key) as sealed:
        assert sealed.content_length == size
        assert sealed.content_type is ContentType.CBZ
        assert sealed.read_all() == content
        # The padded plaintext, not the content, decides the file size.
        assert sealed.plaintext_length == padme(size + TRAILER_BYTES)
        chunks = -(-sealed.plaintext_length // CHUNK)
        assert len(blob) == HEADER_BYTES + sealed.plaintext_length + chunks * TAG_BYTES


def test_random_access_reads_match_the_plaintext(key: VaultPrivateKey) -> None:
    content = os.urandom(9 * CHUNK + 123)
    with SealedFile.from_bytes(_seal(content, key), key) as sealed:
        for offset, length in [
            (0, 1), (CHUNK - 1, 2), (CHUNK, CHUNK), (5, 4 * CHUNK), (len(content) - 3, 3),
            (len(content) - 3, 100),  # clamped at the end of the content
            (len(content), 10),  # past the end: nothing, not padding
        ]:
            assert sealed.read(offset, length) == content[offset : offset + length]


def test_streamed_writes_equal_a_single_write(key: VaultPrivateKey) -> None:
    content = os.urandom(5 * CHUNK + 77)
    sink = io.BytesIO()
    writer = SealedWriter(sink, key.public_key, ContentType.PDF, chunk_log2=CHUNK_LOG2)
    position = 0
    for step in (1, 7, CHUNK - 1, CHUNK, 3, 2 * CHUNK + 11):
        writer.write(content[position : position + step])
        position += step
    writer.write(content[position:])
    result = writer.finish()

    assert result.content_sha256 == hashlib.sha256(content).digest()
    assert result.sealed_length == len(sink.getvalue())
    with SealedFile.from_bytes(sink.getvalue(), key) as sealed:
        assert sealed.read_all() == content
        assert sealed.content_type is ContentType.PDF


def test_every_header_field_is_where_the_spec_says(key: VaultPrivateKey) -> None:
    blob = _seal(b"x" * 100, key)

    assert blob[:4] == b"JRL1"
    assert blob[4] == 1  # suite
    assert blob[5] == CHUNK_LOG2
    assert blob[6:8] == b"\x00\x00"
    assert blob[8:24] == key.public_key.key_id


def test_the_frozen_v1_file_still_opens() -> None:
    """Every later build must read what version 1 wrote."""

    key = VaultPrivateKey.from_raw(vault_golden.PRIVATE_KEY)
    with SealedFile.from_bytes(vault_golden.SEALED, key) as sealed:
        assert sealed.header.chunk_log2 == vault_golden.CHUNK_LOG2
        assert sealed.content_type is ContentType.CBZ
        assert sealed.read_all() == vault_golden.CONTENT
        assert sealed.read(1000, 300) == vault_golden.CONTENT[1000:1300]


def test_a_flipped_byte_fails_only_the_reads_that_touch_its_chunk(key: VaultPrivateKey) -> None:
    content = os.urandom(6 * CHUNK)
    blob = bytearray(_seal(content, key))
    blob[HEADER_BYTES + 2 * STRIDE + 10] ^= 0x01  # inside chunk 2

    with SealedFile.from_bytes(bytes(blob), key) as sealed:
        assert sealed.read(0, CHUNK) == content[:CHUNK]
        assert sealed.read(4 * CHUNK, CHUNK) == content[4 * CHUNK : 5 * CHUNK]
        with pytest.raises(VaultIntegrityError):
            sealed.read(2 * CHUNK + 5, 10)
        with pytest.raises(VaultIntegrityError):
            sealed.read_all()


@pytest.mark.parametrize("cut", ["chunk_boundary", "mid_chunk", "inside_tag"])
def test_truncation_is_detected(cut: str, key: VaultPrivateKey) -> None:
    blob = _seal(os.urandom(4 * CHUNK), key)
    if cut == "chunk_boundary":
        # The new last chunk was sealed as "not last", so it cannot pass as last.
        truncated = blob[: HEADER_BYTES + 3 * STRIDE]
    elif cut == "mid_chunk":
        truncated = blob[: HEADER_BYTES + 2 * STRIDE + 500]
    else:
        truncated = blob[: HEADER_BYTES + 2 * STRIDE + 7]

    with pytest.raises((VaultIntegrityError, VaultFormatError)):
        SealedFile.from_bytes(truncated, key)


def test_reordered_chunks_fail(key: VaultPrivateKey) -> None:
    content = os.urandom(5 * CHUNK)
    blob = _seal(content, key)
    body = blob[HEADER_BYTES:]
    chunk1, chunk2 = body[STRIDE : 2 * STRIDE], body[2 * STRIDE : 3 * STRIDE]
    swapped = blob[:HEADER_BYTES] + body[:STRIDE] + chunk2 + chunk1 + body[3 * STRIDE :]

    with SealedFile.from_bytes(swapped, key) as sealed:
        with pytest.raises(VaultIntegrityError):
            sealed.read(CHUNK, 10)


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (5, VaultIntegrityError),  # chunk size, still in range: bound by the key wrap
        (6, VaultFormatError),  # flags: refused before any key is used
        (9, VaultKeyMismatchError),  # key id: no longer names this vault
        (30, VaultIntegrityError),  # file nonce
        (50, VaultIntegrityError),  # HPKE enc
        (80, VaultIntegrityError),  # sealed file key
    ],
)
def test_no_header_byte_can_change(offset: int, expected: type[Exception], key: VaultPrivateKey) -> None:
    blob = bytearray(_seal(os.urandom(2 * CHUNK), key))
    blob[offset] ^= 0x01

    with pytest.raises(expected):
        SealedFile.from_bytes(bytes(blob), key)


def test_another_vault_cannot_open_the_file(key: VaultPrivateKey) -> None:
    blob = _seal(b"secret pages", key)

    with pytest.raises(VaultKeyMismatchError):
        SealedFile.from_bytes(blob, VaultPrivateKey.generate())


def test_files_that_are_not_sealed_are_refused(key: VaultPrivateKey, tmp_path: Path) -> None:
    for junk in (b"", b"PK\x03\x04" + os.urandom(200), b"JRL1" + os.urandom(50)):
        with pytest.raises(VaultFormatError):
            SealedFile.from_bytes(junk, key)
    plain = tmp_path / "plain.cbz"
    plain.write_bytes(b"PK\x03\x04" + os.urandom(500))
    with pytest.raises(VaultFormatError):
        read_header(plain)


def test_padme_rounds_up_by_little_and_is_stable() -> None:
    assert [padme(value) for value in (0, 1, 2, 9, 100, 1000)] == [0, 1, 2, 10, 104, 1024]
    for length in list(range(1, 5000)) + [10**6 + 7, 2 * 10**8 + 3]:
        padded = padme(length)
        assert padded >= length
        assert padme(padded) == padded
        if length >= 64:
            assert (padded - length) / length <= 0.12


def test_writer_streams_into_zipfile_without_seeking(key: VaultPrivateKey, tmp_path: Path) -> None:
    """The path CbzWriter takes when sealing: no plaintext CBZ touches disk."""

    pages = {f"{index:03d}.jpg": os.urandom(3 * CHUNK + index) for index in range(4)}
    target = tmp_path / "book.jrl"
    with target.open("wb") as sink:
        writer = SealedWriter(sink, key.public_key, ContentType.CBZ, chunk_log2=CHUNK_LOG2)
        with zipfile.ZipFile(writer, "w") as archive:
            for name, data in pages.items():
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                archive.writestr(info, data)
        result = writer.finish()

    # The writer can prove its own output decrypts without the vault key.
    with writer.reopen(target) as reopened:
        assert hashlib.sha256(reopened.read_all()).digest() == result.content_sha256

    with SealedFile.open(target, key) as sealed, zipfile.ZipFile(sealed.open_content()) as archive:
        assert archive.namelist() == list(pages)
        for name, data in pages.items():
            assert archive.read(name) == data


def test_content_view_seeks_like_a_file(key: VaultPrivateKey) -> None:
    content = os.urandom(7 * CHUNK + 9)
    with SealedFile.from_bytes(_seal(content, key), key) as sealed:
        view = sealed.open_content()
        assert view.seek(0, io.SEEK_END) == len(content)
        view.seek(-20, io.SEEK_END)
        assert view.read() == content[-20:]
        view.seek(CHUNK - 3)
        assert view.read(10) == content[CHUNK - 3 : CHUNK + 7]
        view.seek(5, io.SEEK_CUR)
        assert view.tell() == CHUNK + 12
        assert view.read(6 * CHUNK) == content[CHUNK + 12 : 7 * CHUNK + 12]
        with pytest.raises(ValueError):
            view.seek(-1)


def test_concurrent_reads_share_one_open_file(key: VaultPrivateKey, tmp_path: Path) -> None:
    content = os.urandom(40 * CHUNK)
    target = tmp_path / "shared.jrl"
    target.write_bytes(_seal(content, key))
    failures: list[str] = []

    with SealedFile.open(target, key) as sealed:
        def reader(start: int) -> None:
            for offset in range(start, len(content) - CHUNK, 7 * CHUNK):
                if sealed.read(offset, CHUNK + 5) != content[offset : offset + CHUNK + 5]:
                    failures.append(f"offset {offset}")

        threads = [threading.Thread(target=reader, args=(start,)) for start in (0, 1, 2 * CHUNK + 3, 5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert failures == []


def test_seal_stream_verifies_and_cancels(key: VaultPrivateKey, tmp_path: Path) -> None:
    content = os.urandom(3 << 20)
    target = tmp_path / "book.jrl"
    with target.open("wb") as sink:
        writer, result = seal_stream(io.BytesIO(content), sink, key.public_key, ContentType.PDF)
    with writer.reopen(target) as reopened:
        assert hashlib.sha256(reopened.read_all()).digest() == result.content_sha256 == hashlib.sha256(content).digest()

    calls = iter([False, True])
    with pytest.raises(VaultOperationCancelled):
        seal_stream(
            io.BytesIO(content), io.BytesIO(), key.public_key, ContentType.PDF,
            is_cancelled=lambda: next(calls),
        )


def test_a_finished_writer_refuses_more_content(key: VaultPrivateKey) -> None:
    writer = SealedWriter(io.BytesIO(), key.public_key, ContentType.PNG)
    writer.write(b"cover")
    writer.finish()
    with pytest.raises(ValueError):
        writer.write(b"more")
    with pytest.raises(ValueError):
        writer.finish()
    with pytest.raises(io.UnsupportedOperation):
        writer.seek(0)


def test_reprs_never_show_key_material(key: VaultPrivateKey) -> None:
    text = repr(key)
    assert key.raw().hex() not in text
    assert key.public_key.key_id.hex() in text
