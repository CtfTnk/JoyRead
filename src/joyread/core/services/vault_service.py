"""The Library's Hidden Space vault: setup, unlock, recovery and passphrases.

Qt-free, so it is testable without the GUI. Every method that derives a key
from a passphrase runs scrypt (about 175 ms by design) and must be called off
the UI thread.

**A vault is never stored before its recovery code is confirmed.** Setup is two
steps: :meth:`VaultService.prepare` builds the key pair, the recovery code and
both unlock slots in memory and returns a :class:`VaultDraft` to show the code
from; :meth:`VaultService.create` writes only once the user has typed the code
back. Dropping a draft cancels setup, because nothing was written. Issuing a
new recovery code follows the same pattern.

The private key leaves an unlock slot only to go into the process-wide
:class:`~joyread.core.vault.VaultKeyring` (or into a draft that is about to be
stored). Nothing here keeps a copy, and nothing here logs a passphrase, a
recovery code or key material.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import secrets
from threading import Lock
from typing import Callable
from uuid import uuid4

from joyread.core.models.vault import VaultRecord
from joyread.core.repositories.sqlite_vault_repository import SqliteVaultRepository
from joyread.core.vault import (
    DEFAULT_SCRYPT_COST,
    SLOT_TYPE_PASSPHRASE,
    SLOT_TYPE_RECOVERY,
    RecoveryCode,
    ScryptCost,
    UnlockSlot,
    VaultError,
    VaultKeyring,
    VaultPrivateKey,
    VaultPublicKey,
    create_passphrase_slot,
    create_recovery_slot,
    unlock_with_passphrase,
    unlock_with_recovery_code,
    validate_new_passphrase,
)


logger = logging.getLogger(__name__)


class VaultMissingError(VaultError):
    """The operation needs a vault and this Library has none."""

    code = "no_vault"
    task_failure_kind = "expected"


class RecoveryCodeMismatchError(VaultError):
    """The code typed back at setup is well formed but is not the one shown."""

    code = "recovery_mismatch"
    task_failure_kind = "expected"


@dataclass(frozen=True, eq=False, repr=False)
class VaultDraft:
    """A vault waiting for its recovery code to be confirmed.

    Holds the private key, so keep it only while the setup screen is open.
    """

    vault_id: str
    private_key: VaultPrivateKey
    recovery_code: RecoveryCode
    slots: tuple[UnlockSlot, ...] = field(default=())

    @property
    def public_key(self) -> VaultPublicKey:
        return self.private_key.public_key

    def __repr__(self) -> str:
        return f"VaultDraft(vault_id={self.vault_id!r})"


@dataclass(frozen=True, eq=False, repr=False)
class RecoveryCodeDraft:
    """A replacement recovery code waiting to be confirmed."""

    vault_id: str
    recovery_code: RecoveryCode
    slot: UnlockSlot

    def __repr__(self) -> str:
        return f"RecoveryCodeDraft(vault_id={self.vault_id!r})"


class VaultService:
    """One Library's vault record and slots, unlocking into the shared keyring."""

    def __init__(
        self,
        repository: SqliteVaultRepository,
        keyring: VaultKeyring,
        *,
        scrypt_cost: ScryptCost = DEFAULT_SCRYPT_COST,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._repository = repository
        self._keyring = keyring
        self._scrypt_cost = scrypt_cost
        self._random_bytes = random_bytes
        self._lock = Lock()
        self._vault: VaultRecord | None = None
        self._loaded = False

    # -- state ----------------------------------------------------------------

    @property
    def keyring(self) -> VaultKeyring:
        return self._keyring

    @property
    def vault(self) -> VaultRecord | None:
        # Cached: the record changes only through this service, and callers
        # ask on every hide and every shelf state check.
        with self._lock:
            if not self._loaded:
                self._vault = self._repository.get_vault()
                self._loaded = True
            return self._vault

    @property
    def has_vault(self) -> bool:
        return self.vault is not None

    @property
    def public_key(self) -> VaultPublicKey | None:
        vault = self.vault
        return vault.public_key if vault is not None else None

    @property
    def is_unlocked(self) -> bool:
        """Whether the shared keyring holds *this* Library's key.

        The keyring is process-wide, so "unlocked" alone could describe a
        vault from another Library location.
        """

        vault = self.vault
        return vault is not None and self._keyring.key_id == vault.public_key.key_id

    # -- setup ----------------------------------------------------------------

    def prepare(self, passphrase: str, confirm: str) -> VaultDraft:
        """Build a vault in memory. Slow (scrypt); nothing is stored yet."""

        normalized = validate_new_passphrase(passphrase, confirm)
        if self.has_vault:
            raise VaultError("This Library already has a vault.", code="already_setup")
        vault_id = str(uuid4())
        private_key = VaultPrivateKey.generate()
        code = RecoveryCode.generate(self._random_bytes)
        slots = (
            create_passphrase_slot(
                vault_id, private_key, normalized, cost=self._scrypt_cost, random_bytes=self._random_bytes
            ),
            create_recovery_slot(vault_id, private_key, code, random_bytes=self._random_bytes),
        )
        return VaultDraft(vault_id, private_key, code, slots)

    def create(self, draft: VaultDraft, typed_code: str) -> VaultRecord:
        """Store *draft* once *typed_code* matches its recovery code, then unlock."""

        confirm_recovery_code(draft.recovery_code, typed_code)
        record = self._repository.create_vault(draft.public_key, draft.slots, vault_id=draft.vault_id)
        with self._lock:
            self._vault = record
            self._loaded = True
        self._keyring.unlock(draft.private_key)
        logger.info("Hidden Space vault created", extra={"event": "vault.created", "category": "vault"})
        return record

    # -- unlocking ------------------------------------------------------------

    def unlock(self, passphrase: str) -> None:
        """Open the passphrase slot into the keyring. Slow (scrypt)."""

        vault = self._require_vault()
        private_key = unlock_with_passphrase(
            vault.vault_id, self._repository.list_slots(vault.vault_id), passphrase, vault.public_key
        )
        self._keyring.unlock(private_key)
        logger.info("Hidden Space vault unlocked", extra={"event": "vault.unlocked", "category": "vault"})

    def recover(self, typed_code: str, new_passphrase: str, confirm: str) -> None:
        """Unlock with the recovery code and replace the forgotten passphrase.

        The new passphrase is checked first, so a typo in it never spends the
        recovery attempt. The recovery code itself stays valid.
        """

        normalized = validate_new_passphrase(new_passphrase, confirm)
        code = RecoveryCode.parse(typed_code)
        vault = self._require_vault()
        private_key = unlock_with_recovery_code(
            vault.vault_id, self._repository.list_slots(vault.vault_id), code, vault.public_key
        )
        slot = create_passphrase_slot(
            vault.vault_id, private_key, normalized, cost=self._scrypt_cost, random_bytes=self._random_bytes
        )
        self._repository.replace_slots(vault.vault_id, SLOT_TYPE_PASSPHRASE, slot)
        self._keyring.unlock(private_key)
        logger.info(
            "Hidden Space vault recovered with its recovery code",
            extra={"event": "vault.recovered", "category": "vault"},
        )

    def lock(self) -> int:
        """Drop the key from the keyring. Returns how many open files were revoked."""

        return self._keyring.lock()

    # -- changing secrets -----------------------------------------------------

    def change_passphrase(self, current: str, new_passphrase: str, confirm: str) -> None:
        """Rewrite the passphrase slot. Asks for *current* even while unlocked."""

        normalized = validate_new_passphrase(new_passphrase, confirm)
        vault = self._require_vault()
        private_key = unlock_with_passphrase(
            vault.vault_id, self._repository.list_slots(vault.vault_id), current, vault.public_key
        )
        slot = create_passphrase_slot(
            vault.vault_id, private_key, normalized, cost=self._scrypt_cost, random_bytes=self._random_bytes
        )
        self._repository.replace_slots(vault.vault_id, SLOT_TYPE_PASSPHRASE, slot)
        logger.info(
            "Hidden Space passphrase changed",
            extra={"event": "vault.passphrase_changed", "category": "vault"},
        )

    def prepare_recovery_code(self, passphrase: str) -> RecoveryCodeDraft:
        """Build a replacement recovery code; the old one works until it is confirmed."""

        vault = self._require_vault()
        private_key = unlock_with_passphrase(
            vault.vault_id, self._repository.list_slots(vault.vault_id), passphrase, vault.public_key
        )
        code = RecoveryCode.generate(self._random_bytes)
        slot = create_recovery_slot(vault.vault_id, private_key, code, random_bytes=self._random_bytes)
        return RecoveryCodeDraft(vault.vault_id, code, slot)

    def replace_recovery_code(self, draft: RecoveryCodeDraft, typed_code: str) -> None:
        confirm_recovery_code(draft.recovery_code, typed_code)
        self._repository.replace_slots(draft.vault_id, SLOT_TYPE_RECOVERY, draft.slot)
        logger.info(
            "Hidden Space recovery code replaced",
            extra={"event": "vault.recovery_replaced", "category": "vault"},
        )

    # -- removal --------------------------------------------------------------

    def delete(self) -> None:
        """Lock and forget the vault. Every sealed book becomes unreadable for good."""

        vault = self.vault
        self._keyring.lock()
        if vault is not None:
            self._repository.delete_vault(vault.vault_id)
        with self._lock:
            self._vault = None
            self._loaded = True
        logger.info("Hidden Space vault deleted", extra={"event": "vault.deleted", "category": "vault"})

    def _require_vault(self) -> VaultRecord:
        vault = self.vault
        if vault is None:
            raise VaultMissingError("This Library has no Hidden Space vault.")
        return vault


def confirm_recovery_code(expected: RecoveryCode, typed: str) -> None:
    """Raise unless *typed* is the code shown. Typos and wrong codes differ."""

    parsed = RecoveryCode.parse(typed)
    if not secrets.compare_digest(parsed.value, expected.value):
        raise RecoveryCodeMismatchError("That is not the recovery code shown.")


__all__ = [
    "RecoveryCodeDraft",
    "RecoveryCodeMismatchError",
    "VaultDraft",
    "VaultMissingError",
    "VaultService",
    "confirm_recovery_code",
]
