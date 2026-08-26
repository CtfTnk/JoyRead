"""When an import repackages an archive instead of copying it.

Converting is not free -- it re-reads every page and writes a new file -- so it
is a user choice, and the default converts only the archives that keep paying a
cost for the rest of their life in the library.

Two things make an archive expensive to read page by page:

*Format.* ``7z`` and ``rar`` need a subprocess or a slower pure-Python backend
per read, where zip is a seek.

*Nesting.* This one is structural, not a matter of degree. ``ArchiveImageSession``
can bulk-convert a slow archive into the cache once, but only when the source is
a single path-backed file -- a nested tree is neither, so it falls back to
sequential warmup on **every** open. Converting at import is the only way that
book ever stops paying, which is why a nested zip qualifies even though zip is
otherwise cheap.
"""

from __future__ import annotations

from enum import StrEnum


class CanonicalImportPolicy(StrEnum):
    """How eagerly import repackages what it is given."""

    NEVER = "never"
    EXPENSIVE_AND_NESTED = "expensive_and_nested"
    ALWAYS = "always"


#: The default. Converts what is slow to read and leaves plain zips alone --
#: repackaging a flat CBZ costs a full rewrite to save nothing measurable.
DEFAULT_CANONICAL_IMPORT_POLICY = CanonicalImportPolicy.EXPENSIVE_AND_NESTED

CANONICAL_IMPORT_POLICY_LABELS: dict[CanonicalImportPolicy, str] = {
    CanonicalImportPolicy.NEVER: "Never",
    CanonicalImportPolicy.EXPENSIVE_AND_NESTED: "Expensive and nested formats",
    CanonicalImportPolicy.ALWAYS: "Always",
}

#: Formats whose every page read costs a subprocess or a slow decoder.
EXPENSIVE_ARCHIVE_SUFFIXES = frozenset({".7z", ".cb7", ".rar", ".cbr"})


def normalize_canonical_import_policy(value: object) -> CanonicalImportPolicy:
    text = str(value or DEFAULT_CANONICAL_IMPORT_POLICY.value).strip().lower()
    for policy, label in CANONICAL_IMPORT_POLICY_LABELS.items():
        if text in {policy.value, label.lower()}:
            return policy
    return DEFAULT_CANONICAL_IMPORT_POLICY


def should_convert(
    policy: CanonicalImportPolicy,
    *,
    suffix: str,
    has_nested_archives: bool,
) -> bool:
    """Whether an archive with this shape should be repackaged under *policy*."""

    if policy is CanonicalImportPolicy.NEVER:
        return False
    if policy is CanonicalImportPolicy.ALWAYS:
        return True
    return has_nested_archives or suffix.lower() in EXPENSIVE_ARCHIVE_SUFFIXES


__all__ = [
    "CANONICAL_IMPORT_POLICY_LABELS",
    "DEFAULT_CANONICAL_IMPORT_POLICY",
    "EXPENSIVE_ARCHIVE_SUFFIXES",
    "CanonicalImportPolicy",
    "normalize_canonical_import_policy",
    "should_convert",
]
