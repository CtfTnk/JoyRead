"""Package a built ``dist/JoyRead.app`` into a distributable .dmg.

Run ``scripts/build_release.py`` first; this only wraps what that produced.

Signing is deliberately *not* delegated to PyInstaller here. Setting
``JOYREAD_CODESIGN_IDENTITY`` makes PyInstaller sign with ``--options runtime``,
and the Hardened Runtime requires every loaded library to share the main
executable's Team ID. An ad-hoc signature has no Team ID, so the app dies at
launch with "different Team IDs" before reaching Python. Hardened Runtime is
only needed for notarization, which an ad-hoc build cannot do anyway -- so the
app is built unsigned and sealed here without it.

Layout comes from ``create-dmg`` (``brew install create-dmg``), which drives
Finder over AppleScript to set the window size, icon positions and background.
That step needs a real GUI session: it cannot run headless or in CI, and
``--skip-jenkins`` would skip precisely the part worth having. Plain ``hdiutil``
leaves no ``.DS_Store`` at all, which is why the window used to open at Finder's
default size with the icons wherever it felt like putting them.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import plistlib
import shutil
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


def hidpi_background(workspace: Path) -> Path:
    """Render the window background as a 1x/2x TIFF Finder can use.

    ``create-dmg`` copies the background through untouched, so anything the
    Finder can display works -- and a plain PNG would be resampled and soft on
    every Mac sold in the last decade. ``tiffutil -cathidpicheck`` is the
    supported way to pair the two scales; a single PNG is the fallback if it
    ever stops being.
    """
    one = background.render(workspace / "background.png", scale=1)
    two = background.render(workspace / "background@2x.png", scale=2)
    combined = workspace / "background.tiff"
    result = subprocess.run(
        ["tiffutil", "-cathidpicheck", str(one), str(two), "-out", str(combined)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and combined.is_file():
        return combined
    print(f"tiffutil declined ({result.stderr.strip()}); falling back to 1x", file=sys.stderr)
    return one


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--identity",
        default="-",
        help="codesign identity; '-' (default) is ad-hoc. A Developer ID here "
             "still will not notarize on its own -- see docs/PACKAGING.md.",
    )
    args = parser.parse_args()

    if not APP.is_dir():
        raise SystemExit(f"No app bundle at {APP}. Run scripts/build_release.py first.")
    if shutil.which("create-dmg") is None:
        raise SystemExit("create-dmg is not installed. brew install create-dmg")

    # Seal the bundle *without* --options runtime; see the module docstring.
    subprocess.run(
        ["codesign", "--force", "--sign", args.identity, "--timestamp=none", str(APP)],
        check=True,
    )
    subprocess.run(["codesign", "--verify", "--strict", str(APP)], check=True)

    version = app_version()
    target = ROOT / "dist" / f"JoyRead-{version}-macos-arm64.dmg"
    target.unlink(missing_ok=True)

    with TemporaryDirectory(prefix="joyread-dmg-") as workspace_name:
        workspace = Path(workspace_name)
        # create-dmg copies everything in the source folder, so it holds the
        # app and nothing else; the Applications link comes from the drop-link
        # option rather than from a symlink we make ourselves.
        source = workspace / "source"
        source.mkdir()
        shutil.copytree(APP, source / APP.name, symlinks=True)

        command = [
            "create-dmg",
            "--volname", f"JoyRead {version}",
            "--background", str(hidpi_background(workspace)),
            "--window-pos", "200", "120",
            "--window-size", str(background.WIDTH), str(background.HEIGHT),
            "--icon-size", "128",
            "--text-size", "12",
            "--icon", APP.name, str(background.APP_CENTRE[0]), str(background.APP_CENTRE[1]),
            "--app-drop-link",
            str(background.APPLICATIONS_CENTRE[0]),
            str(background.APPLICATIONS_CENTRE[1]),
            "--hide-extension", APP.name,
            # The download is a one-off; mounting and copying it automatically
            # is a behaviour people find surprising rather than helpful.
            "--no-internet-enable",
        ]
        if VOLUME_ICON.is_file():
            command[1:1] = ["--volicon", str(VOLUME_ICON)]
        command += [str(target), str(source)]
        subprocess.run(command, check=True)

    subprocess.run(["hdiutil", "verify", str(target)], check=True)
    size_mb = target.stat().st_size / (1024 * 1024)
    print(f"{target}  ({size_mb:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
