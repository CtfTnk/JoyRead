"""Hidden Space: soft visibility layer for books and collections.

This service owns the password + state lifecycle for the feature. It does
not encrypt anything — "hidden" is a UI/visibility flag persisted on the
``books`` and ``collections`` rows. The password gates the user-facing
*reveal* action; books on disk remain in cleartext.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets

from joyread.core.services.library_service import LibraryService
from joyread.infrastructure.config.settings_store import AppSettings, SettingsStore


logger = logging.getLogger(__name__)


# Password rule: 4+ chars, ASCII letters + digits only. The shorter floor
# matches the user spec; restricting the alphabet keeps the typing flow on
# the lock screen predictable across keyboard layouts.
_PASSWORD_RE = re.compile(r"^[A-Za-z0-9]{4,}$")

# PBKDF2 work factor. 200k iterations sits between OWASP's 2023 SHA-256
# floor (600k) and the older 100k baseline — comfortable for a desktop app
# where the unlock happens once per launch.
_PBKDF2_ITERATIONS = 200_000
_SALT_BYTES = 16


class HiddenSpacePasswordError(ValueError):
    """User-visible error from password setup, verify, or change."""


class HiddenSpaceService:
    def __init__(
        self,
        settings_store: SettingsStore,
        library_service: LibraryService,
    ) -> None:
        self._settings_store = settings_store
        self._library_service = library_service

    @property
    def _settings(self) -> AppSettings:
        return self._settings_store.load()

    @property
    def is_initialized(self) -> bool:
        return self._settings.hidden_space_password_hash is not None

    @property
    def hint(self) -> str | None:
        return self._settings.hidden_space_password_hint

    # ------------------------------------------------------------------
    # Password lifecycle

    def initialize(self, password: str, confirm: str, hint: str | None) -> None:
        if self.is_initialized:
            raise HiddenSpacePasswordError("Hidden Space is already set up.")
        self._validate_pair(password, confirm)
        salt = secrets.token_bytes(_SALT_BYTES)
        digest = _hash_password(password, salt)
        self._settings_store.update(
            hidden_space_password_hash=digest,
            hidden_space_password_salt=base64.b64encode(salt).decode("ascii"),
            hidden_space_password_hint=_normalize_hint(hint),
            show_hidden_collection=True,
        )
        logger.info("Hidden Space initialized")

    def verify(self, password: str) -> bool:
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

    def change_password(
        self,
        old_password: str,
        new_password: str,
        confirm: str,
        hint: str | None = None,
    ) -> None:
        if not self.is_initialized:
            raise HiddenSpacePasswordError("Hidden Space has not been set up yet.")
        if not self.verify(old_password):
            raise HiddenSpacePasswordError("Current password is incorrect.")
        self._validate_pair(new_password, confirm)
        salt = secrets.token_bytes(_SALT_BYTES)
        digest = _hash_password(new_password, salt)
        changes: dict[str, object] = {
            "hidden_space_password_hash": digest,
            "hidden_space_password_salt": base64.b64encode(salt).decode("ascii"),
        }
        if hint is not None:
            changes["hidden_space_password_hint"] = _normalize_hint(hint)
        self._settings_store.update(**changes)
        logger.info("Hidden Space password rotated")

    def set_show_hidden_collection(self, enabled: bool) -> None:
        self._settings_store.update(show_hidden_collection=bool(enabled))

    # ------------------------------------------------------------------
    # Book / collection mutations

    def hide_book(self, book_uuid: str) -> None:
        self._library_service.set_book_hidden(book_uuid, True)

    def unhide_book(self, book_uuid: str) -> None:
        self._library_service.set_book_hidden(book_uuid, False)

    def set_collection_hidable(self, collection_uuid: str, hidable: bool) -> None:
        self._library_service.set_collection_hidable(collection_uuid, hidable)

    def revert_all(self) -> None:
        # Reverting clears the per-row visibility flags but leaves the
        # password + display-toggle alone — the user explicitly asked for
        # this so the feature stays "armed" after an undo.
        self._library_service.revert_hidden_state()
        logger.info("Hidden Space: reverted all hidden books + hidable collections")

    def reset_and_erase(self) -> None:
        # Wipe everything related to Hidden Space:
        #   1. Delete every is_hidden=1 book (delete_book handles cascade
        #      cleanup of book_files, covers, recent, progress, bookmarks).
        #   2. Delete every is_hidable=1 collection (collection_books rows
        #      cascade away via ON DELETE CASCADE).
        #   3. Clear the password + hint + display toggle from settings so
        #      the feature returns to its uninitiated state.
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
        self._settings_store.update(
            hidden_space_password_hash=None,
            hidden_space_password_salt=None,
            hidden_space_password_hint=None,
            show_hidden_collection=False,
        )
        logger.info("Hidden Space reset complete; feature back to uninitiated state")

    # ------------------------------------------------------------------
    # Internal helpers

    def _validate_pair(self, password: str, confirm: str) -> None:
        if not _PASSWORD_RE.fullmatch(password or ""):
            raise HiddenSpacePasswordError(
                "Password must be at least 4 characters and contain only letters and digits."
            )
        if password != confirm:
            raise HiddenSpacePasswordError("Passwords do not match.")


def _hash_password(password: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
    )
    return digest.hex()


def _normalize_hint(hint: str | None) -> str | None:
    if hint is None:
        return None
    cleaned = hint.strip()
    return cleaned or None
