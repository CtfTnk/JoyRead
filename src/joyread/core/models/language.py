"""Language metadata used by book records and UI selectors."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    plain_text: str
    iso_code: str
