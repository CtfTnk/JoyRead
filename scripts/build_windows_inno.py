#!/usr/bin/env python3
"""Build the Windows JoyRead installer from the verified PyInstaller tree."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
INNO_SOURCE = ROOT / "packaging" / "windows" / "inno" / "JoyRead.iss"
APP_DIR = ROOT / "dist" / "JoyRead"
OUTPUT_DIR = ROOT / "dist"
INNO_COMPILER_ENV_VAR = "JOYREAD_INNO_ISCC"
APP_NAME = "JoyRead"
DOCUMENT_ICON_SOURCE = ROOT / "src" / "joyread" / "ui" / "resources" / "icons" / "JoyReadDocument.ico"
REQUIRED_FILES = (
    Path("JoyRead.exe"),
    Path("_internal/LICENSE"),
    Path("_internal/THIRD_PARTY_NOTICES.txt"),
    Path("_internal/joyread/ui/resources/styles/main.qss"),
    Path("_internal/joyread/resources/extractors/7zip/windows-x86_64/7z.exe"),
    Path("_internal/joyread/resources/extractors/7zip/windows-x86_64/7z.dll"),
)
BUILD_SCRIPT = ROOT / "scripts" / "build_release.py"


def project_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(project["project"]["version"])


def windows_file_version(version: str) -> str:
    """Return Inno's required four-component numeric file version."""

    numeric_prefix = re.match(r"^(\d+(?:\.\d+){0,3})", version)
    if numeric_prefix is None:
        raise SystemExit(f"Cannot derive a Windows file version from {version!r}.")
    parts = numeric_prefix.group(1).split(".")
    return ".".join([*parts, *(["0"] * (4 - len(parts)))])


def output_path(version: str) -> Path:
    return OUTPUT_DIR / f"{APP_NAME}-{version}-windows-x86_64-setup.exe"


def validate_app_dir(app_dir: Path) -> None:
    """Reject a partial onedir before Inno can turn it into a broken setup."""

    if not app_dir.is_dir():
        raise SystemExit(
            f"Missing PyInstaller app directory: {app_dir}. "
            f"Run {BUILD_SCRIPT.relative_to(ROOT)} first."
        )
    missing = [str(path) for path in REQUIRED_FILES if not (app_dir / path).is_file()]
    if missing:
        raise SystemExit(f"Incomplete PyInstaller app directory at {app_dir}; missing {missing}.")
    if not any(path.name.casefold() == "qwindows.dll" for path in app_dir.rglob("*.dll")):
        raise SystemExit(f"Incomplete PyInstaller app directory at {app_dir}; missing Qt qwindows.dll.")


def validate_document_icon_source(source: Path = DOCUMENT_ICON_SOURCE) -> None:
    """Make the separately installed Explorer icon an explicit release input."""

    if not source.is_file():
        raise SystemExit(
            f"Missing Windows file-association icon: {source}. "
            "Run scripts/build_windows_document_icon.py first."
        )


def _registered_program_files_dirs() -> tuple[Path, ...]:
    """Read standard Program Files locations when a runner stripped its env."""

    if sys.platform != "win32":
        return ()
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion",
        ) as key:
            directories = []
            for value_name in ("ProgramW6432Dir", "ProgramFilesDir"):
                try:
                    value, _ = winreg.QueryValueEx(key, value_name)
                except FileNotFoundError:
                    continue
                if isinstance(value, str) and value:
                    directories.append(Path(value))
            return tuple(dict.fromkeys(directories))
    except OSError:
        return ()


def _candidate_iscc_paths(environment: dict[str, str]) -> tuple[Path, ...]:
    candidates: list[Path] = []
    configured = environment.get(INNO_COMPILER_ENV_VAR)
    if configured:
        candidates.append(Path(configured))
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        directory = environment.get(variable)
        if directory:
            candidates.append(Path(directory) / "Inno Setup 7" / "ISCC.exe")
    for directory in _registered_program_files_dirs():
        candidates.append(directory / "Inno Setup 7" / "ISCC.exe")
    on_path = shutil.which("ISCC.exe", path=environment.get("PATH"))
    if on_path:
        candidates.append(Path(on_path))
    return tuple(dict.fromkeys(candidates))


def discover_iscc(environment: dict[str, str] | None = None) -> Path:
    """Find the Inno Setup 7 command-line compiler without a hard-coded user path."""

    active_environment = dict(os.environ if environment is None else environment)
    for candidate in _candidate_iscc_paths(active_environment):
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(path) for path in _candidate_iscc_paths(active_environment)) or "PATH"
    raise SystemExit(
        "Inno Setup 7 command-line compiler (ISCC.exe) was not found. "
        f"Install it or set {INNO_COMPILER_ENV_VAR}; searched {searched}."
    )


def iscc_command(
    compiler: Path,
    *,
    version: str,
    app_dir: Path,
    destination: Path,
) -> list[str]:
    """Return the compiler command with every machine-specific path supplied."""

    return [
        str(compiler),
        "--quiet-progress",
        f"--define=MyProjectRoot={ROOT}",
        f"--define=MyAppSourceDir={app_dir}",
        f"--define=MyAppVersion={version}",
        f"--define=MyAppFileVersion={windows_file_version(version)}",
        f"--output-dir={destination.parent}",
        f"--output-filename={destination.stem}",
        str(INNO_SOURCE),
    ]


def _build_application() -> None:
    subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--skip-tests"],
        cwd=ROOT,
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-app-build",
        action="store_true",
        help="Use the existing onedir instead of rebuilding it without tests.",
    )
    parser.add_argument(
        "--app-dir",
        type=Path,
        help="Use an explicit verified onedir (requires --skip-app-build).",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    if sys.platform != "win32":
        raise SystemExit("The Inno Setup installer must be built on Windows.")
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("JoyRead release installers currently require Python 3.12.")
    if args.app_dir is not None and not args.skip_app_build:
        raise SystemExit("--app-dir requires --skip-app-build to avoid an ambiguous source.")

    if not args.skip_app_build:
        _build_application()

    app_dir = (args.app_dir or APP_DIR).resolve()
    version = project_version()
    destination = output_path(version)
    destination.parent.mkdir(parents=True, exist_ok=True)
    validate_app_dir(app_dir)
    validate_document_icon_source()
    compiler = discover_iscc()
    subprocess.run(
        iscc_command(compiler, version=version, app_dir=app_dir, destination=destination),
        cwd=ROOT,
        check=True,
    )
    if not destination.is_file():
        raise SystemExit(f"Inno Setup completed without creating {destination}.")
    print(f"built {destination.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
