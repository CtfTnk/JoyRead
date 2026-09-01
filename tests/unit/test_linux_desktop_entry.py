"""The Linux desktop entry is what makes "Open With" exist at all on Linux.

Without it no file manager can hand JoyRead a document, so the single-instance
forwarding path is not slow there -- it is unreachable. These tests guard the
one thing that can silently rot: the mapping from the extensions the app
dispatches to the MIME types the desktop is told about.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

from joyread.core.file_types import SUPPORTED_READER_EXTENSIONS


REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "scripts" / "build_linux_desktop.py"
DESKTOP_FILE = REPO_ROOT / "packaging" / "linux" / "joyread.desktop"


def _load_generator():
    spec = importlib.util.spec_from_file_location("build_linux_desktop", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_supported_extension_has_a_mime_type() -> None:
    """An unmapped extension is one Linux will never offer JoyRead for.

    It fails silently: the app still opens the file from the command line, so
    only a user right-clicking in a file manager would ever notice.
    """

    generator = _load_generator()
    unmapped = sorted(
        extension
        for extension in SUPPORTED_READER_EXTENSIONS
        if extension not in generator.MIME_TYPES
    )

    assert not unmapped, f"add MIME types for {unmapped} in scripts/build_linux_desktop.py"


def test_the_generator_refuses_an_unmapped_extension() -> None:
    """The guard has to be in the generator, not only in this test.

    A build run on a machine nobody tested must fail loudly rather than emit a
    desktop entry that quietly omits a format.
    """

    generator = _load_generator()

    with pytest.raises(SystemExit, match="No MIME type mapping"):
        generator.mime_types_for((".cbz", ".unheard-of"))


def test_the_checked_in_desktop_entry_is_current() -> None:
    """Regenerating must be a no-op, or the checked-in file is stale."""

    generator = _load_generator()
    expected = generator.render("/opt/joyread/JoyRead")

    assert DESKTOP_FILE.is_file(), "run scripts/build_linux_desktop.py"
    assert DESKTOP_FILE.read_text(encoding="utf-8") == expected


def test_the_desktop_entry_accepts_multiple_documents() -> None:
    """``%F`` not ``%f``: a launch can carry several paths.

    ``LaunchCoordinator`` merges them into one intent, which is how selecting
    three archives opens three Readers instead of three Libraries.
    """

    text = DESKTOP_FILE.read_text(encoding="utf-8")
    exec_line = next(line for line in text.splitlines() if line.startswith("Exec="))

    assert exec_line.endswith(" %F")


def test_the_desktop_entry_has_the_required_keys() -> None:
    text = DESKTOP_FILE.read_text(encoding="utf-8")
    entries = dict(
        line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("[")
    )

    assert entries["Type"] == "Application"
    assert entries["Name"] == "JoyRead"
    assert entries["Terminal"] == "false"
    assert entries["Categories"] == "Graphics;Viewer;"
    # `Icon` is a theme name, not a path: the installer drops JoyRead.png into
    # hicolor as `joyread.png` and the theme resolves the rest.
    assert entries["Icon"] == "joyread"
    assert entries["MimeType"].endswith(";")
    assert "application/vnd.comicbook+zip" in entries["MimeType"]


def test_the_app_identifies_itself_with_the_desktop_entry_name() -> None:
    """The entry alone is not enough: the window has to claim it.

    Without ``setDesktopFileName`` a Wayland compositor cannot match the running
    window to ``joyread.desktop``, so it shows a generic icon and a second
    taskbar entry beside the launcher. This asserts the name the app claims is
    the basename of the file the installer writes.
    """

    source = (REPO_ROOT / "src" / "joyread" / "app" / "bootstrap.py").read_text(encoding="utf-8")

    assert 'setDesktopFileName("joyread")' in source
    assert DESKTOP_FILE.stem == "joyread"
