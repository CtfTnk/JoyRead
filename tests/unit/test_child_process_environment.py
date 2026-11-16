"""Programs JoyRead starts must see the host's search paths, not the bundle's."""

from __future__ import annotations

import pytest

from joyread.infrastructure.child_process_environment import (
    bundle_environment_applies,
    restore_child_process_environment,
)


BUNDLE = "/opt/joyread/_internal"


def test_unset_library_path_is_removed_with_the_qt_variables() -> None:
    # PyInstaller saves no LD_LIBRARY_PATH_ORIG when the user had none.
    env = {
        "LD_LIBRARY_PATH": BUNDLE,
        "QT_PLUGIN_PATH": f"{BUNDLE}/PySide6/Qt/plugins",
        "QML2_IMPORT_PATH": f"{BUNDLE}/PySide6/Qt/qml",
        "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
    }

    changed = restore_child_process_environment(env)

    assert changed == ("LD_LIBRARY_PATH", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH")
    assert env == {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}


def test_a_users_own_library_path_is_put_back() -> None:
    env = {
        "LD_LIBRARY_PATH": f"{BUNDLE}:/home/user/lib",
        "LD_LIBRARY_PATH_ORIG": "/home/user/lib",
    }

    changed = restore_child_process_environment(env)

    assert changed == ("LD_LIBRARY_PATH",)
    assert env == {"LD_LIBRARY_PATH": "/home/user/lib"}


def test_an_already_clean_environment_reports_nothing() -> None:
    env = {"PATH": "/usr/bin"}

    assert restore_child_process_environment(env) == ()
    assert env == {"PATH": "/usr/bin"}


@pytest.mark.parametrize(
    ("frozen", "platform", "expected"),
    [
        (True, "linux", True),
        # A source run has no bundle values; clearing a developer's own
        # QT_PLUGIN_PATH there would break their setup.
        (False, "linux", False),
        # macOS opens folders through NSWorkspace and Windows through the
        # shell; neither starts xdg-open with the bundle's variables.
        (True, "darwin", False),
        (True, "win32", False),
    ],
)
def test_only_the_frozen_linux_bundle_restores(frozen, platform, expected) -> None:
    assert bundle_environment_applies(frozen=frozen, platform=platform) is expected
