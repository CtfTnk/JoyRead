"""Tag domain model and normalization helper."""

from __future__ import annotations

from dataclasses import dataclass


MAX_TAG_NAME_LENGTH = 32


@dataclass(frozen=True)
class Tag:
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
