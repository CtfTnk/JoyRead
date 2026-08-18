"""PyInstaller specification for JoyRead desktop builds."""

from __future__ import annotations

import os
import platform
from pathlib import Path
import runpy
import sys
import tomllib

from PyInstaller.utils.hooks import collect_submodules


ROOT = Path(SPECPATH).parent
PACKAGE_ROOT = ROOT / "src" / "joyread"
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
APP_NAME = "JoyRead"
VERSION = str(PROJECT["version"])
# Reverse-DNS under the project's GitHub namespace. macOS caches UTI
# registrations by identifier, so changing these after a public release
# strands the old ones in Launch Services -- treat them as frozen.
BUNDLE_ID = "io.github.ctftnk.joyread"
COMIC_ARCHIVE_UTI = f"{BUNDLE_ID}.comic-archive"
COPYRIGHT = "Copyright (C) 2026 JoyRead contributors. Licensed under GPL-3.0."
# Set by the release build to a Developer ID Application identity; an unsigned
# build stays possible for local development, but a distributed one must be
# signed and notarized or Gatekeeper refuses to open it.
CODESIGN_IDENTITY = os.environ.get("JOYREAD_CODESIGN_IDENTITY") or None
ENTITLEMENTS_FILE = os.environ.get("JOYREAD_ENTITLEMENTS") or None
FILE_TYPES = runpy.run_path(str(PACKAGE_ROOT / "core" / "file_types.py"))
ARCHIVE_DOCUMENT_EXTENSIONS = sorted(
    extension.removeprefix(".") for extension in FILE_TYPES["ARCHIVE_EXTENSIONS"]
)
COMIC_ARCHIVE_EXTENSIONS = sorted(
    set(ARCHIVE_DOCUMENT_EXTENSIONS) & {"cb7", "cbr", "cbz"}
)
PDF_DOCUMENT_EXTENSIONS = sorted(
    extension.removeprefix(".") for extension in FILE_TYPES["PDF_EXTENSIONS"]
)
EPUB_DOCUMENT_EXTENSIONS = sorted(
    extension.removeprefix(".") for extension in FILE_TYPES["EPUB_EXTENSIONS"]
)
EPUB_ACCESS_ENABLED = bool(FILE_TYPES["EPUB_ACCESS_ENABLED"])

MACOS_DOCUMENT_TYPES = [
    {
        "CFBundleTypeExtensions": ARCHIVE_DOCUMENT_EXTENSIONS,
        "CFBundleTypeName": "Manga Archive",
        "CFBundleTypeRole": "Viewer",
        "LSHandlerRank": "Alternate",
        "LSItemContentTypes": [
            COMIC_ARCHIVE_UTI,
            "public.zip-archive",
            "com.rarlab.rar-archive",
            "org.7-zip.7-zip-archive",
        ],
    },
    {
        # CBZ/CBR/CB7 have no system-wide UTI on a clean macOS install.
        # Keep this extension-only claim separate: macOS ignores extension
        # keys whenever LSItemContentTypes exists in the same document type.
        "CFBundleTypeExtensions": COMIC_ARCHIVE_EXTENSIONS,
        "CFBundleTypeName": "Comic Archive",
        "CFBundleTypeRole": "Viewer",
        "LSHandlerRank": "Alternate",
    },
    {
        "CFBundleTypeExtensions": PDF_DOCUMENT_EXTENSIONS,
        "CFBundleTypeName": "PDF Document",
        "CFBundleTypeRole": "Viewer",
        "LSHandlerRank": "Alternate",
        "LSItemContentTypes": ["com.adobe.pdf"],
    },
]
if EPUB_ACCESS_ENABLED:
    MACOS_DOCUMENT_TYPES.append(
        {
            "CFBundleTypeExtensions": EPUB_DOCUMENT_EXTENSIONS,
            "CFBundleTypeName": "EPUB Document",
            "CFBundleTypeRole": "Viewer",
            "LSHandlerRank": "Alternate",
            "LSItemContentTypes": ["org.idpf.epub-container"],
        }
    )


def platform_key() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "darwin"
    if system.startswith("win"):
        return "windows"
    if system == "linux":
        return "linux"
    return system or "unknown"


def architecture_key() -> str:
    machine = platform.machine().lower().replace("-", "_")
    if machine in {"amd64", "x64", "x86_64"}:
        return "x86_64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return machine or "unknown"


platform_directory = f"{platform_key()}-{architecture_key()}"
extractor_directory = PACKAGE_ROOT / "resources" / "extractors" / "7zip" / platform_directory
extractor_name = "7zz.exe" if platform_key() == "windows" else "7zz"
extractor_path = extractor_directory / extractor_name
if not extractor_path.is_file():
    raise SystemExit(
        f"Missing release extractor: {extractor_path}. "
        "Add the platform 7-Zip binary before building this target."
    )

extractor_destination = f"joyread/resources/extractors/7zip/{platform_directory}"
datas = [
    (str(ROOT / "packaging" / "THIRD_PARTY_NOTICES.txt"), "."),
    (str(PACKAGE_ROOT / "ui" / "resources"), "joyread/ui/resources"),
    (str(PACKAGE_ROOT / "resources" / "locales"), "joyread/resources/locales"),
    (
        str(PACKAGE_ROOT / "resources" / "extractors" / "7zip" / "LICENSE.txt"),
        "joyread/resources/extractors/7zip",
    ),
    (
        str(PACKAGE_ROOT / "resources" / "extractors" / "7zip" / "README.md"),
        "joyread/resources/extractors/7zip",
    ),
]
hiddenimports = collect_submodules("py7zr")
if sys.platform == "darwin":
    hiddenimports.extend(["objc", "Foundation", "AppKit"])
for legal_name in ("License.txt", "readme.txt", "History.txt"):
    legal_path = extractor_directory / legal_name
    if legal_path.is_file():
        datas.append((str(legal_path), extractor_destination))

a = Analysis(
    [str(ROOT / "src" / "joyread" / "app" / "main.py")],
    pathex=[str(ROOT / "src")],
    binaries=[(str(extractor_path), extractor_destination)],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

icon_path = PACKAGE_ROOT / "ui" / "resources" / "icons" / "JoyRead.icns"
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(icon_path) if sys.platform == "darwin" else None,
    codesign_identity=CODESIGN_IDENTITY,
    entitlements_file=ENTITLEMENTS_FILE,
)
collection = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

if sys.platform == "darwin":
    app = BUNDLE(
        collection,
        name=f"{APP_NAME}.app",
        icon=str(icon_path),
        bundle_identifier=BUNDLE_ID,
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "CFBundleDocumentTypes": MACOS_DOCUMENT_TYPES,
            "LSMinimumSystemVersion": "13.0",
            "LSSupportsOpeningDocumentsInPlace": True,
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": COPYRIGHT,
            "UTExportedTypeDeclarations": [
                {
                    "UTTypeConformsTo": ["public.archive", "public.data"],
                    "UTTypeDescription": "JoyRead Manga Archive",
                    "UTTypeIdentifier": COMIC_ARCHIVE_UTI,
                    "UTTypeTagSpecification": {
                        "public.filename-extension": COMIC_ARCHIVE_EXTENSIONS,
                    },
                }
            ],
        },
    )
