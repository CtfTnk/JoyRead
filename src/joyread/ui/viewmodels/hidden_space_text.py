"""User-facing text for Hidden Space failures, chosen by stable error code.

Vault and Hidden Space errors carry a ``code`` (see ``joyread.core.vault.errors``)
so their English developer messages never reach the screen. Several
operations share a code with different meanings -- a wrong passphrase and a
wrong recovery code both fail to unlock -- so callers can name the text for
``unlock_failed`` themselves.
"""

from __future__ import annotations

from joyread.core.models.vault import BookSealState
from joyread.infrastructure.i18n.locale_service import t


_ERROR_KEYS = {
    "unlock_failed": "hidden_space_error.incorrect_passphrase",
    "incorrect_current": "hidden_space_error.incorrect_password",
    "too_short": "hidden_space_error.too_short",
    "rules": "hidden_space_error.too_short",
    "mismatch": "hidden_space_error.mismatch",
    "recovery_format": "hidden_space_error.recovery_format",
    "recovery_mismatch": "hidden_space_error.recovery_mismatch",
    "locked": "hidden_space_error.locked",
    "format": "hidden_space_error.damaged",
    "integrity": "hidden_space_error.damaged",
    "key_mismatch": "hidden_space_error.other_vault",
    "already_setup": "hidden_space_error.already_setup",
    "no_vault": "hidden_space_error.unavailable",
}


def hidden_space_error_text(error: BaseException, *, unlock_failed_key: str | None = None) -> str:
    code = getattr(error, "code", None)
    if code == "unlock_failed" and unlock_failed_key is not None:
        return t(unlock_failed_key)
    key = _ERROR_KEYS.get(code) if isinstance(code, str) else None
    return t(key or "hidden_space_error.unexpected")


_SEAL_STATE_KEYS = {
    BookSealState.ENCRYPTING: "shelf.seal_encrypting",
    BookSealState.DECRYPTING: "shelf.seal_decrypting",
    BookSealState.ENCRYPT_FAILED: "shelf.seal_encrypt_failed",
    BookSealState.DECRYPT_FAILED: "shelf.seal_decrypt_failed",
}


def seal_state_text(state: BookSealState) -> str:
    """The short line a shelf card shows in place of its reading progress."""

    return t(_SEAL_STATE_KEYS[state])


def seal_state_failed(state: BookSealState | None) -> bool:
    return state in (BookSealState.ENCRYPT_FAILED, BookSealState.DECRYPT_FAILED)


__all__ = ["hidden_space_error_text", "seal_state_failed", "seal_state_text"]
