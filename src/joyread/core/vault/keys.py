"""The vault key pair.

A vault is one X25519 key pair per Library. The public half seals books and
covers whenever the user hides one, with no prompt. The private half exists on
disk only inside unlock slots (``slots.py``), and in memory only while the
vault is unlocked.

Python cannot reliably wipe memory: ``bytes`` are immutable and the
cryptography library keeps its own copies. So this module does not pretend to
zeroize. It does keep key material out of ``repr`` and exception text, and it
gives callers one object to drop when the vault locks.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from Cryptodome.Protocol.DH import import_x25519_private_key, import_x25519_public_key
from Cryptodome.PublicKey import ECC


RAW_KEY_BYTES = 32
KEY_ID_BYTES = 16

#: Domain separation for the key id, so the id can never collide with a hash
#: of the same 32 bytes computed for any other purpose.
_KEY_ID_LABEL = b"joyread-vault-key-id-v1"

_CURVE = "Curve25519"


@dataclass(frozen=True)
class VaultPublicKey:
    """The raw 32-byte X25519 public key that seals books."""

    raw: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.raw, (bytes, bytearray)) or len(self.raw) != RAW_KEY_BYTES:
            raise ValueError("A vault public key is exactly 32 bytes.")
        object.__setattr__(self, "raw", bytes(self.raw))

    @property
    def key_id(self) -> bytes:
        """A short, non-secret fingerprint written into every sealed file.

        It lets a reader say "sealed to another vault" without attempting a
        key unwrap, and lets maintenance group files by vault while locked.
        """

        return hashlib.sha256(_KEY_ID_LABEL + self.raw).digest()[:KEY_ID_BYTES]

    def ecc_key(self) -> ECC.EccKey:
        return import_x25519_public_key(self.raw)


class VaultPrivateKey:
    """The unlocked private key. Hold it only while the vault is unlocked."""

    __slots__ = ("_key", "_public")

    def __init__(self, key: ECC.EccKey) -> None:
        if key.curve != _CURVE or not key.has_private():
            raise ValueError("A vault private key must be an X25519 private key.")
        self._key = key
        self._public = VaultPublicKey(key.public_key().export_key(format="raw"))

    @classmethod
    def generate(cls) -> VaultPrivateKey:
        return cls(ECC.generate(curve=_CURVE))

    @classmethod
    def from_raw(cls, raw: bytes) -> VaultPrivateKey:
        if len(raw) != RAW_KEY_BYTES:
            raise ValueError("A raw vault private key is exactly 32 bytes.")
        return cls(import_x25519_private_key(bytes(raw)))

    @property
    def public_key(self) -> VaultPublicKey:
        return self._public

    def raw(self) -> bytes:
        """The 32-byte private key, for wrapping into an unlock slot only."""

        return bytes(self._key.seed)

    def ecc_key(self) -> ECC.EccKey:
        return self._key

    def __repr__(self) -> str:
        return f"VaultPrivateKey(key_id={self._public.key_id.hex()})"


__all__ = ["KEY_ID_BYTES", "RAW_KEY_BYTES", "VaultPrivateKey", "VaultPublicKey"]
