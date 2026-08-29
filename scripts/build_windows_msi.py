#!/usr/bin/env python3
"""Build the Windows JoyRead MSI from the verified PyInstaller onedir tree.

The repository pins WiX as a local .NET tool. The script restores that exact
version, optionally rebuilds ``dist/JoyRead``, validates the release inputs and
then creates one embedded-cab MSI under ``dist``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "dist" / "JoyRead"
WIX_SOURCE = ROOT / "packaging" / "windows" / "JoyRead.wxs"
INTERMEDIATE_DIR = ROOT / "build" / "msi"
LICENSE_SOURCE = ROOT / "LICENSE"
LICENSE_RTF = INTERMEDIATE_DIR / "LICENSE.rtf"
WIX_UI_EXTENSION = "WixToolset.UI.wixext/5.0.2"
REQUIRED_RELEASE_FILES = (
    Path("JoyRead.exe"),
    Path("_internal") / "LICENSE",
    Path("_internal") / "THIRD_PARTY_NOTICES.txt",
)


def project_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(project["project"]["version"])


def output_path(version: str) -> Path:
    return ROOT / "dist" / f"JoyRead-{version}-windows-x86_64.msi"


def validate_app_dir(app_dir: Path = APP_DIR) -> None:
    if not app_dir.is_dir():
        raise SystemExit(
            f"Missing PyInstaller app directory: {app_dir}. "
            "Run scripts/build_release.py first."
        )
    missing = [
        str(relative)
        for relative in REQUIRED_RELEASE_FILES
        if not (app_dir / relative).is_file()
    ]
    if missing:
        raise SystemExit(
            f"Incomplete PyInstaller release at {app_dir}; missing {missing}. "
            "Rebuild it before creating the MSI."
        )


def render_license_rtf(text: str) -> str:
    """Return plain license text as conservative ANSI/Unicode RTF."""

    escaped: list[str] = []
    for character in text:
        if character in "\\{}":
            escaped.append(f"\\{character}")
        elif character == "\n":
            escaped.append("\\par\n")
        elif ord(character) > 127:
            codepoint = ord(character)
            signed = codepoint if codepoint <= 32767 else codepoint - 65536
            escaped.append(f"\\u{signed}?")
        elif character != "\r":
            escaped.append(character)
    return "{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Segoe UI;}}\\f0\\fs18\n" + "".join(escaped) + "\n}"


def write_license_rtf(destination: Path = LICENSE_RTF) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        render_license_rtf(LICENSE_SOURCE.read_text(encoding="utf-8")),
        encoding="ascii",
    )
    return destination


def wix_extension_command() -> list[str]:
    return [
        "dotnet",
        "tool",
        "run",
        "wix",
        "--",
        "extension",
        "add",
        WIX_UI_EXTENSION,
    ]


def wix_build_command(
    version: str,
    destination: Path,
    license_rtf: Path = LICENSE_RTF,
) -> list[str]:
    return [
        "dotnet",
        "tool",
        "run",
        "wix",
        "--",
        "build",
        "-arch",
        "x64",
        "-d",
        f"Version={version}",
        "-d",
        f"LicenseRtf={license_rtf}",
        "-ext",
        WIX_UI_EXTENSION,
        "-bindpath",
        f"AppDir={APP_DIR}",
        "-intermediateFolder",
        str(INTERMEDIATE_DIR),
        "-pdbtype",
        "none",
        "-o",
        str(destination),
        str(WIX_SOURCE),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-app-build",
        action="store_true",
        help="Use the existing dist/JoyRead instead of rebuilding it.",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    if sys.platform != "win32":
        raise SystemExit("The WiX MSI must be built on Windows.")
    if shutil.which("dotnet") is None:
        raise SystemExit("The .NET SDK is required to restore and run the pinned WiX tool.")

    if not args.skip_app_build:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_release.py"), "--skip-tests"],
            cwd=ROOT,
            check=True,
        )

    validate_app_dir()
    version = project_version()
    destination = output_path(version)
    destination.parent.mkdir(parents=True, exist_ok=True)
    INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)

    subprocess.run(["dotnet", "tool", "restore"], cwd=ROOT, check=True)
    # Keep the extension cache under ignored build/ rather than changing a
    # developer's global WiX state. The version is explicit so it cannot drift
    # away from the pinned CLI major.
    subprocess.run(wix_extension_command(), cwd=INTERMEDIATE_DIR, check=True)
    license_rtf = write_license_rtf()
    subprocess.run(
        wix_build_command(version, destination, license_rtf),
        cwd=INTERMEDIATE_DIR,
        check=True,
    )
    print(f"built {destination.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
