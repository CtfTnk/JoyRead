"""Tag domain model and normalization helper."""

from __future__ import annotations

from dataclasses import dataclass


MAX_TAG_NAME_LENGTH = 32


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
        return self.name.strip().casefold()


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
