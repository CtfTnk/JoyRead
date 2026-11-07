"""Recovery codes: the way back in when the passphrase is forgotten.

A recovery code is 128 random bits, high-entropy enough that it needs no slow
key stretching. It is shown once, at setup. Losing both it and the passphrase
loses every hidden book; nobody, including JoyRead, can get them back.

Display form: Crockford base32 in seven groups of four, ``XXXX-XXXX-...``.
26 characters carry the code (130 bits, the last 2 always zero) and 2 carry a
10-bit checksum. The alphabet has no I, L, O or U, and parsing maps the
look-alikes back (I/L -> 1, O -> 0), so a code read off paper or a screen
survives the usual transcription slips. The checksum turns most remaining
typos into "check what you typed" rather than "wrong code".
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import secrets
from typing import Callable

from joyread.core.vault.errors import RecoveryCodeFormatError


CODE_BYTES = 16
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_VALUES = {symbol: index for index, symbol in enumerate(_ALPHABET)}
_LOOKALIKES = {"I": "1", "L": "1", "O": "0"}
_DATA_CHARS = 26
_CHECK_CHARS = 2
_GROUP = 4
_CHECK_LABEL = b"joyread-recovery-check-v1"
_SEPARATORS = " -_\t\r\n"


@dataclass(frozen=True)
class RecoveryCode:
    """A parsed, checksum-verified recovery code."""

    value: bytes

    def __post_init__(self) -> None:
        if len(self.value) != CODE_BYTES:
            raise ValueError("A recovery code is exactly 16 bytes.")

    @classmethod
    def generate(cls, random_bytes: Callable[[int], bytes] = secrets.token_bytes) -> RecoveryCode:
        return cls(random_bytes(CODE_BYTES))

    @classmethod
    def parse(cls, text: str) -> RecoveryCode:
        """Read a code the user typed, tolerating case, spacing and look-alikes."""

        compact = "".join(
            _LOOKALIKES.get(character, character)
            for character in text.upper()
            if character not in _SEPARATORS
        )
        if len(compact) != _DATA_CHARS + _CHECK_CHARS or any(
            character not in _VALUES for character in compact
        ):
            raise RecoveryCodeFormatError("That is not a complete recovery code.")
        number = 0
        for character in compact[:_DATA_CHARS]:
            number = (number << 5) | _VALUES[character]
        if number & 0b11:
            raise RecoveryCodeFormatError("That is not a valid recovery code.")
        code = cls((number >> 2).to_bytes(CODE_BYTES, "big"))
        if compact[_DATA_CHARS:] != _check_symbols(code.value):
            raise RecoveryCodeFormatError("The recovery code has a typo.")
        return code

    def formatted(self) -> str:
        number = int.from_bytes(self.value, "big") << 2
        data = "".join(
            _ALPHABET[(number >> (5 * (_DATA_CHARS - 1 - index))) & 0b11111]
            for index in range(_DATA_CHARS)
        )
        symbols = data + _check_symbols(self.value)
        return "-".join(symbols[index : index + _GROUP] for index in range(0, len(symbols), _GROUP))

    def __repr__(self) -> str:
        return "RecoveryCode(<hidden>)"

    __str__ = __repr__


def _check_symbols(value: bytes) -> str:
    digest = hashlib.sha256(_CHECK_LABEL + value).digest()
    bits = int.from_bytes(digest[:2], "big") >> 6
    return _ALPHABET[(bits >> 5) & 0b11111] + _ALPHABET[bits & 0b11111]


__all__ = ["CODE_BYTES", "RecoveryCode"]
