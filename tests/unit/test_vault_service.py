"""The Library vault: nothing is stored before the recovery code is confirmed."""

from __future__ import annotations

from pathlib import Path

import pytest

from joyread.core.repositories.sqlite_vault_repository import SqliteVaultRepository
from joyread.core.services.vault_service import (
    RecoveryCodeMismatchError,
    VaultMissingError,
    VaultService,
)
from joyread.core.vault import (
    PassphraseRuleError,
    RecoveryCode,
    RecoveryCodeFormatError,
    UnlockFailedError,
    VaultError,
    VaultKeyring,
    VaultPrivateKey,
)
from joyread.infrastructure.database import DatabaseInterpreter, DatabasePriority, apply_migrations
from tests.support.vault_library import FAST_SCRYPT


@pytest.fixture
def database(tmp_path: Path):
    database = DatabaseInterpreter(tmp_path / "joyread.sqlite3")
    database.execute(apply_migrations, DatabasePriority.CRITICAL)
    yield database
    database.close()


@pytest.fixture
def keyring() -> VaultKeyring:
    return VaultKeyring()


@pytest.fixture
def service(database: DatabaseInterpreter, keyring: VaultKeyring) -> VaultService:
    return VaultService(SqliteVaultRepository(database), keyring, scrypt_cost=FAST_SCRYPT)


def _create(service: VaultService, passphrase: str = "1234") -> RecoveryCode:
    draft = service.prepare(passphrase, passphrase)
    service.create(draft, draft.recovery_code.formatted())
    return draft.recovery_code


def test_preparing_stores_nothing_until_the_code_comes_back(
    service: VaultService, database: DatabaseInterpreter, keyring: VaultKeyring
) -> None:
    draft = service.prepare("1234", "1234")

    assert not service.has_vault and not keyring.is_unlocked
    assert "1234" not in repr(draft) and draft.recovery_code.formatted() not in repr(draft)

    wrong = RecoveryCode.generate()
    with pytest.raises(RecoveryCodeMismatchError):
        service.create(draft, wrong.formatted())
    with pytest.raises(RecoveryCodeFormatError):
        service.create(draft, draft.recovery_code.formatted()[:-1] + "!")
    assert not service.has_vault
    assert SqliteVaultRepository(database).get_vault() is None

    # Typed in lower case, without dashes: still the same code.
    service.create(draft, draft.recovery_code.formatted().replace("-", " ").lower())

    assert service.has_vault and service.is_unlocked
    assert service.public_key == draft.public_key
    assert keyring.key_id == draft.public_key.key_id


def test_the_passphrase_rules_apply_before_any_key_is_made(service: VaultService) -> None:
    with pytest.raises(PassphraseRuleError):
        service.prepare("abc", "abc")
    with pytest.raises(PassphraseRuleError):
        service.prepare("abcd", "abce")
    _create(service)
    with pytest.raises(VaultError):
        service.prepare("abcd", "abcd")


def test_unlocking_takes_the_passphrase_and_nothing_else(service: VaultService, keyring: VaultKeyring) -> None:
    _create(service, "1234")
    service.lock()
    assert not service.is_unlocked

    with pytest.raises(UnlockFailedError):
        service.unlock("12345")
    assert not keyring.is_unlocked

    service.unlock("1234")
    assert service.is_unlocked


def test_another_librarys_key_in_the_keyring_does_not_count_as_unlocked(
    service: VaultService, keyring: VaultKeyring
) -> None:
    _create(service)
    keyring.unlock(VaultPrivateKey.generate())

    assert not service.is_unlocked


def test_the_recovery_code_resets_a_forgotten_passphrase(service: VaultService) -> None:
    code = _create(service, "old-pass")
    service.lock()

    with pytest.raises(PassphraseRuleError):
        service.recover(code.formatted(), "new", "new")  # Checked before the code is spent.
    with pytest.raises(UnlockFailedError):
        service.recover(RecoveryCode.generate().formatted(), "new-pass", "new-pass")
    service.recover(code.formatted(), "new-pass", "new-pass")

    assert service.is_unlocked
    service.lock()
    with pytest.raises(UnlockFailedError):
        service.unlock("old-pass")
    service.unlock("new-pass")
    # The code itself stays valid.
    service.recover(code.formatted(), "third-pass", "third-pass")


def test_changing_the_passphrase_asks_for_the_current_one(service: VaultService) -> None:
    _create(service, "first")

    with pytest.raises(UnlockFailedError):
        service.change_passphrase("wrong", "second", "second")
    service.change_passphrase("first", "second", "second")
    service.lock()

    with pytest.raises(UnlockFailedError):
        service.unlock("first")
    service.unlock("second")


def test_a_new_recovery_code_replaces_the_old_only_once_confirmed(service: VaultService) -> None:
    old = _create(service, "pass")
    draft = service.prepare_recovery_code("pass")

    with pytest.raises(RecoveryCodeMismatchError):
        service.replace_recovery_code(draft, old.formatted())
    service.recover(old.formatted(), "pass", "pass")  # Still the live code.

    service.replace_recovery_code(draft, draft.recovery_code.formatted())

    with pytest.raises(UnlockFailedError):
        service.recover(old.formatted(), "pass", "pass")
    service.recover(draft.recovery_code.formatted(), "pass", "pass")


def test_deleting_locks_and_forgets_the_vault(
    service: VaultService, database: DatabaseInterpreter, keyring: VaultKeyring
) -> None:
    _create(service)

    service.delete()

    assert not service.has_vault and not keyring.is_unlocked
    assert SqliteVaultRepository(database).get_vault() is None
    with pytest.raises(VaultMissingError):
        service.unlock("1234")
