"""The Hidden Space vault: the ``.jrl`` format, the vault key and unlock slots.

This package is self-contained on purpose. It imports the standard library,
``pycryptodomex`` and itself -- no Qt and no other JoyRead module -- so the
format can be tested, specified and reused without the application, and so
nothing in it can quietly depend on UI state. ``tests/unit/test_vault_isolation.py``
fails if that changes. Storage, sealing jobs and UI live elsewhere and call in.

``docs/jrl-format.md`` is the normative description of the file format.
"""

from joyread.core.vault.container import (
    ContentType,
    SealedContentView,
    SealedFile,
    SealedHeader,
    SealedWriter,
    SealResult,
    padme,
    read_header,
    seal_bytes,
    seal_stream,
)
from joyread.core.vault.errors import (
    PassphraseRuleError,
    RecoveryCodeFormatError,
    UnlockFailedError,
    VaultError,
    VaultFormatError,
    VaultIntegrityError,
    VaultKeyMismatchError,
    VaultLockedError,
    VaultOperationCancelled,
)
from joyread.core.vault.keyring import VaultKeyring
from joyread.core.vault.keys import VaultPrivateKey, VaultPublicKey
from joyread.core.vault.recovery import RecoveryCode
from joyread.core.vault.slots import (
    DEFAULT_SCRYPT_COST,
    PASSPHRASE_MIN_LENGTH,
    SLOT_TYPE_PASSPHRASE,
    SLOT_TYPE_RECOVERY,
    ScryptCost,
    UnlockSlot,
    create_passphrase_slot,
    create_recovery_slot,
    normalize_passphrase,
    unlock_with_passphrase,
    unlock_with_recovery_code,
    validate_new_passphrase,
)

__all__ = [
    "ContentType",
    "DEFAULT_SCRYPT_COST",
    "PASSPHRASE_MIN_LENGTH",
    "PassphraseRuleError",
    "RecoveryCode",
    "RecoveryCodeFormatError",
    "SLOT_TYPE_PASSPHRASE",
    "SLOT_TYPE_RECOVERY",
    "ScryptCost",
    "SealResult",
    "SealedContentView",
    "SealedFile",
    "SealedHeader",
    "SealedWriter",
    "UnlockFailedError",
    "UnlockSlot",
    "VaultError",
    "VaultFormatError",
    "VaultIntegrityError",
    "VaultKeyMismatchError",
    "VaultKeyring",
    "VaultLockedError",
    "VaultOperationCancelled",
    "VaultPrivateKey",
    "VaultPublicKey",
    "create_passphrase_slot",
    "create_recovery_slot",
    "normalize_passphrase",
    "padme",
    "read_header",
    "seal_bytes",
    "seal_stream",
    "unlock_with_passphrase",
    "unlock_with_recovery_code",
    "validate_new_passphrase",
]
