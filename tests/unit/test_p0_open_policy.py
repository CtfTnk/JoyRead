"""Executable target contract for later bootstrap and import phases."""

from __future__ import annotations

import json

import pytest

from joyread.app.launch.intent import LaunchIntent, encode_launch_intent
from joyread.app.p0_open_policy import (
    LibraryState,
    OpenDisposition,
    OpenOrigin,
    RecoveryAction,
    decide_open,
    library_ui_contract,
)


@pytest.mark.parametrize("state", tuple(LibraryState))
def test_os_open_is_read_only_in_every_library_state(state: LibraryState) -> None:
    assert decide_open(OpenOrigin.OS_FILE, library_state=state, import_on_read_drop=True) is OpenDisposition.READ_ONLY


@pytest.mark.parametrize("state", tuple(LibraryState))
@pytest.mark.parametrize("enabled", (False, True))
def test_read_drop_imports_only_when_enabled_and_library_ready(
    state: LibraryState, enabled: bool
) -> None:
    expected = (
        OpenDisposition.READ_AND_IMPORT
        if enabled and state is LibraryState.READY
        else OpenDisposition.READ_ONLY
    )
    assert decide_open(OpenOrigin.DROP_READ, library_state=state, import_on_read_drop=enabled) is expected


@pytest.mark.parametrize("state", tuple(LibraryState))
@pytest.mark.parametrize(
    ("origin", "ready_disposition"),
    (
        (OpenOrigin.LIBRARY_BOOK, OpenDisposition.READ_ONLY),
        (OpenOrigin.LIBRARY_OPEN_AND_IMPORT, OpenDisposition.READ_AND_IMPORT),
        (OpenOrigin.LIBRARY_IMPORT, OpenDisposition.IMPORT_ONLY),
        (OpenOrigin.DROP_IMPORT, OpenDisposition.IMPORT_ONLY),
    ),
)
def test_library_operations_require_ready(
    state: LibraryState, origin: OpenOrigin, ready_disposition: OpenDisposition
) -> None:
    expected = ready_disposition if state is LibraryState.READY else OpenDisposition.BLOCKED
    assert decide_open(origin, library_state=state) is expected


@pytest.mark.parametrize("state", tuple(LibraryState))
def test_external_file_picker_remains_readable_with_library_unavailable(state: LibraryState) -> None:
    assert decide_open(OpenOrigin.LIBRARY_OPEN_FILE, library_state=state) is OpenDisposition.READ_ONLY


@pytest.mark.parametrize("state", tuple(LibraryState))
def test_library_controls_and_recovery_actions(state: LibraryState) -> None:
    contract = library_ui_contract(state)
    assert contract.can_open_external_file
    assert contract.can_use_shelf is (state is LibraryState.READY)
    assert contract.can_import is (state is LibraryState.READY)
    assert contract.status_key == f"library_state.{state.value}"
    if state is LibraryState.FAILED:
        assert contract.recovery_actions == (
            RecoveryAction.RETRY,
            RecoveryAction.SELECT_LIBRARY,
            RecoveryAction.USE_DEFAULT,
            RecoveryAction.SKIP_SESSION,
        )
    elif state is LibraryState.SKIPPED:
        assert contract.recovery_actions == (
            RecoveryAction.RETRY,
            RecoveryAction.SELECT_LIBRARY,
            RecoveryAction.USE_DEFAULT,
        )
    else:
        assert contract.recovery_actions == ()


def test_launch_ipc_payload_still_has_no_import_instruction(tmp_path) -> None:
    payload = json.loads(encode_launch_intent(LaunchIntent.open_files((tmp_path / "book.cbz",))))
    assert set(payload) == {"version", "action", "paths"}
    assert payload["version"] == 1
    assert payload["action"] == "open_files"
