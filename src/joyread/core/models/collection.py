"""Collection domain model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Collection:
    """A user-defined group of books shown in the shelf sidebar.

    ``is_hidable`` marks the special "hidable" type of collection: only
    hidable collections may mix hidden and non-hidden books, since hidden
    books are otherwise removed from every membership when the user hides
    them.
    """

    uuid: str
    name: str
    is_private: bool
    created_at: datetime
    updated_at: datetime
    is_hidable: bool = False
