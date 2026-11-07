"""Hidden Space covers: sealed on disk, decrypted only into a bounded memory cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from joyread.core.services import sealed_cover_store as store_module
from joyread.core.services.sealed_cover_store import SealedCoverStore
from joyread.core.vault import (
    ContentType,
    SealedFile,
    VaultFormatError,
    VaultKeyring,
    VaultLockedError,
    VaultPrivateKey,
)
from joyread.infrastructure.filesystem.path_service import PathService
from tests.support.sealed_books import png_page, seal_file

BOOK = "4b0c6c2e-8f1e-4a55-9d0e-3c1f1c2b7a10"
OTHER = "9f8e7d6c-5b4a-4321-8765-0fedcba98765"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def key() -> VaultPrivateKey:
    return VaultPrivateKey.generate()


@pytest.fixture
def keyring(key: VaultPrivateKey) -> VaultKeyring:
    ring = VaultKeyring()
    ring.unlock(key)
    return ring


@pytest.fixture
def store(tmp_path: Path, keyring: VaultKeyring) -> SealedCoverStore:
    paths = PathService(base_dir=tmp_path)
    paths.ensure_directories()
    return SealedCoverStore(paths, keyring)


def test_a_written_cover_is_sealed_on_disk_and_reads_back(
    store: SealedCoverStore, key: VaultPrivateKey
) -> None:
    cover = png_page(1, size=(120, 160))

    path = store.write(BOOK, cover, key.public_key)

    assert path == store.covers_dir / f"{BOOK}.jrl"
    on_disk = path.read_bytes()
    assert PNG_SIGNATURE not in on_disk and cover[:64] not in on_disk
    assert not list(store.covers_dir.glob("*.partial"))
    assert store.read(BOOK) == cover
    assert store.read(OTHER) is None


def test_writing_needs_only_the_public_key(tmp_path: Path, key: VaultPrivateKey) -> None:
    locked = VaultKeyring()
    paths = PathService(base_dir=tmp_path)
    store = SealedCoverStore(paths, locked)
    cover = png_page(2)

    store.write(BOOK, cover, key.public_key)

    with pytest.raises(VaultLockedError):
        store.read(BOOK)
    locked.unlock(key)
    assert store.read(BOOK) == cover


def test_locking_purges_decrypted_covers(
    store: SealedCoverStore, keyring: VaultKeyring, key: VaultPrivateKey
) -> None:
    store.write(BOOK, png_page(3), key.public_key)
    store.read(BOOK)
    assert store.cached_bytes > 0

    keyring.lock()

    assert store.cached_bytes == 0
    with pytest.raises(VaultLockedError):
        store.read(BOOK)


def test_repeat_reads_are_served_from_memory(
    store: SealedCoverStore, keyring: VaultKeyring, key: VaultPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    cover = png_page(4)
    store.write(BOOK, cover, key.public_key)
    opens: list[Path] = []
    real_open = keyring.open_sealed

    def counting_open(path: Path) -> SealedFile:
        opens.append(path)
        return real_open(path)

    monkeypatch.setattr(keyring, "open_sealed", counting_open)

    assert store.read(BOOK) == cover
    assert store.read(BOOK) == cover
    assert len(opens) == 1


def test_a_read_that_races_a_lock_does_not_refill_the_cache(
    store: SealedCoverStore, keyring: VaultKeyring, key: VaultPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    cover = png_page(5)
    store.write(BOOK, cover, key.public_key)
    real_read_all = SealedFile.read_all

    def read_then_lock(self: SealedFile) -> bytes:
        data = real_read_all(self)
        keyring.lock()
        return data

    monkeypatch.setattr(SealedFile, "read_all", read_then_lock)

    assert store.read(BOOK) == cover
    assert store.cached_bytes == 0


def test_the_cache_is_bounded_by_bytes(tmp_path: Path, keyring: VaultKeyring, key: VaultPrivateKey) -> None:
    first, second = png_page(6), png_page(7)
    store = SealedCoverStore(
        PathService(base_dir=tmp_path), keyring, cache_max_bytes=max(len(first), len(second)) + 10
    )
    store.write(BOOK, first, key.public_key)
    store.write(OTHER, second, key.public_key)

    store.read(BOOK)
    store.read(OTHER)

    assert store.cached_bytes == len(second)


def test_rewriting_and_deleting_replace_what_readers_see(
    store: SealedCoverStore, key: VaultPrivateKey
) -> None:
    store.write(BOOK, png_page(8), key.public_key)
    store.read(BOOK)
    replacement = png_page(9)

    store.write(BOOK, replacement, key.public_key)
    assert store.read(BOOK) == replacement

    store.delete(BOOK)
    assert store.read(BOOK) is None
    assert not store.path_for(BOOK).exists()


def test_only_plain_book_ids_name_cover_files(store: SealedCoverStore) -> None:
    for bad in ("../escape", "a/b", "", ".hidden", "x" * 200):
        with pytest.raises(ValueError):
            store.path_for(bad)
    assert store.is_sealed_cover(store.path_for(BOOK))
    assert not store.is_sealed_cover(store.covers_dir / f"{BOOK}-generated-200x284.png")
    assert not store.is_sealed_cover(store.covers_dir.parent / f"{BOOK}.jrl")


def test_a_cover_file_holding_something_else_is_refused(
    store: SealedCoverStore, key: VaultPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    seal_file(store.path_for(BOOK), b"PK\x03\x04 a book, not a cover", key, ContentType.CBZ)
    with pytest.raises(VaultFormatError):
        store.read(BOOK)

    monkeypatch.setattr(store_module, "MAX_COVER_BYTES", 16)
    seal_file(store.path_for(OTHER), png_page(10), key, ContentType.PNG)
    with pytest.raises(VaultFormatError):
        store.read(OTHER)


def test_closing_the_store_stops_listening_to_the_keyring(
    store: SealedCoverStore, keyring: VaultKeyring
) -> None:
    listeners = len(keyring._listeners)

    store.close()

    assert len(keyring._listeners) == listeners - 1
