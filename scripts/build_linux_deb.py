#!/usr/bin/env python3
"""Build a native Ubuntu/Debian installer from JoyRead's PyInstaller tree."""

from __future__ import annotations

import argparse
import importlib.util
import math
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "dist" / "JoyRead"
OUTPUT_DIR = ROOT / "dist"
BUILD_SCRIPT = ROOT / "scripts" / "build_release.py"
DESKTOP_BUILDER = ROOT / "scripts" / "build_linux_desktop.py"
ICON_SOURCE = ROOT / "src" / "joyread" / "ui" / "resources" / "icons" / "JoyRead.png"
LICENSE_SOURCE = ROOT / "LICENSE"
NOTICES_SOURCE = ROOT / "packaging" / "THIRD_PARTY_NOTICES.txt"

APP_NAME = "JoyRead"
PACKAGE_NAME = "joyread"
INSTALL_PREFIX = Path("opt/joyread")
DESKTOP_PATH = Path("usr/share/applications/joyread.desktop")
ICON_PATH = Path("usr/share/icons/hicolor/512x512/apps/joyread.png")
DOC_DIRECTORY = Path("usr/share/doc/joyread")

ARCHITECTURES: dict[str, tuple[str, str]] = {
    "amd64": ("x86_64", "linux-x86_64"),
    "arm64": ("arm64", "linux-arm64"),
}
MACHINE_ARCHITECTURES = {
    "amd64": "amd64",
    "x64": "amd64",
    "x86_64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}

# These are the host libraries PySide6's Linux wheels still link dynamically.
# The remaining Python, Qt, and archive runtime is carried by the onedir itself.
RUNTIME_DEPENDENCIES = (
    "libc6",
    "libdbus-1-3",
    "libegl1",
    "libfontconfig1",
    "libfreetype6",
    "libgl1",
    "libwayland-client0",
    "libwayland-cursor0",
    "libxcb-cursor0",
    "libxkbcommon0",
    "libxkbcommon-x11-0",
    "desktop-file-utils",
    "shared-mime-info",
)

# Updating the application and icon caches is sufficient to expose JoyRead as
# an Open With candidate. Neither script writes mimeapps.list or asks xdg-mime
# to choose a default; that decision remains owned by the user.
POSTINST = """#!/bin/sh
set -e

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi

exit 0
"""

POSTRM = """#!/bin/sh
set -e

if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
    fi
fi

exit 0
"""


def project_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(project["project"]["version"])


def debian_architecture(machine: str | None = None) -> str:
    normalized = (machine or platform.machine()).casefold().replace("-", "_")
    architecture = MACHINE_ARCHITECTURES.get(normalized)
    if architecture is None:
        supported = ", ".join(sorted(MACHINE_ARCHITECTURES))
        raise SystemExit(
            f"Unsupported Linux machine architecture {normalized!r}; expected one of {supported}."
        )
    return architecture


def output_path(version: str, architecture: str) -> Path:
    return OUTPUT_DIR / f"{APP_NAME}-{version}-linux-{architecture}.deb"


def required_app_files(architecture: str) -> tuple[Path, ...]:
    try:
        _, extractor_directory = ARCHITECTURES[architecture]
    except KeyError as error:
        raise SystemExit(f"Unsupported Debian architecture {architecture!r}.") from error
    return (
        Path(APP_NAME),
        Path("_internal/LICENSE"),
        Path("_internal/THIRD_PARTY_NOTICES.txt"),
        Path("_internal/PySide6/Qt/plugins/platforms/libqxcb.so"),
        Path("_internal/PySide6/Qt/plugins/platforms/libqwayland.so"),
        Path(f"_internal/joyread/resources/extractors/7zip/{extractor_directory}/7zz"),
    )


def validate_app_dir(app_dir: Path, architecture: str) -> None:
    """Reject a partial or wrong-architecture onedir before packaging it."""

    if not app_dir.is_dir():
        raise SystemExit(
            f"Missing PyInstaller app directory: {app_dir}. "
            f"Run {BUILD_SCRIPT.relative_to(ROOT)} first."
        )
    missing = [str(path) for path in required_app_files(architecture) if not (app_dir / path).is_file()]
    if missing:
        raise SystemExit(f"Incomplete PyInstaller app directory at {app_dir}; missing {missing}.")
    executable = app_dir / APP_NAME
    if not os.access(executable, os.X_OK):
        raise SystemExit(f"PyInstaller launcher is not executable: {executable}.")


def _load_desktop_builder():
    spec = importlib.util.spec_from_file_location("build_linux_desktop_for_deb", DESKTOP_BUILDER)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Could not load desktop-entry builder: {DESKTOP_BUILDER}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def desktop_entry_text() -> str:
    builder = _load_desktop_builder()
    # The desktop entry is installed on Linux even when its shape is unit-tested
    # from another host. Keep the target path POSIX rather than inheriting the
    # build host's path separator.
    return str(builder.render(f"/{INSTALL_PREFIX.as_posix()}/{APP_NAME}"))


def installed_size_kib(payload_root: Path) -> int:
    total_bytes = sum(
        path.lstat().st_size
        for path in payload_root.rglob("*")
        if not path.is_dir()
    )
    return max(1, math.ceil(total_bytes / 1024))


def render_control(*, version: str, architecture: str, installed_size: int) -> str:
    dependencies = ", ".join(RUNTIME_DEPENDENCIES)
    return "\n".join(
        (
            f"Package: {PACKAGE_NAME}",
            f"Version: {version}",
            "Section: graphics",
            "Priority: optional",
            f"Architecture: {architecture}",
            "Maintainer: CtfTnk <ctftnk@users.noreply.github.com>",
            f"Installed-Size: {installed_size}",
            f"Depends: {dependencies}",
            "Homepage: https://github.com/CtfTnk/JoyRead",
            "Description: local manga archive and PDF reader",
            " JoyRead manages and reads local comic archives and PDF documents.",
            " It supports CBZ, CBR, CB7, ZIP, RAR, and 7Z archives.",
            "",
        )
    )


def _write_text(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def _validate_desktop_file(path: Path) -> None:
    validator = shutil.which("desktop-file-validate")
    if validator is None:
        return
    result = subprocess.run([validator, str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stdout or result.stderr).strip()
        raise SystemExit(f"Invalid Linux desktop entry: {detail}")


def _normalize_payload_modes(root: Path) -> None:
    """Remove build-host umask/group-write differences from installed files."""

    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o755)
            continue
        current_mode = path.stat().st_mode
        path.chmod(0o755 if current_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) else 0o644)


def stage_package(
    staging_root: Path,
    *,
    app_dir: Path,
    version: str,
    architecture: str,
) -> None:
    """Create a complete Debian filesystem tree under ``staging_root``."""

    validate_app_dir(app_dir, architecture)
    for source in (ICON_SOURCE, LICENSE_SOURCE, NOTICES_SOURCE):
        if not source.is_file():
            raise SystemExit(f"Missing Debian package input: {source}.")

    install_dir = staging_root / INSTALL_PREFIX
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(app_dir, install_dir, symlinks=True)

    desktop_path = staging_root / DESKTOP_PATH
    _write_text(desktop_path, desktop_entry_text(), 0o644)
    _validate_desktop_file(desktop_path)

    icon_path = staging_root / ICON_PATH
    icon_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ICON_SOURCE, icon_path)
    icon_path.chmod(0o644)

    doc_dir = staging_root / DOC_DIRECTORY
    doc_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(LICENSE_SOURCE, doc_dir / "LICENSE")
    shutil.copy2(NOTICES_SOURCE, doc_dir / "THIRD_PARTY_NOTICES.txt")

    control_dir = staging_root / "DEBIAN"
    control_dir.mkdir(parents=True, exist_ok=True)
    size = installed_size_kib(staging_root)
    _write_text(
        control_dir / "control",
        render_control(version=version, architecture=architecture, installed_size=size),
        0o644,
    )
    _write_text(control_dir / "postinst", POSTINST, 0o755)
    _write_text(control_dir / "postrm", POSTRM, 0o755)
    _normalize_payload_modes(staging_root)


def dpkg_deb_command(dpkg_deb: str, staging_root: Path, destination: Path) -> list[str]:
    return [
        dpkg_deb,
        "--root-owner-group",
        "--uniform-compression",
        "-Zxz",
        "-z6",
        "--build",
        staging_root.as_posix(),
        destination.as_posix(),
    ]


def build_deb(staging_root: Path, destination: Path) -> None:
    dpkg_deb = shutil.which("dpkg-deb")
    if dpkg_deb is None:
        raise SystemExit("dpkg-deb was not found. Install dpkg-dev before building the installer.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    subprocess.run(dpkg_deb_command(dpkg_deb, staging_root, destination), cwd=ROOT, check=True)
    if not destination.is_file():
        raise SystemExit(f"dpkg-deb completed without creating {destination}.")


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
        help="Use the existing verified PyInstaller onedir instead of rebuilding it.",
    )
    parser.add_argument(
        "--app-dir",
        type=Path,
        help="Use an explicit verified PyInstaller onedir (requires --skip-app-build).",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    if sys.platform != "linux":
        raise SystemExit("The Debian installer must be built on Linux.")
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("JoyRead release installers currently require Python 3.12.")
    if args.app_dir is not None and not args.skip_app_build:
        raise SystemExit("--app-dir requires --skip-app-build to avoid an ambiguous source.")

    if not args.skip_app_build:
        _build_application()

    architecture = debian_architecture()
    app_dir = (args.app_dir or APP_DIR).resolve()
    version = project_version()
    destination = output_path(version, architecture)
    with tempfile.TemporaryDirectory(prefix="joyread-deb-") as temporary_directory:
        staging_root = Path(temporary_directory) / f"{PACKAGE_NAME}_{version}_{architecture}"
        staging_root.mkdir()
        stage_package(
            staging_root,
            app_dir=app_dir,
            version=version,
            architecture=architecture,
        )
        build_deb(staging_root, destination)
    print(f"built {destination.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
