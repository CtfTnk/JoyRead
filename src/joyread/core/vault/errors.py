"""Errors raised by the vault format and key code.

Every class carries a stable ``code`` so the presentation layer can pick a
localized message without parsing exception text -- the same contract
``HiddenSpacePasswordError`` keeps for the legacy password.

Messages never contain secrets, key material or decrypted bytes. They may name
a chunk index or a size, which says nothing about the content.
"""

from __future__ import annotations


class VaultError(Exception):
    """Base class for every vault failure."""

    code = "vault_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class VaultFormatError(VaultError):
    """Not a ``.jrl`` file, or one this version cannot read.

    Covers a wrong magic, an unknown suite or flag, a chunk size outside the
    accepted range, a file too short to hold its own geometry, and a trailer
    that does not describe the plaintext it ends.
    """

    code = "format"


class VaultIntegrityError(VaultError):
    """A chunk failed authentication.

    Tampering, truncation, reordered or duplicated chunks, and plain disk
    corruption all end here. Chunks authenticate independently, so only the
    read that touches a damaged chunk fails.
    """

    code = "integrity"


class VaultKeyMismatchError(VaultError):
    """The file was sealed to another vault, or its key wrap does not open.

    Distinct from :class:`VaultIntegrityError` because the remedy differs: the
    payload may be intact, it is just not this vault's to read.
    """

    code = "key_mismatch"


class VaultLockedError(VaultError):
    """The operation needs the private key and the vault is locked."""

    code = "locked"


class UnlockFailedError(VaultError):
    """No unlock slot opened with the secret given.

    Deliberately silent about *why*: a wrong passphrase, a damaged slot and a
    slot for another vault look the same to whoever is typing.
    """

    code = "unlock_failed"


class PassphraseRuleError(VaultError):
    """A new passphrase was refused before anything was stored.

    ``code`` is ``"too_short"`` or ``"mismatch"``.
    """

    code = "rules"


class RecoveryCodeFormatError(VaultError):
    """The text is not a well-formed recovery code: a typo, not a wrong code.

    Caught before any slot is tried, so the user is told to check what they
    typed rather than that the code is wrong.
    """

    code = "recovery_format"


class VaultOperationCancelled(VaultError):
    """A long seal or unseal stopped because its caller asked it to."""

    code = "cancelled"


__all__ = [
    "PassphraseRuleError",
    "RecoveryCodeFormatError",
    "UnlockFailedError",
    "VaultError",
    "VaultFormatError",
    "VaultIntegrityError",
    "VaultKeyMismatchError",
    "VaultLockedError",
    "VaultOperationCancelled",
]
