"""PyInstaller specification for JoyRead desktop builds."""

from __future__ import annotations

import os
import platform
from pathlib import Path
import runpy
import sys
import tomllib

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


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
# Unix ships one self-contained `7zz`. Windows has no equivalent: the only
# standalone console build published there is `7za.exe`, which has "reduced
# formats support" and drops RAR -- a format JoyRead advertises. So Windows
# vendors the full `7z.exe` with its `7z.dll`, which must travel together: the
# executable is a front end and opens nothing without the library beside it.
extractor_name = "7z.exe" if platform_key() == "windows" else "7zz"
extractor_path = extractor_directory / extractor_name
extractor_support = [extractor_directory / "7z.dll"] if platform_key() == "windows" else []
for required in (extractor_path, *extractor_support):
    if not required.is_file():
        raise SystemExit(
            f"Missing release extractor: {required}. "
            "Add the platform 7-Zip binary before building this target."
        )

extractor_destination = f"joyread/resources/extractors/7zip/{platform_directory}"
binaries = [
    (str(path), extractor_destination)
    for path in (extractor_path, *extractor_support)
]
if platform_key() == "windows":
    # The repository's prefix-based Conda Python links these from
    # <prefix>/Library/bin. PyInstaller does not add that directory to its
    # dependency search, even when the environment is activated, so an
    # otherwise-successful build omits them and fails when ctypes or SQLite is
    # first imported on a clean machine.
    conda_runtime_directory = Path(sys.prefix) / "Library" / "bin"
    for runtime_name in ("ffi.dll", "sqlite3.dll"):
        runtime_path = conda_runtime_directory / runtime_name
        if not runtime_path.is_file():
            raise SystemExit(
                f"Missing Windows Conda runtime DLL: {runtime_path}. "
                "Build with the repository's .conda/joyread-py312 environment."
            )
        binaries.append((str(runtime_path), "."))

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

# Tag indexing romanizes CJK tag names so they sort onto the same A-Z rail as
# Latin ones. Both libraries carry dictionary *data*, not just code, and both
# are imported lazily inside joyread.core.tag_indexing -- PyInstaller's static
# analysis cannot see through either, so a build without these lines starts
# fine and then drops every CJK tag into "#" (or fails to import) only once a
# tag surface is opened.
datas += collect_data_files("pykakasi")
datas += collect_data_files("pypinyin")
hiddenimports.extend(collect_submodules("pykakasi"))
hiddenimports.extend(collect_submodules("pypinyin"))

# The novel reader enters the app through one function-level import in
# bootstrap, which PyInstaller's static analysis follows regardless of the
# gate that guards it at runtime. Left alone, a gate-off build would ship the
# whole disabled feature and lxml with it, so the exclusion has to be explicit.
# Gate-on builds need the opposite: the package is only ever reached
# dynamically, so its submodules have to be named to be collected at all.
excludes = ["tkinter"]
if EPUB_ACCESS_ENABLED:
    hiddenimports.extend(collect_submodules("joyread.novel"))
    hiddenimports.extend(["lxml", "lxml.etree"])
else:
    excludes.extend(["joyread.novel", "lxml"])
for legal_name in ("License.txt", "readme.txt", "History.txt"):
    legal_path = extractor_directory / legal_name
    if legal_path.is_file():
        datas.append((str(legal_path), extractor_destination))

a = Analysis(
    [str(ROOT / "src" / "joyread" / "app" / "main.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)
if platform_key() == "windows":
    # Qt6Core links against Windows' system ICU shim (`System32/icuuc.dll`).
    # PyInstaller searches the build process PATH and can mistake an unrelated
    # application's full ICU distribution for that system library. The Codex
    # Poppler runtime exposed ICU 78 this way: bundling it as `_internal/icuuc.dll`
    # made Windows prefer it over the system shim and QtCore failed at import
    # with "The specified procedure could not be found." Root-level ICU is
    # therefore never a JoyRead binary; platform-local Qt files, if Qt starts
    # shipping any later, retain their subdirectory and are left untouched.
    def is_foreign_root_icu(entry) -> bool:  # noqa: ANN001
        destination = Path(entry[0])
        name = destination.name.casefold()
        return len(destination.parts) == 1 and (
            name == "icuuc.dll" or (name.startswith("icudt") and name.endswith(".dll"))
        )

    a.binaries = [entry for entry in a.binaries if not is_foreign_root_icu(entry)]
pyz = PYZ(a.pure)

icon_directory = PACKAGE_ROOT / "ui" / "resources" / "icons"
macos_icon_path = icon_directory / "JoyRead.icns"
windows_icon_path = icon_directory / "JoyRead.ico"
executable_icon_path = {
    "darwin": macos_icon_path,
    "windows": windows_icon_path,
}.get(platform_key())
if executable_icon_path is not None and not executable_icon_path.is_file():
    raise SystemExit(f"Missing application icon for {platform_key()}: {executable_icon_path}")
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
    icon=str(executable_icon_path) if executable_icon_path is not None else None,
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
        icon=str(macos_icon_path),
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
