"""Unlock slots and recovery codes.

Slot tests use scrypt's smallest accepted cost so the suite stays fast. The
production default is pinned separately, so a test-speed shortcut cannot leak
into what users get.
"""

from __future__ import annotations

import pytest

from joyread.core.vault import (
    DEFAULT_SCRYPT_COST,
    PASSPHRASE_MIN_LENGTH,
    PassphraseRuleError,
    RecoveryCode,
    RecoveryCodeFormatError,
    ScryptCost,
    UnlockFailedError,
    UnlockSlot,
    VaultPrivateKey,
    create_passphrase_slot,
    create_recovery_slot,
    unlock_with_passphrase,
    unlock_with_recovery_code,
    validate_new_passphrase,
)


FAST = ScryptCost(n=1 << 14, r=8, p=1)
VAULT = "vault-0001"
PASSPHRASE = "correct horse battery staple"


@pytest.fixture(scope="module")
def key() -> VaultPrivateKey:
    return VaultPrivateKey.generate()


@pytest.fixture(scope="module")
def passphrase_slot(key: VaultPrivateKey) -> UnlockSlot:
    return create_passphrase_slot(VAULT, key, PASSPHRASE, cost=FAST)


def test_the_default_cost_is_not_weakened() -> None:
    assert (DEFAULT_SCRYPT_COST.n, DEFAULT_SCRYPT_COST.r, DEFAULT_SCRYPT_COST.p) == (1 << 17, 8, 1)
    assert PASSPHRASE_MIN_LENGTH == 12


def test_the_passphrase_unlocks_and_nothing_else_does(key: VaultPrivateKey, passphrase_slot: UnlockSlot) -> None:
    unlocked = unlock_with_passphrase(VAULT, [passphrase_slot], PASSPHRASE, key.public_key)
    assert unlocked.raw() == key.raw()

    for wrong in ("", PASSPHRASE + " ", PASSPHRASE.upper(), "correct horse battery stapl"):
        with pytest.raises(UnlockFailedError):
            unlock_with_passphrase(VAULT, [passphrase_slot], wrong, key.public_key)


def test_composed_and_decomposed_accents_are_the_same_passphrase(key: VaultPrivateKey) -> None:
    composed = "café au lait, s'il vous plaît"
    decomposed = "café au lait, s'il vous plâit".replace("plâit", "plaît")
    slot = create_passphrase_slot(VAULT, key, composed, cost=FAST)

    assert unlock_with_passphrase(VAULT, [slot], decomposed, key.public_key).raw() == key.raw()


def test_new_passphrase_rules() -> None:
    assert validate_new_passphrase("a" * 12, "a" * 12) == "a" * 12
    assert validate_new_passphrase("twelve chars", "twelve chars") == "twelve chars"
    assert validate_new_passphrase("\U0001f600" * 12, "\U0001f600" * 12)  # characters, not bytes

    with pytest.raises(PassphraseRuleError) as short:
        validate_new_passphrase("a" * 11, "a" * 11)
    assert short.value.code == "too_short"
    with pytest.raises(PassphraseRuleError) as blank:
        validate_new_passphrase(" " * 20, " " * 20)
    assert blank.value.code == "too_short"
    with pytest.raises(PassphraseRuleError) as mismatch:
        validate_new_passphrase("a" * 12, "a" * 13)
    assert mismatch.value.code == "mismatch"
    # A decomposed accent is one character, and matches its composed confirm.
    assert validate_new_passphrase("é" * 12, "é" * 12) == "é" * 12


def test_unknown_slot_types_are_skipped(key: VaultPrivateKey, passphrase_slot: UnlockSlot) -> None:
    future = UnlockSlot("slot-future", "device-pin", {"v": 9, "kdf": "keychain"}, b"\x00" * 48)

    assert unlock_with_passphrase(VAULT, [future, passphrase_slot], PASSPHRASE, key.public_key).raw() == key.raw()
    with pytest.raises(UnlockFailedError):
        unlock_with_passphrase(VAULT, [future], PASSPHRASE, key.public_key)


def test_a_slot_opens_only_for_its_own_record(key: VaultPrivateKey, passphrase_slot: UnlockSlot) -> None:
    moved = UnlockSlot("another-slot", passphrase_slot.slot_type, passphrase_slot.params, passphrase_slot.wrapped_key)
    edited = UnlockSlot(
        passphrase_slot.slot_id,
        passphrase_slot.slot_type,
        {**passphrase_slot.params, "r": 9},
        passphrase_slot.wrapped_key,
    )

    for slot in (moved, edited):
        with pytest.raises(UnlockFailedError):
            unlock_with_passphrase(VAULT, [slot], PASSPHRASE, key.public_key)
    with pytest.raises(UnlockFailedError):
        unlock_with_passphrase("vault-other", [passphrase_slot], PASSPHRASE, key.public_key)


def test_a_slot_for_another_key_does_not_unlock_this_vault(passphrase_slot: UnlockSlot) -> None:
    with pytest.raises(UnlockFailedError):
        unlock_with_passphrase(VAULT, [passphrase_slot], PASSPHRASE, VaultPrivateKey.generate().public_key)


def test_unsafe_stored_costs_are_skipped_not_run(key: VaultPrivateKey, passphrase_slot: UnlockSlot) -> None:
    hostile = UnlockSlot(
        passphrase_slot.slot_id,
        passphrase_slot.slot_type,
        {**passphrase_slot.params, "n": 1 << 30},
        passphrase_slot.wrapped_key,
    )
    with pytest.raises(UnlockFailedError):
        unlock_with_passphrase(VAULT, [hostile], PASSPHRASE, key.public_key)


def test_slots_survive_a_storage_round_trip(key: VaultPrivateKey, passphrase_slot: UnlockSlot) -> None:
    stored = UnlockSlot.from_storage(
        passphrase_slot.slot_id,
        passphrase_slot.slot_type,
        passphrase_slot.params_json(),
        passphrase_slot.wrapped_key,
    )
    assert unlock_with_passphrase(VAULT, [stored], PASSPHRASE, key.public_key).raw() == key.raw()

    damaged = UnlockSlot.from_storage("slot-x", "passphrase", "{not json", b"\x00" * 48)
    assert damaged.params == {}
    assert unlock_with_passphrase(VAULT, [damaged, stored], PASSPHRASE, key.public_key).raw() == key.raw()


def test_recovery_code_unlocks_the_same_key(key: VaultPrivateKey) -> None:
    code = RecoveryCode.generate()
    slot = create_recovery_slot(VAULT, key, code)

    assert unlock_with_recovery_code(VAULT, [slot], RecoveryCode.parse(code.formatted()), key.public_key).raw() == key.raw()
    with pytest.raises(UnlockFailedError):
        unlock_with_recovery_code(VAULT, [slot], RecoveryCode.generate(), key.public_key)


def test_recovery_code_display_and_forgiving_parse() -> None:
    code = RecoveryCode(bytes(range(16)))
    shown = code.formatted()
    groups = shown.split("-")

    assert len(groups) == 7 and all(len(group) == 4 for group in groups)
    assert not set(shown) & set("ILOU")
    assert RecoveryCode.parse(shown) == code
    assert RecoveryCode.parse(" ".join(groups).lower()) == code
    assert RecoveryCode.parse(shown.replace("0", "O").replace("1", "I")) == code
    assert "RecoveryCode(<hidden>)" == repr(code) == str(code)


def test_recovery_code_typos_are_caught_before_any_slot_is_tried() -> None:
    code = RecoveryCode(bytes(range(16)))
    compact = code.formatted().replace("-", "")
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    rejected = 0
    for symbol in alphabet:
        if symbol == compact[3]:
            continue
        try:
            RecoveryCode.parse(compact[:3] + symbol + compact[4:])
        except RecoveryCodeFormatError:
            rejected += 1
    # A 10-bit checksum misses about 1 substitution in 1024.
    assert rejected >= 29

    for text in ("", "ABCD", compact[:-1], compact + "0", compact[:5] + "!" + compact[6:]):
        with pytest.raises(RecoveryCodeFormatError):
            RecoveryCode.parse(text)
