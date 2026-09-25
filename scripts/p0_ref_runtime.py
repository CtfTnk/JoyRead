"""Keep the disposable Windows P0Ref build separate from the installed JoyRead.

This PyInstaller runtime hook runs before the application imports its settings
store or claims its single-instance lock. An explicit JOYREAD_RUNTIME_DIR from
the measurement harness still wins. Remove this hook after P5 acceptance.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


if "JOYREAD_RUNTIME_DIR" not in os.environ:
    executable = Path(sys.executable).resolve()
    checkout = executable.parent.parent.parent
    if (checkout / "pyproject.toml").is_file():
        profile = checkout / "reports" / "p0-ref-profile"
    else:
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        profile = local_app_data / "JoyRead-P0Ref"
    os.environ["JOYREAD_RUNTIME_DIR"] = str(profile)
