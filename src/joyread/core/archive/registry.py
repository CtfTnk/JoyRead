"""Which format backend reads which container suffix.

The archive service used to hard-code this as a dict. A registry lets the
composition root add a backend the archive core does not construct itself --
the sealed Hidden Space backend needs the process's vault keyring, which the
core has no business owning -- while the built-in formats stay where they are.

It is fixed once the service is built. Lookups therefore need no lock, and a
suffix can never change meaning while a session is reading through it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from joyread.core.archive.formats import ArchiveFormatBackend


class ArchiveBackendRegistry:
    """Suffix -> backend, plus each backend's confidentiality.

    A backend that sets ``confidential = True`` decrypts its sources in memory.
    The service then keeps every source it lists away from the disk: see
    ``ArchiveSource.confidential``. The attribute is read with ``is True`` so a
    mock or a truthy property cannot opt a backend in by accident.
    """

    def __init__(self) -> None:
        self._backends: dict[str, ArchiveFormatBackend] = {}

    def register(self, suffixes: Iterable[str], backend: ArchiveFormatBackend) -> None:
        for raw in suffixes:
            suffix = str(raw).lower()
            if not suffix.startswith(".") or len(suffix) < 2:
                raise ValueError(f"Archive suffixes start with a dot: {raw!r}")
            if suffix in self._backends:
                # Silently replacing a reader would change what an existing
                # file opens as. Two registrations for one suffix are a wiring
                # mistake, so they fail where they are made.
                raise ValueError(f"A backend is already registered for {suffix}")
            self._backends[suffix] = backend

    def get(self, suffix: str) -> ArchiveFormatBackend | None:
        return self._backends.get(suffix.lower())

    def is_confidential(self, suffix: str) -> bool:
        backend = self._backends.get(suffix.lower())
        return getattr(backend, "confidential", False) is True

    def __contains__(self, suffix: object) -> bool:
        return isinstance(suffix, str) and suffix.lower() in self._backends

    def __iter__(self) -> Iterator[str]:
        return iter(tuple(self._backends))


__all__ = ["ArchiveBackendRegistry"]
