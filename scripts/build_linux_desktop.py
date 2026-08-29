#!/usr/bin/env python3
"""Generate JoyRead's freedesktop desktop entry from the canonical file types.

Without a desktop entry Linux has no "Open With JoyRead" at all: no file manager
can hand the app a document, which means the single-instance forwarding path is
not merely slow there, it is unreachable. This is the Linux counterpart of the
``CFBundleDocumentTypes`` block ``packaging/joyread.spec`` builds for macOS, and
like that block it reads ``src/joyread/core/file_types.py`` directly so the
declared types cannot drift from what the app actually dispatches.

Generate (writes ``packaging/linux/joyread.desktop``)::

    python scripts/build_linux_desktop.py --exec /opt/joyread/JoyRead

Install for the current user, on Linux only::

    python scripts/build_linux_desktop.py --exec /opt/joyread/JoyRead --install

Installing writes to ``~/.local/share/applications`` and the hicolor icon theme
and refreshes the desktop database. It never edits ``mimeapps.list``: making
JoyRead the *default* for a type is the user's choice, not the installer's. See
"Linux desktop integration" in ``docs/PACKAGING.md``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import runpy
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
FILE_TYPES_PATH = ROOT / "src" / "joyread" / "core" / "file_types.py"
OUTPUT_PATH = ROOT / "packaging" / "linux" / "joyread.desktop"
ICON_SOURCE = ROOT / "src" / "joyread" / "ui" / "resources" / "icons" / "JoyRead.png"

#: freedesktop MIME type per supported extension. Comic formats use the
#: shared-mime-info names (``vnd.comicbook``); the generic container types are
#: claimed too, matching the "Alternate" handler rank the macOS bundle declares
#: -- listing a type here offers JoyRead in "Open With" without making it the
#: default, which is the freedesktop equivalent.
MIME_TYPES: dict[str, tuple[str, ...]] = {
    ".cbz": ("application/vnd.comicbook+zip",),
    ".cbr": ("application/vnd.comicbook-rar",),
    ".cb7": ("application/x-cb7",),
    ".zip": ("application/zip",),
    ".rar": ("application/vnd.rar", "application/x-rar-compressed"),
    ".7z": ("application/x-7z-compressed",),
    ".pdf": ("application/pdf",),
    # Carried even though `EPUB_ACCESS_ENABLED` is currently False. The entry
    # is emitted only for extensions in SUPPORTED_READER_EXTENSIONS, so the
    # desktop file already tracks that flag; flipping it needs no edit here.
    ".epub": ("application/epub+zip",),
}


def supported_extensions() -> tuple[str, ...]:
    """Read the extensions the app dispatches, from the module that owns them."""

    namespace = runpy.run_path(str(FILE_TYPES_PATH))
    return tuple(sorted(namespace["SUPPORTED_READER_EXTENSIONS"]))


def mime_types_for(extensions: tuple[str, ...]) -> tuple[str, ...]:
    """Map extensions to MIME types, refusing to silently drop an unknown one."""

    missing = sorted(extension for extension in extensions if extension not in MIME_TYPES)
    if missing:
        raise SystemExit(
            f"No MIME type mapping for {missing}. Add it to MIME_TYPES in "
            f"{Path(__file__).name} -- an unmapped extension is one the Linux "
            "desktop silently will not offer JoyRead for."
        )
    seen: list[str] = []
    for extension in extensions:
        for mime in MIME_TYPES[extension]:
            if mime not in seen:
                seen.append(mime)
    return tuple(seen)


def render(executable: str) -> str:
    extensions = supported_extensions()
    mimes = mime_types_for(extensions)
    # %F, not %f: the app accepts several documents in one launch, and the
    # launch coordinator merges them into a single intent.
    return "\n".join(
        (
            "[Desktop Entry]",
            "Type=Application",
            "Version=1.0",
            "Name=JoyRead",
            "GenericName=Manga and Light Novel Reader",
            "Comment=Read manga archives and light novels",
            f"Exec={executable} %F",
            "Icon=joyread",
            "Terminal=false",
            # `Viewer` is an additional category and needs a main one beside it;
            # Graphics covers comics, Office covers PDF/EPUB.
            "Categories=Graphics;Office;Viewer;",
            f"MimeType={';'.join(mimes)};",
            "StartupWMClass=JoyRead",
            "",
        )
    )


def install(desktop_text: str) -> None:
    if sys.platform != "linux":
        raise SystemExit(f"--install only makes sense on Linux, not {sys.platform}.")
    applications = Path.home() / ".local" / "share" / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    target = applications / "joyread.desktop"
    target.write_text(desktop_text, encoding="utf-8")
    print(f"installed {target}")

    if ICON_SOURCE.is_file():
        icons = Path.home() / ".local" / "share" / "icons" / "hicolor" / "512x512" / "apps"
        icons.mkdir(parents=True, exist_ok=True)
        # Named `joyread.png` to match `Icon=joyread`; the theme resolves the
        # extension itself.
        shutil.copyfile(ICON_SOURCE, icons / "joyread.png")
        print(f"installed {icons / 'joyread.png'}")

    for command in (
        ["update-desktop-database", str(applications)],
        ["gtk-update-icon-cache", "-f", "-t", str(Path.home() / ".local/share/icons/hicolor")],
    ):
        if shutil.which(command[0]) is None:
            continue
        # Best effort: both caches rebuild on their own eventually, and a
        # missing or failing updater must not fail the install.
        subprocess.run(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--exec",
        dest="executable",
        default="/opt/joyread/JoyRead",
        help="Absolute path to the installed JoyRead executable (default: %(default)s).",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Also install into ~/.local/share for the current user (Linux only).",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    desktop_text = render(args.executable)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(desktop_text, encoding="utf-8")
    print(f"wrote {OUTPUT_PATH.relative_to(ROOT)}")

    validator = shutil.which("desktop-file-validate")
    if validator is not None:
        result = subprocess.run([validator, str(OUTPUT_PATH)], capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stdout or result.stderr, file=sys.stderr)
            return 1
        print("desktop-file-validate: OK")

    if args.install:
        install(desktop_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
