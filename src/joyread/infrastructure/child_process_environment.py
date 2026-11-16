"""Give programs JoyRead starts the host's environment, not the bundle's.

The frozen Linux build runs with variables that point inside the bundle: the
PyInstaller bootloader prepends ``_internal`` to ``LD_LIBRARY_PATH`` (saving any
original in ``LD_LIBRARY_PATH_ORIG``), and the PySide6 runtime hook sets
``QT_PLUGIN_PATH`` and ``QML2_IMPORT_PATH``. Every child inherits them. When
``QDesktopServices`` runs ``xdg-open`` to show the log folder, GNOME's ``gio``
then binds the bundle's older GLib, aborts on a missing symbol, and
``xdg-open`` falls back to the web browser. A Qt file manager would likewise
be pointed at JoyRead's Qt plugins.

JoyRead itself no longer needs them once ``QApplication`` exists: the dynamic
loader read ``LD_LIBRARY_PATH`` when the process started, Qt has cached its
plugin search paths (and PySide6's embedded ``qt.conf`` names the same
directory), and JoyRead loads no QML. So the restore runs right after the
application object is created. The bundled ``7zz`` is statically linked and
needs none of this.
"""

from __future__ import annotations

from collections.abc import MutableMapping
import os
import sys


_LIBRARY_PATH = "LD_LIBRARY_PATH"
# PyInstaller's bootloader keeps the pre-launch value here, and only when there
# was one; its absence means the variable was originally unset.
_LIBRARY_PATH_ORIGINAL = "LD_LIBRARY_PATH_ORIG"
# Set unconditionally by PyInstaller's PySide6 runtime hook, which overwrites
# rather than saves any user value, so there is nothing to restore them to.
_BUNDLE_ONLY_VARIABLES = ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH")


def bundle_environment_applies(
    *, frozen: bool | None = None, platform: str | None = None
) -> bool:
    """Whether this process is the frozen Linux bundle that injects these paths."""

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    resolved_platform = sys.platform if platform is None else platform
    return is_frozen and resolved_platform.startswith("linux")


def restore_child_process_environment(
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Undo the bundle's search-path variables; return the names changed.

    Callers decide whether the bundle applies; a development run has no
    bundle-injected values, and clearing a developer's own ``QT_PLUGIN_PATH``
    there would be wrong.
    """

    env = os.environ if environ is None else environ
    changed: list[str] = []
    original = env.pop(_LIBRARY_PATH_ORIGINAL, None)
    if original is not None:
        if env.get(_LIBRARY_PATH) != original:
            env[_LIBRARY_PATH] = original
            changed.append(_LIBRARY_PATH)
    elif env.pop(_LIBRARY_PATH, None) is not None:
        changed.append(_LIBRARY_PATH)
    for name in _BUNDLE_ONLY_VARIABLES:
        if env.pop(name, None) is not None:
            changed.append(name)
    return tuple(changed)
