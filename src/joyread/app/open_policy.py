"""Qt-free opening policy shared by Library actions and file drops.

Launch IPC remains a read-only request. Import is selected only by an explicit
Library action or a Read-zone drop with the dedicated preference enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LibraryState(StrEnum):
    UNLOADED = "unloaded"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"
    SKIPPED = "skipped"


class OpenOrigin(StrEnum):
    OS_FILE = "os_file"
    LIBRARY_OPEN_FILE = "library_open_file"
    LIBRARY_BOOK = "library_book"
    LIBRARY_OPEN_AND_IMPORT = "library_open_and_import"
    LIBRARY_IMPORT = "library_import"
    DROP_READ = "drop_read"
    DROP_IMPORT = "drop_import"


class OpenDisposition(StrEnum):
    READ_ONLY = "read_only"
    READ_AND_IMPORT = "read_and_import"
    IMPORT_ONLY = "import_only"
    BLOCKED = "blocked"


class RecoveryAction(StrEnum):
    RETRY = "retry"
    SELECT_LIBRARY = "select_library"
    USE_DEFAULT = "use_default"
    SKIP_SESSION = "skip_session"


@dataclass(frozen=True)
class LibraryUiContract:
    state: LibraryState
    can_open_external_file: bool
    can_use_shelf: bool
    can_import: bool
    recovery_actions: tuple[RecoveryAction, ...]
    status_key: str


_RECOVERY_ACTIONS = (
    RecoveryAction.RETRY,
    RecoveryAction.SELECT_LIBRARY,
    RecoveryAction.USE_DEFAULT,
)


def library_ui_contract(state: LibraryState) -> LibraryUiContract:
    """Describe Library controls in a state without constructing the runtime."""

    return LibraryUiContract(
        state=state,
        can_open_external_file=True,
        can_use_shelf=state is LibraryState.READY,
        can_import=state is LibraryState.READY,
        recovery_actions=(
            (*_RECOVERY_ACTIONS, RecoveryAction.SKIP_SESSION)
            if state is LibraryState.FAILED
            else _RECOVERY_ACTIONS if state is LibraryState.SKIPPED else ()
        ),
        status_key=f"library_state.{state.value}",
    )


def decide_open(
    origin: OpenOrigin,
    *,
    library_state: LibraryState,
    import_on_read_drop: bool = False,
) -> OpenDisposition:
    """Resolve one opening request without consulting Qt or the database."""

    if origin in (OpenOrigin.OS_FILE, OpenOrigin.LIBRARY_OPEN_FILE):
        return OpenDisposition.READ_ONLY
    if origin is OpenOrigin.DROP_READ:
        if import_on_read_drop and library_state is LibraryState.READY:
            return OpenDisposition.READ_AND_IMPORT
        return OpenDisposition.READ_ONLY
    if library_state is not LibraryState.READY:
        return OpenDisposition.BLOCKED
    if origin is OpenOrigin.LIBRARY_BOOK:
        return OpenDisposition.READ_ONLY
    if origin is OpenOrigin.LIBRARY_OPEN_AND_IMPORT:
        return OpenDisposition.READ_AND_IMPORT
    if origin in (OpenOrigin.LIBRARY_IMPORT, OpenOrigin.DROP_IMPORT):
        return OpenDisposition.IMPORT_ONLY
    raise ValueError(f"Unknown open origin: {origin!r}")
