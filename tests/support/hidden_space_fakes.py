"""Hidden Space stand-ins for tests that are about the shelf, not the vault."""

from __future__ import annotations

import base64
import secrets

from joyread.core.repositories.sqlite_sealing_repository import UnhideOutcome
from joyread.core.services.hidden_space_service import _hash_password
from joyread.infrastructure.config.settings_store import SettingsStore


class FlagHiddenSpaceService:
    """Hides and unhides by flag at once, as if every seal finished instantly.

    The shelf's filtering rules (Hidden shelf, hidable collections, the
    favourite and collection side effects of hiding) do not depend on
    encryption, so shelf tests use this instead of a real vault.
    """

    sealing = None

    def __init__(self, repository) -> None:  # noqa: ANN001 - any BookRepository.
        self._repository = repository

    def hide_books(self, book_uuids: tuple[str, ...]) -> tuple[str, ...]:
        for book_uuid in book_uuids:
            self._repository.set_book_hidden(book_uuid, True)
        return tuple(book_uuids)

    def unhide_books(self, book_uuids: tuple[str, ...], **_kwargs: object) -> dict[str, UnhideOutcome]:
        for book_uuid in book_uuids:
            self._repository.set_book_hidden(book_uuid, False)
        return {book_uuid: UnhideOutcome.VISIBLE for book_uuid in book_uuids}


def seed_legacy_password(store: SettingsStore, password: str) -> None:
    """Leave settings as a JoyRead from before the vault would have: a LEGACY Library."""

    salt = secrets.token_bytes(16)
    store.update(
        hidden_space_password_hash=_hash_password(password, salt),
        hidden_space_password_salt=base64.b64encode(salt).decode("ascii"),
    )


__all__ = ["FlagHiddenSpaceService", "seed_legacy_password"]
