"""Collection domain model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Collection:
    uuid: str
    name: str
    is_private: bool
    created_at: datetime
    updated_at: datetime
