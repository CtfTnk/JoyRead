"""Tag domain model and normalization helper."""

from __future__ import annotations

from dataclasses import dataclass


MAX_TAG_NAME_LENGTH = 32

# Upper bound on how many tags one library may hold.
#
# This is a performance ceiling, not a storage one. Every tag surface builds
# one chip widget per tag in the library rather than per tag on screen, so
# cost scales with the library. Measured on the tag browser (offscreen, warm
# romanizers): 1,000 tags open in 187ms and re-filter in 54ms; 5,000 in 891ms
# and 282ms; 10,000 in 1.8s and 587ms. 5,000 is the last scale where opening a
# tag dialog still stays under a second, and it is already far beyond a
# realistic library -- the design this UI comes from treats 60 tags as a
# crowded case.
#
# Raising this without making the browser virtualize its chips will make the
# tag dialogs slow, not merely large. See docs/technical/runtime-flows.md.
MAX_TAG_COUNT = 5000


@dataclass(frozen=True)
class Tag:
    """User-defined label that can be applied to any book.

    Two forms exist: ``name`` is the display form preserved for the UI;
    ``name_normalized`` is the case-folded form used for uniqueness checks
    and lookups (so "Manga" and "manga" are treated as the same tag).
    """

    tag_id: str
    name: str  # Display form. First character is upper-cased; rest is preserved.

    @property
    def name_normalized(self) -> str:
        return normalized_tag_key(self.name)


def normalized_tag_key(name: str) -> str:
    """Lookup form of *name*, for callers holding a bare string.

    Same rule as :attr:`Tag.name_normalized`, kept here so uniqueness checks
    outside a ``Tag`` instance cannot drift from it.
    """

    return (name or "").strip().casefold()


def normalize_tag_name(raw: str) -> str:
    """Trim, validate, and normalize a user-supplied tag name for display.

    Raises ``ValueError`` if the result is empty or longer than
    ``MAX_TAG_NAME_LENGTH``. Display form upper-cases only the first
    character so user-entered case is otherwise preserved.
    """

    if raw is None:
        raise ValueError("Tag name cannot be empty.")
    trimmed = str(raw).strip()
    if not trimmed:
        raise ValueError("Tag name cannot be empty.")
    if len(trimmed) > MAX_TAG_NAME_LENGTH:
        raise ValueError(f"Tag name cannot exceed {MAX_TAG_NAME_LENGTH} characters.")
    return trimmed[0].upper() + trimmed[1:]
