"""The two decisions that shape a canonical import: whether, and where."""

from __future__ import annotations

from pathlib import Path

from joyread.core.models.import_policy import (
    CanonicalImportPolicy,
    normalize_canonical_import_policy,
    should_convert,
)
from joyread.core.models.storage_layout import (
    STORAGE_KIND_CANONICAL,
    STORAGE_KIND_VERBATIM,
    is_content_addressed,
    storage_target,
)


def _convert(policy: CanonicalImportPolicy, suffix: str, nested: bool = False) -> bool:
    return should_convert(policy, suffix=suffix, has_nested_archives=nested)


def test_the_default_converts_slow_formats_and_leaves_flat_zips_alone() -> None:
    policy = CanonicalImportPolicy.EXPENSIVE_AND_NESTED

    assert _convert(policy, ".cb7") is True
    assert _convert(policy, ".rar") is True
    assert _convert(policy, ".cbz") is False


def test_nesting_alone_qualifies_even_for_a_cheap_format() -> None:
    """A nested tree can never be bulk-converted into the cache, so it pays
    sequential warmup on every open regardless of how cheap zip is."""

    assert _convert(CanonicalImportPolicy.EXPENSIVE_AND_NESTED, ".cbz", nested=True) is True


def test_never_and_always_ignore_the_archives_shape() -> None:
    assert _convert(CanonicalImportPolicy.NEVER, ".cb7", nested=True) is False
    assert _convert(CanonicalImportPolicy.ALWAYS, ".cbz") is True


def test_suffix_matching_is_case_insensitive() -> None:
    assert _convert(CanonicalImportPolicy.EXPENSIVE_AND_NESTED, ".CB7") is True


def test_an_unreadable_stored_policy_falls_back_to_the_default() -> None:
    """Settings are a JSON file a user can edit, so the value is untrusted."""

    assert normalize_canonical_import_policy("nonsense") is CanonicalImportPolicy.EXPENSIVE_AND_NESTED
    assert normalize_canonical_import_policy(None) is CanonicalImportPolicy.EXPENSIVE_AND_NESTED
    assert normalize_canonical_import_policy("Always") is CanonicalImportPolicy.ALWAYS
    assert normalize_canonical_import_policy("always") is CanonicalImportPolicy.ALWAYS


# ----------------------------------------------------------------------
# Layout
# ----------------------------------------------------------------------


def test_a_verbatim_file_is_addressed_by_its_bytes() -> None:
    target = storage_target(
        Path("/library/Books"),
        storage_kind=STORAGE_KIND_VERBATIM,
        stored_hash="abcdef0123",
        file_id="ffff1111",
        suffix=".cbz",
    )

    assert target == Path("/library/Books/ab/abcdef0123.cbz")


def test_a_canonical_file_is_addressed_by_its_row() -> None:
    """Its bytes cannot name it: two different sources repackage to the same
    ones, and a shared path would make deleting one book break the other."""

    target = storage_target(
        Path("/library/Books"),
        storage_kind=STORAGE_KIND_CANONICAL,
        stored_hash="abcdef0123",
        file_id="ffff1111",
        suffix=".cbz",
    )

    assert target == Path("/library/Books/ff/ffff1111.cbz")


def test_only_content_addressed_rows_may_be_relocated_or_merged() -> None:
    assert is_content_addressed(STORAGE_KIND_VERBATIM) is True
    assert is_content_addressed(STORAGE_KIND_CANONICAL) is False
