"""Package a built ``dist/JoyRead.app`` into a distributable .dmg.

Run ``scripts/build_release.py`` first; this only wraps what that produced.

Signing is deliberately *not* delegated to PyInstaller here. Setting
``JOYREAD_CODESIGN_IDENTITY`` makes PyInstaller sign with ``--options runtime``,
and the Hardened Runtime requires every loaded library to share the main
executable's Team ID. An ad-hoc signature has no Team ID, so the app dies at
launch with "different Team IDs" before reaching Python. Hardened Runtime is
only needed for notarization, which an ad-hoc build cannot do anyway -- so the
app is built unsigned and sealed here without it.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "dist" / "JoyRead.app"
NOTE = ROOT / "packaging" / "dmg" / "READ ME 请先阅读 お読みください.txt"


def app_version() -> str:
    plist = plistlib.loads((APP / "Contents" / "Info.plist").read_bytes())
    return str(plist["CFBundleShortVersionString"])


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
    if not NOTE.is_file():
        raise SystemExit(f"Missing the first-launch note: {NOTE}")

    # Seal the bundle *without* --options runtime; see the module docstring.
    subprocess.run(
        ["codesign", "--force", "--sign", args.identity, "--timestamp=none", str(APP)],
        check=True,
    )
    subprocess.run(["codesign", "--verify", "--strict", str(APP)], check=True)

    version = app_version()
    target = ROOT / "dist" / f"JoyRead-{version}-macos-arm64.dmg"
    target.unlink(missing_ok=True)

    with TemporaryDirectory(prefix="joyread-dmg-") as staging_name:
        staging = Path(staging_name)
        shutil.copytree(APP, staging / APP.name, symlinks=True)
        (staging / "Applications").symlink_to("/Applications")
        shutil.copy2(NOTE, staging / NOTE.name)
        subprocess.run(
            ["hdiutil", "create", "-volname", f"JoyRead {version}",
             "-srcfolder", str(staging), "-ov", "-format", "UDZO",
             "-quiet", str(target)],
            check=True,
        )

    subprocess.run(["hdiutil", "verify", str(target)], check=True)
    size_mb = target.stat().st_size / (1024 * 1024)
    print(f"{target}  ({size_mb:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
