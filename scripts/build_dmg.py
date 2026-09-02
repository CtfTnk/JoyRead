"""Package a built ``dist/JoyRead.app`` into a distributable .dmg.

Run ``scripts/build_release.py`` first; this only wraps what that produced.

Signing is deliberately *not* delegated to PyInstaller here. Setting
``JOYREAD_CODESIGN_IDENTITY`` makes PyInstaller sign with ``--options runtime``,
and the Hardened Runtime requires every loaded library to share the main
executable's Team ID. An ad-hoc signature has no Team ID, so the app dies at
launch with "different Team IDs" before reaching Python. Hardened Runtime is
only needed for notarization, which an ad-hoc build cannot do anyway -- so the
app is built unsigned and sealed here without it.

The window layout comes from ``dmgbuild``, which writes the Finder ``.DS_Store``
itself through ``ds_store`` and ``mac_alias``. That is the whole reason it is
used in preference to ``create-dmg``: the layout is what makes a disk image look
deliberate rather than like a dumped folder, and ``create-dmg`` can only set it
by driving Finder over AppleScript -- which needs a logged-in GUI session, so it
cannot run in CI, and which is flaky enough that the tool ships a five-second
sleep to work around ``Can't get disk (-1728)``. Nothing here touches Finder.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import plistlib
import subprocess
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging" / "dmg"))

import background  # noqa: E402 - resolved through the path insert above.

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "dist" / "JoyRead.app"
VOLUME_ICON = ROOT / "src" / "joyread" / "ui" / "resources" / "icons" / "JoyRead.icns"


def app_version() -> str:
    plist = plistlib.loads((APP / "Contents" / "Info.plist").read_bytes())
    return str(plist["CFBundleShortVersionString"])


def dmg_settings(workspace: Path) -> dict:
    """The window, as one dict shared with the drawing that fills it.

    ``background.py`` owns the size and the two icon centres because the
    background has to be drawn to the same window it is displayed in -- Finder
    pins a background at its natural size and crops whatever does not fit.
    """
    # dmgbuild pairs a 1x and a 2x image into a HiDPI TIFF by itself, as long as
    # the second sits beside the first under an ``@2x`` name.
    one = background.render(workspace / "background.png", scale=1)
    background.render(workspace / "background@2x.png", scale=2)

    settings = {
        "format": "UDZO",
        "files": [str(APP)],
        "symlinks": {"Applications": "/Applications"},
        "background": str(one),
        # Where the window opens, in dmgbuild's coordinates -- which run
        # bottom-to-top, unlike create-dmg's --window-pos. The number looks
        # large for that reason: it places the title bar near the top of a
        # laptop display rather than near the bottom. Any absolute position is
        # a guess about someone else's screen; Finder clamps it to fit.
        "window_rect": ((200, 460), (background.WIDTH, background.HEIGHT)),
        "icon_size": 128,
        "text_size": 12,
        "icon_locations": {
            APP.name: background.APP_CENTRE,
            "Applications": background.APPLICATIONS_CENTRE,
        },
        "hide_extensions": [APP.name],
    }
    if VOLUME_ICON.is_file():
        settings["icon"] = str(VOLUME_ICON)
    return settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--identity",
        default="-",
        help="codesign identity; '-' (default) is ad-hoc. A Developer ID here "
             "still will not notarize on its own -- see docs/PACKAGING.md.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write the image here instead of dist/JoyRead-<version>-macos-arm64.dmg.",
    )
    args = parser.parse_args()

    if not APP.is_dir():
        raise SystemExit(f"No app bundle at {APP}. Run scripts/build_release.py first.")
    try:
        import dmgbuild
    except ImportError:  # pragma: no cover - a release-only dependency.
        raise SystemExit("dmgbuild is missing. pip install -e '.[release]'") from None

    # Seal the bundle *without* --options runtime; see the module docstring.
    subprocess.run(
        ["codesign", "--force", "--sign", args.identity, "--timestamp=none", str(APP)],
        check=True,
    )
    subprocess.run(["codesign", "--verify", "--strict", str(APP)], check=True)

    version = app_version()
    target = args.output or ROOT / "dist" / f"JoyRead-{version}-macos-arm64.dmg"
    target.unlink(missing_ok=True)

    with TemporaryDirectory(prefix="joyread-dmg-") as workspace_name:
        dmgbuild.build_dmg(
            str(target),
            f"JoyRead {version}",
            settings=dmg_settings(Path(workspace_name)),
        )

    subprocess.run(["hdiutil", "verify", str(target)], check=True)
    size_mb = target.stat().st_size / (1024 * 1024)
    print(f"{target}  ({size_mb:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
