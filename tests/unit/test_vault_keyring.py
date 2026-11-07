"""The process-wide vault keyring: unlock, open, and revocation on lock."""

from __future__ import annotations

import gc
import os
from pathlib import Path

import pytest

from joyread.core.vault import (
    ContentType,
    SealedFile,
    VaultKeyring,
    VaultLockedError,
    VaultPrivateKey,
)
from joyread.core.vault import keyring as keyring_module
from tests.support.sealed_books import seal_file

CONTENT = bytes(range(256)) * 40


@pytest.fixture
def key() -> VaultPrivateKey:
    return VaultPrivateKey.generate()


@pytest.fixture
def sealed_path(tmp_path: Path, key: VaultPrivateKey) -> Path:
    return seal_file(tmp_path / "book.jrl", CONTENT, key, ContentType.CBZ)


def test_a_locked_keyring_opens_nothing(sealed_path: Path) -> None:
    keyring = VaultKeyring()

    assert not keyring.is_unlocked
    assert keyring.key_id is None
    with pytest.raises(VaultLockedError):
        keyring.open_sealed(sealed_path)


def test_an_unlocked_keyring_opens_files_sealed_to_its_vault(
    sealed_path: Path, key: VaultPrivateKey
) -> None:
    keyring = VaultKeyring()
    keyring.unlock(key)

    sealed = keyring.open_sealed(sealed_path)
    try:
        assert keyring.key_id == key.public_key.key_id
        assert sealed.read_all() == CONTENT
    finally:
        sealed.close()


def test_locking_revokes_every_open_file_including_decrypted_chunks(
    sealed_path: Path, key: VaultPrivateKey
) -> None:
    keyring = VaultKeyring()
    keyring.unlock(key)
    sealed = keyring.open_sealed(sealed_path)
    view = sealed.open_content()
    assert view.read(100) == CONTENT[:100]  # Leaves a decrypted chunk cached.

    assert keyring.lock() == 1

    assert sealed.is_revoked
    with pytest.raises(VaultLockedError):
        sealed.read(0, 10)
    view.seek(0)
    with pytest.raises(VaultLockedError):
        view.read(10)


def test_lock_listeners_run_after_the_key_is_gone(sealed_path: Path, key: VaultPrivateKey) -> None:
    keyring = VaultKeyring()
    keyring.unlock(key)
    seen: list[bool] = []

    def broken() -> None:
        raise RuntimeError("listener bug")

    keyring.add_lock_listener(broken)
    remove = keyring.add_lock_listener(lambda: seen.append(keyring.is_unlocked))

    keyring.lock()
    # Locking an already-locked, empty keyring tells nobody anything.
    keyring.lock()

    assert seen == [False]
    remove()
    keyring.unlock(key)
    keyring.lock()
    assert seen == [False]


def test_unlocking_another_vault_revokes_files_of_the_first(
    sealed_path: Path, key: VaultPrivateKey
) -> None:
    keyring = VaultKeyring()
    keyring.unlock(key)
    sealed = keyring.open_sealed(sealed_path)
    locks: list[int] = []
    keyring.add_lock_listener(lambda: locks.append(1))

    keyring.unlock(key)  # Same vault: nothing changes.
    assert not sealed.is_revoked
    keyring.unlock(VaultPrivateKey.generate())

    assert sealed.is_revoked
    assert locks == [1]
    assert keyring.is_unlocked


def test_a_file_whose_open_races_a_lock_is_revoked_not_returned(
    sealed_path: Path, key: VaultPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring = VaultKeyring()
    keyring.unlock(key)
    opened: list[SealedFile] = []
    real_open = SealedFile.open

    def open_then_lock(path: Path, private_key: VaultPrivateKey) -> SealedFile:
        sealed = real_open(path, private_key)
        opened.append(sealed)
        keyring.lock()
        return sealed

    monkeypatch.setattr(keyring_module.SealedFile, "open", staticmethod(open_then_lock))

    with pytest.raises(VaultLockedError):
        keyring.open_sealed(sealed_path)
    assert opened[0].is_revoked


@pytest.mark.skipif(not hasattr(os, "pread"), reason="Windows reads through short-lived handles.")
def test_a_dropped_reader_releases_its_descriptor(sealed_path: Path, key: VaultPrivateKey) -> None:
    sealed = SealedFile.open(sealed_path, key)
    release = sealed._source._release_fd
    assert release is not None and release.alive

    del sealed
    gc.collect()

    assert not release.alive
