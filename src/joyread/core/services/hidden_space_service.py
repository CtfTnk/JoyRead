"""Hidden Space: hide books and collections, each hidden book encrypted.

A Library is in one of four states (:class:`HiddenSpaceState`):

* ``NONE`` -- never set up.
* ``LEGACY`` -- set up by a JoyRead from before the vault: a PBKDF2 password
  hash in settings gated a visibility flag, and the hidden books are still
  plaintext. Nothing can be hidden or shown until the upgrade below.
* ``LOCKED`` / ``UNLOCKED`` -- a vault exists (``VaultService``). Hidden books
  are sealed to it by ``SealingService``, and their pages and covers can be
  read only while it is unlocked.

Every hide and unhide goes through sealing, so there is no way to hide a book
without encrypting it. A ``LEGACY`` Library can still revert (after its old
password) or reset; both are ways out that do not need a vault.

**The upgrade.** A ``LEGACY`` Library keeps its old password: every password
the soft lock accepted (four or more letters and digits) is a valid vault
passphrase. :meth:`prepare_upgrade` checks it against the old hash once; then,
as with a new setup, nothing is stored until the recovery code is confirmed.
The old hash is cleared in the same step, because a PBKDF2 hash of the vault's
own passphrase would be the cheaper thing to attack.
"""

from __future__ import annotations

import base64
from enum import StrEnum
import hashlib
import logging
import secrets

from joyread.core.repositories.sqlite_sealing_repository import UnhideOutcome
from joyread.core.services.library_service import LibraryService
from joyread.core.services.sealing_service import DEFAULT_UNHIDE_WAIT_SECONDS, SealingService
from joyread.core.services.vault_service import RecoveryCodeDraft, VaultDraft, VaultService
from joyread.core.vault import VaultError
from joyread.infrastructure.config.settings_store import AppSettings, SettingsStore


logger = logging.getLogger(__name__)


# The earlier password's PBKDF2 work factor, needed only to check that
# password once during the upgrade (and before a legacy revert).
_PBKDF2_ITERATIONS = 200_000

#: How long reset waits for a running seal or unseal to stop.
_RESET_DRAIN_SECONDS = 30.0


class HiddenSpaceState(StrEnum):
    NONE = "none"
    LEGACY = "legacy"
    LOCKED = "locked"
    UNLOCKED = "unlocked"


class HiddenSpacePasswordError(ValueError):
    """Password failure with a stable reason for presentation-layer localization."""

    def __init__(self, message: str, *, code: str = "unknown") -> None:
        super().__init__(message)
        self.code = code


class HiddenSpaceService:
    def __init__(
        self,
        settings_store: SettingsStore,
        library_service: LibraryService,
        *,
        vault: VaultService | None = None,
        sealing: SealingService | None = None,
    ) -> None:
        self._settings_store = settings_store
        self._library_service = library_service
        self._vault = vault
        self._sealing = sealing

    @property
    def _settings(self) -> AppSettings:
        return self._settings_store.load()

    @property
    def has_legacy_password(self) -> bool:
        """Whether settings still hold the earlier password. See :attr:`state`."""

        return self._settings.hidden_space_password_hash is not None

    @property
    def state(self) -> HiddenSpaceState:
        if self._vault is not None and self._vault.has_vault:
            return HiddenSpaceState.UNLOCKED if self._vault.is_unlocked else HiddenSpaceState.LOCKED
        if self.has_legacy_password:
            return HiddenSpaceState.LEGACY
        return HiddenSpaceState.NONE

    @property
    def has_vault(self) -> bool:
        return self._vault is not None and self._vault.has_vault

    @property
    def sealing(self) -> SealingService | None:
        return self._sealing

    # ------------------------------------------------------------------
    # Vault setup, the legacy upgrade, and unlocking. Slow calls (scrypt)
    # are marked; run them off the UI thread.

    def prepare_vault(self, passphrase: str, confirm: str) -> VaultDraft:
        """Slow. Build a new vault to confirm; nothing is stored yet."""

        if self.state is HiddenSpaceState.LEGACY:
            raise VaultError("Upgrade the existing Hidden Space instead.", code="upgrade_required")
        return self._require_vault().prepare(passphrase, confirm)

    def create_vault(self, draft: VaultDraft, typed_code: str) -> int:
        """Store the vault once its recovery code is confirmed. Returns seals queued."""

        self._require_vault().create(draft, typed_code)
        self._settings_store.update(show_hidden_collection=True)
        return self._require_sealing().queue_hidden_books()

    def prepare_upgrade(self, old_password: str) -> VaultDraft:
        """Slow. Check the legacy password once and build a vault keeping it."""

        if self.state is not HiddenSpaceState.LEGACY:
            raise VaultError("There is no earlier Hidden Space to upgrade.", code="not_legacy")
        if not self.verify(old_password):
            raise HiddenSpacePasswordError("Current password is incorrect.", code="incorrect_current")
        return self._require_vault().prepare(old_password, old_password)

    def complete_upgrade(self, draft: VaultDraft, typed_code: str) -> int:
        """Store the vault, retire the legacy hash, and queue every hidden book's seal."""

        self._require_vault().create(draft, typed_code)
        self._settings_store.update(hidden_space_password_hash=None, hidden_space_password_salt=None)
        queued = self._require_sealing().queue_hidden_books()
        logger.info(
            "Hidden Space upgraded to an encrypted vault",
            extra={"event": "hidden_space.upgraded", "category": "vault", "count": queued},
        )
        return queued

    def unlock(self, passphrase: str) -> None:
        """Slow. Unlock the vault; failed seals and waiting unseals resume."""

        self._require_vault().unlock(passphrase)
        self._resume_after_unlock()

    def recover(self, typed_code: str, new_passphrase: str, confirm: str) -> None:
        """Slow. Unlock with the recovery code and set a new passphrase."""

        self._require_vault().recover(typed_code, new_passphrase, confirm)
        self._resume_after_unlock()

    def lock(self) -> int:
        if self._vault is None:
            return 0
        return self._vault.lock()

    def change_passphrase(self, current: str, new_passphrase: str, confirm: str) -> None:
        """Slow."""

        self._require_vault().change_passphrase(current, new_passphrase, confirm)

    def prepare_recovery_code(self, passphrase: str) -> RecoveryCodeDraft:
        """Slow."""

        return self._require_vault().prepare_recovery_code(passphrase)

    def replace_recovery_code(self, draft: RecoveryCodeDraft, typed_code: str) -> None:
        self._require_vault().replace_recovery_code(draft, typed_code)

    def _resume_after_unlock(self) -> None:
        if self._sealing is not None:
            self._sealing.retry_failed()
            self._sealing.wake()

    # ------------------------------------------------------------------
    # The earlier password, kept only to be checked once.
    #
    # There is deliberately no password hint. A hint is stored in plaintext
    # beside the hash and shown on the lock screen to whoever is at the
    # keyboard, so it only helps the people the password is meant to stop.

    def verify(self, password: str) -> bool:
        """Check the earlier password of a ``LEGACY`` Library. Slow (PBKDF2)."""

        settings = self._settings
        stored_hash = settings.hidden_space_password_hash
        stored_salt = settings.hidden_space_password_salt
        if stored_hash is None or stored_salt is None:
            return False
        try:
            salt = base64.b64decode(stored_salt.encode("ascii"))
        except (ValueError, TypeError):
            logger.warning("Hidden Space salt could not be decoded")
            return False
        candidate = _hash_password(password, salt)
        # Constant-time compare. PBKDF2 already provides cost; this guards
        # against timing oracles on the hex string itself.
        return secrets.compare_digest(candidate, stored_hash)

    def set_show_hidden_collection(self, enabled: bool) -> None:
        self._settings_store.update(show_hidden_collection=bool(enabled))

    # ------------------------------------------------------------------
    # Book / collection mutations

    def hide_books(self, book_uuids: tuple[str, ...]) -> tuple[str, ...]:
        """Hide books and queue their encryption; returns the books queued."""

        self._require_vault_exists()
        return self._require_sealing().hide_books(book_uuids)

    def unhide_books(
        self,
        book_uuids: tuple[str, ...],
        *,
        wait_seconds: float = DEFAULT_UNHIDE_WAIT_SECONDS,
    ) -> dict[str, UnhideOutcome]:
        """Unhide books. A sealed one stays in Hidden until it is decrypted."""

        self._require_vault_exists()
        return self._require_sealing().unhide_books(book_uuids, wait_seconds=wait_seconds)

    def set_collection_hidable(self, collection_uuid: str, hidable: bool) -> None:
        self._library_service.set_collection_hidable(collection_uuid, hidable)

    def revert_all(self, *, wait_seconds: float = DEFAULT_UNHIDE_WAIT_SECONDS) -> dict[str, UnhideOutcome]:
        """Unhide everything and make every hidable collection a normal one.

        Reverting clears the per-row visibility flags but leaves the password,
        or the vault, and the display toggle alone -- the user explicitly asked
        for this so the feature stays "armed" after an undo. With a vault each
        book is decrypted first and leaves Hidden as its unseal commits.
        """

        if not self.has_vault:
            self._library_service.revert_hidden_state()
            logger.info("Hidden Space: reverted all hidden books + hidable collections")
            return {}
        outcomes = self.unhide_books(tuple(self._library_service.list_hidden_book_ids()), wait_seconds=wait_seconds)
        self._library_service.clear_hidable_collections()
        logger.info(
            "Hidden Space: revert queued",
            extra={"event": "hidden_space.reverted", "category": "vault", "count": len(outcomes)},
        )
        return outcomes

    def reset_and_erase(self) -> None:
        # Wipe everything related to Hidden Space:
        #   1. Delete every is_hidden=1 book (delete_book handles cascade
        #      cleanup of book_files, covers, recent, progress, bookmarks,
        #      sealed files and whatever a seal or unseal job had written).
        #   2. Delete every is_hidable=1 collection (collection_books rows
        #      cascade away via ON DELETE CASCADE).
        #   3. Delete the vault, if any, and clear the legacy password and the
        #      display toggle so the feature returns to its uninitiated state.
        sealing = self._sealing if self.has_vault else None
        if sealing is not None:
            # A job writing a book's files while that book is deleted would
            # leave its output behind; let the worker stop between steps.
            sealing.pause()
            sealing.wait_idle(_RESET_DRAIN_SECONDS)
        try:
            hidden_book_ids = self._library_service.list_hidden_book_ids()
            hidable_collection_ids = self._library_service.list_hidable_collection_ids()
            logger.info(
                "Hidden Space reset: deleting %d hidden books and %d hidable collections",
                len(hidden_book_ids),
                len(hidable_collection_ids),
            )
            if hidden_book_ids:
                self._library_service.delete_books(tuple(hidden_book_ids))
            for collection_id in hidable_collection_ids:
                self._library_service.delete_collection(collection_id)
            if self._vault is not None and self._vault.has_vault:
                self._vault.delete()
            self._settings_store.update(
                hidden_space_password_hash=None,
                hidden_space_password_salt=None,
                show_hidden_collection=False,
            )
        finally:
            if sealing is not None:
                sealing.resume()
        logger.info("Hidden Space reset complete; feature back to uninitiated state")

    # ------------------------------------------------------------------
    # Internal helpers

    def _require_vault(self) -> VaultService:
        if self._vault is None:
            raise VaultError("Hidden Space encryption is unavailable in this runtime.", code="no_vault")
        return self._vault

    def _require_sealing(self) -> SealingService:
        if self._sealing is None:
            raise VaultError("Hidden Space encryption is unavailable in this runtime.", code="no_vault")
        return self._sealing

    def _require_vault_exists(self) -> None:
        # Hiding is encrypting: without a vault there is nothing to seal to,
        # and a hidden-but-plaintext book is what the vault replaced.
        if not self.has_vault:
            raise VaultError("Set up Hidden Space first.", code="no_vault")


def _hash_password(password: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
    )
    return digest.hex()
