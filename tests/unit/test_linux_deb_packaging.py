"""Guards for JoyRead's native Ubuntu/Debian installer."""

from __future__ import annotations

import importlib.util
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_linux_deb.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_linux_deb", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _assert_posix_mode(path: Path, expected: int) -> None:
    """Assert Debian payload modes where the host can represent them."""

    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == expected


def _make_valid_app(builder, root: Path, architecture: str = "amd64") -> Path:
    app_dir = root / "app"
    for relative in builder.required_app_files(architecture):
        path = app_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (app_dir / builder.APP_NAME).chmod(0o755)
    payload = app_dir / "_internal" / "payload.bin"
    payload.write_bytes(b"runtime")
    link = app_dir / "_internal" / "payload-link.bin"
    # Symlink creation needs Developer Mode or elevated privileges on Windows.
    # The Debian payload preserves symlinks on its native Linux build host, so
    # exercise that branch there without making the cross-platform unit suite
    # depend on a local Windows policy.
    if sys.platform != "win32":
        link.symlink_to(payload.name)
    return app_dir


def test_debian_architecture_uses_native_package_names() -> None:
    builder = _load_builder()

    assert builder.debian_architecture("x86_64") == "amd64"
    assert builder.debian_architecture("AMD64") == "amd64"
    assert builder.debian_architecture("aarch64") == "arm64"


def test_validate_app_dir_rejects_an_executable_without_its_runtime(tmp_path: Path) -> None:
    builder = _load_builder()
    app_dir = tmp_path / "incomplete"
    app_dir.mkdir()
    executable = app_dir / builder.APP_NAME
    executable.touch(mode=0o755)

    with pytest.raises(SystemExit, match="Incomplete PyInstaller app directory"):
        builder.validate_app_dir(app_dir, "amd64")


def test_stage_package_installs_the_full_app_and_desktop_integration(tmp_path: Path) -> None:
    builder = _load_builder()
    app_dir = _make_valid_app(builder, tmp_path)
    staging_root = tmp_path / "staging"
    staging_root.mkdir()

    builder.stage_package(
        staging_root,
        app_dir=app_dir,
        version="1.2.3",
        architecture="amd64",
    )

    installed_app = staging_root / builder.INSTALL_PREFIX
    assert (installed_app / builder.APP_NAME).is_file()
    _assert_posix_mode(installed_app / builder.APP_NAME, 0o755)
    _assert_posix_mode(installed_app, 0o755)
    assert (installed_app / "_internal/payload.bin").read_bytes() == b"runtime"
    _assert_posix_mode(installed_app / "_internal/payload.bin", 0o644)
    if sys.platform != "win32":
        assert (installed_app / "_internal/payload-link.bin").is_symlink()

    desktop = (staging_root / builder.DESKTOP_PATH).read_text(encoding="utf-8")
    assert "Exec=/opt/joyread/JoyRead %F" in desktop
    assert "MimeType=" in desktop
    for mime_type in (
        "application/vnd.comicbook+zip",
        "application/vnd.comicbook-rar",
        "application/x-cb7",
        "application/pdf",
        "application/zip",
        "application/vnd.rar",
        "application/x-7z-compressed",
    ):
        assert mime_type in desktop
    assert "application/epub+zip" not in desktop
    assert (staging_root / builder.ICON_PATH).is_file()
    assert (staging_root / builder.DOC_DIRECTORY / "LICENSE").is_file()
    _assert_posix_mode(staging_root / builder.DOC_DIRECTORY / "LICENSE", 0o644)
    assert (staging_root / builder.DOC_DIRECTORY / "THIRD_PARTY_NOTICES.txt").is_file()
    _assert_posix_mode(staging_root / "usr/share/applications", 0o755)

    control = (staging_root / "DEBIAN/control").read_text(encoding="utf-8")
    assert "Package: joyread" in control
    assert "Version: 1.2.3" in control
    assert "Architecture: amd64" in control
    assert "Installed-Size: " in control


def test_maintainer_scripts_never_choose_default_handlers() -> None:
    builder = _load_builder()
    scripts = f"{builder.POSTINST}\n{builder.POSTRM}".casefold()

    assert "update-desktop-database" in scripts
    assert "mimeapps.list" not in scripts
    assert "xdg-mime" not in scripts
    assert "gio mime" not in scripts
    assert " default " not in scripts


def test_output_name_uses_debian_architecture() -> None:
    builder = _load_builder()

    assert builder.output_path("1.0.1", "amd64") == (
        builder.ROOT / "dist/JoyRead-1.0.1-linux-amd64.deb"
    )


def test_dpkg_command_builds_with_root_ownership() -> None:
    builder = _load_builder()
    command = builder.dpkg_deb_command(
        "/usr/bin/dpkg-deb",
        PurePosixPath("/tmp/staging"),
        PurePosixPath("/tmp/JoyRead.deb"),
    )

    assert command[0] == "/usr/bin/dpkg-deb"
    assert "--root-owner-group" in command
    assert "--build" in command
    assert command[-1] == "/tmp/JoyRead.deb"


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("dpkg-deb") is None,
    reason="dpkg-deb is only expected on Linux packaging hosts",
)
def test_dpkg_deb_accepts_the_staged_package(tmp_path: Path) -> None:
    builder = _load_builder()
    app_dir = _make_valid_app(builder, tmp_path)
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    builder.stage_package(
        staging_root,
        app_dir=app_dir,
        version="1.2.3",
        architecture="amd64",
    )
    destination = tmp_path / "JoyRead.deb"

    builder.build_deb(staging_root, destination)

    assert destination.is_file()
    fields = subprocess.run(
        ["dpkg-deb", "--field", str(destination), "Package", "Version", "Architecture"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Package: joyread" in fields
    assert "Version: 1.2.3" in fields
    assert "Architecture: amd64" in fields
