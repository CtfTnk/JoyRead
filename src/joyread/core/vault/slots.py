"""Unlock slots: the vault private key, wrapped once per way of unlocking.

The private key never changes for the life of a vault. Each slot wraps it
under a key derived from one secret -- the passphrase, the recovery code, and
later an OS-keychain secret -- the way LUKS key slots work. Adding, removing or
changing an unlock method writes or deletes one slot. It never touches a
sealed book.

Two rules keep "later" cheap. Do not relax either:

* **Unknown slot types are skipped, not refused.** A newer JoyRead may add a
  keychain slot to a Library this version also opens, and this version must
  still unlock that Library with the passphrase. A known type with
  parameters this version will not run (an unknown version, or a cost outside
  the bounds below) is skipped the same way.
* **A slot opens only for its own record.** The wrap's associated data binds
  the vault id, slot id, slot type and every parameter, so a wrapped key
  copied into another row, or a parameter edited in place, fails to open.

There is no stored passphrase hash. A wrong passphrase shows up as the wrap
failing to authenticate, which tells an attacker nothing a hash would not.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import secrets
import struct
import unicodedata
from typing import Callable, Iterable, Mapping
from uuid import uuid4

from Cryptodome.Cipher import ChaCha20_Poly1305
from Cryptodome.Hash import SHA256
from Cryptodome.Protocol.KDF import HKDF

from joyread.core.vault.errors import PassphraseRuleError, UnlockFailedError
from joyread.core.vault.keys import RAW_KEY_BYTES, VaultPrivateKey, VaultPublicKey
from joyread.core.vault.recovery import RecoveryCode


SLOT_TYPE_PASSPHRASE = "passphrase"
SLOT_TYPE_RECOVERY = "recovery"

#: Four characters, the floor of the soft Hidden Space password this vault
#: replaces: people who only want books out of casual sight may keep a short
#: password, and every existing password is also a valid passphrase. Length,
#: not character classes, is what slows an offline guesser. At about 175 ms
#: per guess, eight cores try every 4-character letters-and-digits password
#: in about four days and every 4-digit PIN in under four minutes, so a short
#: passphrase stops casual snooping, not someone who copies the Library.
#: Setup must say so.
PASSPHRASE_MIN_LENGTH = 4
#: Below this, setup cautions that the passphrase only stops casual snooping.
#: Twelve letters and digits put an offline search out of reach of a desktop.
PASSPHRASE_RECOMMENDED_LENGTH = 12

_SLOT_VERSION = 1
_AAD_LABEL = b"joyread-vault-slot-v1"
_RECOVERY_INFO = b"joyread-vault-recovery-v1"
_SALT_BYTES = 16
_NONCE_BYTES = 12
_WRAP_KEY_BYTES = 32
_TAG_BYTES = 16

#: What a stored slot may ask for. A corrupt or hostile database row could
#: otherwise make one unlock attempt allocate gigabytes or spin for minutes.
_MIN_SCRYPT_N = 1 << 14
_MAX_SCRYPT_N = 1 << 20
_MAX_SCRYPT_R = 16
_MAX_SCRYPT_P = 4


def _scrypt_cost_allowed(n: int, r: int, p: int) -> bool:
    return (
        _MIN_SCRYPT_N <= n <= _MAX_SCRYPT_N
        and n & (n - 1) == 0
        and 1 <= r <= _MAX_SCRYPT_R
        and 1 <= p <= _MAX_SCRYPT_P
    )


@dataclass(frozen=True)
class ScryptCost:
    """scrypt work factors. The defaults cost about 175 ms and 128 MiB here."""

    n: int = 1 << 17
    r: int = 8
    p: int = 1

    def __post_init__(self) -> None:
        if not _scrypt_cost_allowed(self.n, self.r, self.p):
            raise ValueError("scrypt cost is outside the supported bounds.")

    @property
    def max_memory(self) -> int:
        # OpenSSL needs 128*r*(N+2) for V plus 128*r*p for B; a little slack
        # absorbs its bookkeeping across versions.
        return 128 * self.r * (self.n + self.p + 2) + (1 << 20)


DEFAULT_SCRYPT_COST = ScryptCost()


@dataclass(frozen=True)
class UnlockSlot:
    """One stored way to unlock. ``params`` holds only JSON scalars."""

    slot_id: str
    slot_type: str
    params: Mapping[str, object]
    wrapped_key: bytes

    def params_json(self) -> str:
        return _canonical_json(self.params)

    @classmethod
    def from_storage(
        cls, slot_id: str, slot_type: str, params_json: str, wrapped_key: bytes
    ) -> UnlockSlot:
        """Rebuild a slot read from the database.

        Unreadable parameters become an empty mapping rather than an error,
        so a damaged slot is skipped like an unknown one instead of stopping
        the other slots from being tried.
        """

        try:
            params = json.loads(params_json)
        except (TypeError, ValueError):
            params = {}
        if not isinstance(params, dict):
            params = {}
        return cls(slot_id, slot_type, params, bytes(wrapped_key))


def normalize_passphrase(passphrase: str) -> str:
    """NFC, so the same phrase typed on another OS or keyboard matches."""

    return unicodedata.normalize("NFC", passphrase)


def validate_new_passphrase(passphrase: str, confirm: str) -> str:
    """Return the passphrase to wrap with, or raise :class:`PassphraseRuleError`.

    Any characters are allowed, spaces included. The length counts characters
    after normalization, so a composed and a decomposed accent count once.
    """

    normalized = normalize_passphrase(passphrase or "")
    if len(normalized) < PASSPHRASE_MIN_LENGTH or not normalized.strip():
        raise PassphraseRuleError(
            f"A passphrase needs at least {PASSPHRASE_MIN_LENGTH} characters.",
            code="too_short",
        )
    if normalized != normalize_passphrase(confirm or ""):
        raise PassphraseRuleError("The passphrases do not match.", code="mismatch")
    return normalized


def create_passphrase_slot(
    vault_id: str,
    private_key: VaultPrivateKey,
    passphrase: str,
    *,
    cost: ScryptCost = DEFAULT_SCRYPT_COST,
    slot_id: str | None = None,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> UnlockSlot:
    """Wrap *private_key* under *passphrase*. Slow by design: run off the UI thread."""

    salt, nonce = random_bytes(_SALT_BYTES), random_bytes(_NONCE_BYTES)
    params = {
        "v": _SLOT_VERSION,
        "kdf": "scrypt",
        "n": cost.n,
        "r": cost.r,
        "p": cost.p,
        "salt": _b64(salt),
        "nonce": _b64(nonce),
    }
    wrap_key = _scrypt(normalize_passphrase(passphrase), salt, cost)
    return _wrap(vault_id, slot_id or str(uuid4()), SLOT_TYPE_PASSPHRASE, params, wrap_key, nonce, private_key)


def create_recovery_slot(
    vault_id: str,
    private_key: VaultPrivateKey,
    code: RecoveryCode,
    *,
    slot_id: str | None = None,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> UnlockSlot:
    """Wrap *private_key* under a recovery code. Fast: the code is 128 random bits."""

    salt, nonce = random_bytes(_SALT_BYTES), random_bytes(_NONCE_BYTES)
    params = {"v": _SLOT_VERSION, "kdf": "hkdf-sha256", "salt": _b64(salt), "nonce": _b64(nonce)}
    wrap_key = _recovery_key(code, salt)
    return _wrap(vault_id, slot_id or str(uuid4()), SLOT_TYPE_RECOVERY, params, wrap_key, nonce, private_key)


def unlock_with_passphrase(
    vault_id: str,
    slots: Iterable[UnlockSlot],
    passphrase: str,
    expected_public_key: VaultPublicKey,
) -> VaultPrivateKey:
    """Open the first passphrase slot *passphrase* fits. Slow: run off the UI thread."""

    normalized = normalize_passphrase(passphrase or "")
    for slot in slots:
        if slot.slot_type != SLOT_TYPE_PASSPHRASE:
            continue
        material = _scrypt_material(slot.params)
        if material is None:
            continue
        salt, nonce, cost = material
        private = _unwrap(vault_id, slot, _scrypt(normalized, salt, cost), nonce, expected_public_key)
        if private is not None:
            return private
    raise UnlockFailedError("The passphrase did not unlock this vault.")


def unlock_with_recovery_code(
    vault_id: str,
    slots: Iterable[UnlockSlot],
    code: RecoveryCode,
    expected_public_key: VaultPublicKey,
) -> VaultPrivateKey:
    for slot in slots:
        if slot.slot_type != SLOT_TYPE_RECOVERY:
            continue
        material = _hkdf_material(slot.params)
        if material is None:
            continue
        salt, nonce = material
        private = _unwrap(vault_id, slot, _recovery_key(code, salt), nonce, expected_public_key)
        if private is not None:
            return private
    raise UnlockFailedError("The recovery code did not unlock this vault.")


# -- internals ----------------------------------------------------------------


def _wrap(
    vault_id: str,
    slot_id: str,
    slot_type: str,
    params: Mapping[str, object],
    wrap_key: bytes,
    nonce: bytes,
    private_key: VaultPrivateKey,
) -> UnlockSlot:
    cipher = ChaCha20_Poly1305.new(key=wrap_key, nonce=nonce)
    cipher.update(_aad(vault_id, slot_id, slot_type, params))
    body, tag = cipher.encrypt_and_digest(private_key.raw())
    return UnlockSlot(slot_id, slot_type, dict(params), body + tag)


def _unwrap(
    vault_id: str,
    slot: UnlockSlot,
    wrap_key: bytes,
    nonce: bytes,
    expected_public_key: VaultPublicKey,
) -> VaultPrivateKey | None:
    if len(slot.wrapped_key) != RAW_KEY_BYTES + _TAG_BYTES:
        return None
    cipher = ChaCha20_Poly1305.new(key=wrap_key, nonce=nonce)
    cipher.update(_aad(vault_id, slot.slot_id, slot.slot_type, slot.params))
    try:
        raw = cipher.decrypt_and_verify(slot.wrapped_key[:-_TAG_BYTES], slot.wrapped_key[-_TAG_BYTES:])
        private = VaultPrivateKey.from_raw(raw)
    except ValueError:
        return None
    # The wrap authenticated, so this is a mismatch between rows, not a wrong
    # secret: a slot that unwraps a key for some other vault must not unlock
    # this one.
    if private.public_key != expected_public_key:
        return None
    return private


def _aad(vault_id: str, slot_id: str, slot_type: str, params: Mapping[str, object]) -> bytes:
    fields = (
        _AAD_LABEL,
        vault_id.encode("utf-8"),
        slot_id.encode("utf-8"),
        slot_type.encode("utf-8"),
        _canonical_json(params).encode("utf-8"),
    )
    # Length-prefixed, so no choice of ids can make two records encode alike.
    return b"".join(struct.pack(">I", len(field)) + field for field in fields)


def _canonical_json(params: Mapping[str, object]) -> str:
    return json.dumps(dict(params), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _scrypt_material(params: Mapping[str, object]) -> tuple[bytes, bytes, ScryptCost] | None:
    if params.get("v") != _SLOT_VERSION or params.get("kdf") != "scrypt":
        return None
    n, r, p = _int(params, "n"), _int(params, "r"), _int(params, "p")
    salt, nonce = _bytes(params, "salt"), _bytes(params, "nonce")
    if None in (n, r, p, salt, nonce) or not _scrypt_cost_allowed(n, r, p):
        return None
    if not _SALT_BYTES <= len(salt) <= 64 or len(nonce) != _NONCE_BYTES:
        return None
    return salt, nonce, ScryptCost(n, r, p)


def _hkdf_material(params: Mapping[str, object]) -> tuple[bytes, bytes] | None:
    if params.get("v") != _SLOT_VERSION or params.get("kdf") != "hkdf-sha256":
        return None
    salt, nonce = _bytes(params, "salt"), _bytes(params, "nonce")
    if salt is None or nonce is None:
        return None
    if not _SALT_BYTES <= len(salt) <= 64 or len(nonce) != _NONCE_BYTES:
        return None
    return salt, nonce


def _scrypt(passphrase: str, salt: bytes, cost: ScryptCost) -> bytes:
    secret = passphrase.encode("utf-8")
    scrypt = getattr(hashlib, "scrypt", None)
    if scrypt is not None:
        return scrypt(
            secret, salt=salt, n=cost.n, r=cost.r, p=cost.p,
            maxmem=cost.max_memory, dklen=_WRAP_KEY_BYTES,
        )
    # Only for a Python built without OpenSSL scrypt. Same function, same
    # output, so slots written either way open either way.
    from Cryptodome.Protocol.KDF import scrypt as cryptodome_scrypt  # pragma: no cover

    return cryptodome_scrypt(secret, salt, _WRAP_KEY_BYTES, cost.n, cost.r, cost.p)  # pragma: no cover


def _recovery_key(code: RecoveryCode, salt: bytes) -> bytes:
    return HKDF(code.value, _WRAP_KEY_BYTES, salt, SHA256, context=_RECOVERY_INFO)


def _int(params: Mapping[str, object], key: str) -> int | None:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _bytes(params: Mapping[str, object], key: str) -> bytes | None:
    value = params.get(key)
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return None


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


__all__ = [
    "DEFAULT_SCRYPT_COST",
    "PASSPHRASE_MIN_LENGTH",
    "SLOT_TYPE_PASSPHRASE",
    "SLOT_TYPE_RECOVERY",
    "ScryptCost",
    "UnlockSlot",
    "create_passphrase_slot",
    "create_recovery_slot",
    "normalize_passphrase",
    "unlock_with_passphrase",
    "unlock_with_recovery_code",
    "validate_new_passphrase",
]
