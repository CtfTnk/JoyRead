"""Temporary P0 import path; the contract now lives in :mod:`open_policy`.

Remove this compatibility module and its P0 tests after P5 acceptance.
"""

from joyread.app.open_policy import (  # noqa: F401
    LibraryState,
    LibraryUiContract,
    OpenDisposition,
    OpenOrigin,
    RecoveryAction,
    decide_open,
    library_ui_contract,
)
