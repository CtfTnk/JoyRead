"""Guards on what the startup path is allowed to import.

Startup cost is dominated by imports, and the expensive ones arrive
transitively -- nobody writes ``import py7zr`` in the composition root. These
tests pin the two properties that keep that from happening again.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

import pytest


SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "joyread"

# The reader engine's transitive weight, measured at 435 ms against 4 ms for
# `core.file_types` alone. `natsort` and `py7zr` are the two largest.
_HEAVY_MODULES = ("natsort", "py7zr", "PIL", "PySide6", "pyzipper")

# What a secondary process must never import. `app_context` alone is ~683 ms,
# and it is the root the rest hangs from: reaching it pulls the window manager,
# MainWindow, the archive stack, natsort, py7zr and PIL in behind it.
_PRIMARY_ONLY_MODULES = (
    "joyread.app.app_context",
    "joyread.app.windows.manager",
    "joyread.ui.views.main_window",
    "joyread.core.reader",
    "joyread.core.archive",
    "natsort",
    "py7zr",
    "PIL",
)


def _run_probe(body: str) -> str:
    """Run a probe in a clean interpreter and return its stdout.

    Import boundaries can only be tested out-of-process: this test session has
    already imported everything, so ``sys.modules`` here proves nothing.
    """

    environment = dict(os.environ)
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    result = subprocess.run(
        [sys.executable, "-c", body],
        capture_output=True,
        text=True,
        check=True,
        timeout=180,
        env=environment,
    )
    return result.stdout.strip()


def test_importing_bootstrap_does_not_drag_in_the_primary_runtime() -> None:
    """``app/main.py`` imports this module before it knows its own role.

    A secondary process exists to forward one file path and exit; its useful
    work is ~5 ms of arbitration. Importing the primary runtime at module scope
    made it pay ~780 ms first, for objects it never constructs.
    """

    leaked = _run_probe(
        "import sys; import joyread.app.bootstrap; "
        f"print(','.join(name for name in {_PRIMARY_ONLY_MODULES!r} if name in sys.modules))"
    )

    assert leaked == "", (
        f"joyread.app.bootstrap imports {leaked} at module scope; "
        "primary-runtime modules belong in the function that uses them"
    )


def test_a_secondary_launch_never_builds_the_primary_runtime() -> None:
    """The boundary that matters is the one `run()` actually walks.

    Checking the import alone would miss a deferred import placed on the
    arbitration path rather than inside `_build_primary_runtime`.
    """

    probe = f"""
import sys
import joyread.app.bootstrap as bootstrap
from joyread.app.launch.single_instance_broker import InstanceRole


class _AlreadyRunningPrimary:
    def __init__(self, *args, **kwargs):
        pass

    def start(self, resolve_intent):
        # Resolving the intent is the secondary's real work; run it so the
        # probe covers `_resolve_secondary_intent` too.
        resolve_intent()
        return InstanceRole.SECONDARY

    def set_intent_handler(self, handler):
        pass

    def dispose(self):
        pass


bootstrap.SingleInstanceBroker = _AlreadyRunningPrimary
code = bootstrap.run(["joyread", "book.cbz"])
leaked = [name for name in {_PRIMARY_ONLY_MODULES!r} if name in sys.modules]
print(f"{{code}}|{{','.join(leaked)}}")
"""

    exit_code, _, leaked = _run_probe(probe).partition("|")

    assert exit_code == "0"
    assert leaked == "", f"a secondary launch imported {leaked}"


def test_center_window_on_launch_is_still_re_exported() -> None:
    """The lazy re-export has to stay a re-export.

    It is in ``__all__`` and callers import it from here; PEP 562 keeps that
    true without paying for ``windows.manager`` on every launch.
    """

    from joyread.app import bootstrap
    from joyread.app.windows.manager import center_window_on_launch

    assert bootstrap.center_window_on_launch is center_window_on_launch
    with pytest.raises(AttributeError):
        bootstrap.no_such_attribute  # noqa: B018


def test_file_types_stays_free_of_the_reader_stack() -> None:
    """``core.file_types`` is the cheap source of the extension constants.

    Its whole value is being importable without the reader engine behind it.
    Run in a subprocess because this process has already imported everything;
    ``sys.modules`` here would prove nothing.
    """

    probe = (
        "import sys; import joyread.core.file_types; "
        f"leaked = [name for name in {_HEAVY_MODULES!r} if name in sys.modules]; "
        "print(','.join(leaked))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )

    assert result.stdout.strip() == "", (
        f"joyread.core.file_types pulled in {result.stdout.strip()}; "
        "the constants module must stay dependency-free"
    )


@pytest.mark.parametrize(
    "module",
    ["app/bootstrap.py", "ui/views/main_window.py"],
)
def test_the_startup_path_takes_extensions_from_file_types(module: str) -> None:
    """``core.reader`` re-exports ``SUPPORTED_READER_EXTENSIONS`` identically.

    Importing it from there costs the whole reader engine, archive stack,
    natsort, py7zr and PIL to obtain a frozenset of seven suffixes -- on every
    launch, secondary processes included.
    """

    text = (SOURCE_ROOT / module).read_text(encoding="utf-8")
    reader_import = re.compile(
        r"^\s*from\s+joyread\.core\.reader\s+import\s+[^\n]*SUPPORTED_READER_EXTENSIONS",
        re.MULTILINE,
    )

    assert not reader_import.search(text)
    assert "from joyread.core.file_types import" in text


def test_the_reader_reexport_is_the_same_object_not_a_copy() -> None:
    """The cheap import has to be equivalent, or this is a behaviour change."""

    from joyread.core.file_types import SUPPORTED_READER_EXTENSIONS as cheap
    from joyread.core.reader import SUPPORTED_READER_EXTENSIONS as reexported

    assert cheap is reexported
