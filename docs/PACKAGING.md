# JoyRead Packaging Guide

This guide builds JoyRead **1.2.0** with PyInstaller.
See [release notes](releases/v1.2.0.md) for installation and validation status.
Build each target on its own operating system; PyInstaller does not
cross-compile desktop apps. The native Debian builder and Inno Setup wrap the
verified Linux and Windows onedirs as the production installers described in
sections 3c and 3d.

The version is read from `pyproject.toml` by `packaging/joyread.spec`, so it is
set in exactly one place. `src/joyread/__init__.py` carries the same string for
runtime reporting and must be bumped alongside it.

## 1. Create the release environment

Run from the repository root:

```bash
conda env create --prefix .conda/joyread-py312 -f environment-release.yml
conda activate ./.conda/joyread-py312
```

The environment uses Python 3.12.9 and installs JoyRead in editable mode with
its test and release dependencies. To refresh an existing environment:

```bash
conda activate ./.conda/joyread-py312
python -m pip install -e '.[dev,release]'
```

## 2. Verify the release candidate

```bash
python --version
python -m pytest -q
```

The release candidate should have a clean test run and no unexpected files in
`git status`. Test with a fresh runtime profile before packaging:

```bash
JOYREAD_RUNTIME_DIR=/tmp/joyread-release-smoke python -m joyread.app.main
```

Check initialization, import, Library recovery, CBZ/ZIP/7z/RAR/PDF,
thumbnail scrolling, Reader close/reopen, and application restart.

For thumbnail HiDPI acceptance, check 100%, 125%, 150%, and 200% scaling and move
the Library between monitors with different scale factors. Covers and Detail
thumbnails should sharpen after loading while card geometry, selection and
Detail scroll position stay unchanged. Verify custom cover edits, cached covers
with missing source files, and switching screens during thumbnail generation.
Repeat for the Reader Topic thumbnail panel, including a panel hidden while
switching screens and reopened afterwards; preserve the reader's page position.
Automated scale simulation does not replace physical multi-monitor acceptance.

For the 1.1.0 custom-sort changes, validate grid and list with a temporary
library: Shift selection, rectangles while wheel-scrolling, group order, first/
last insertion, outside release and return, right-button/Esc cancellation, window
deactivation, and external file import. Reopen to check per-scope order, and
remove/re-add a favourite/collection member to check front insertion. The maintainer confirmed completion of desktop acceptance for the 1.2.0
release on 2026-09-18. Automated checks and package builds are recorded
separately in the release notes and asset provenance.

The novel (EPUB) reader remains in the repository, under `src/joyread/novel/`,
but is disabled for this release until it is complete. With
`EPUB_ACCESS_ENABLED` off the spec excludes both `joyread.novel` and `lxml`
from the bundle — PyInstaller's static analysis follows the gated import in
`bootstrap.py` regardless of the runtime flag, so without those exclusions a
gate-off build would ship the whole disabled feature.

Re-enabling it later therefore means three things, not one: flip the flag,
build in an environment with the extra installed (`pip install -e
'.[release,epub]'`), and re-check `THIRD_PARTY_NOTICES.txt`, where the lxml
entry is currently marked as shipping only in EPUB-enabled builds.

## 3. Build

The build script runs the complete test suite and then PyInstaller:

```bash
python scripts/build_release.py
```

After tests have already passed, the packaging-only command is:

```bash
python scripts/build_release.py --skip-tests
```

Expected artifact on macOS:

```text
dist/JoyRead.app
```

Without `JOYREAD_CODESIGN_IDENTITY` set (see section 5) this is an unsigned
build, fine for local testing and not distributable. Open it once from Finder
and repeat the smoke test against a new Library. Inspect `Contents/MacOS` and
`Contents/Frameworks` if startup fails; JoyRead logs remain under the normal
platform user log directory.

## 3a. Wrapping the app in a .dmg

```bash
python scripts/build_dmg.py
```

Needs the `release` extra, which carries `dmgbuild`:

```bash
pip install -e ".[release]"
```

Produces `dist/JoyRead-<version>-macos-arm64.dmg` containing the app and an
`Applications` symlink, in a window with a set size, positioned icons, a volume
icon and a background drawn by `packaging/dmg/background.py`.

`dmgbuild` writes the Finder `.DS_Store` itself, through `ds_store` and
`mac_alias`. That is the whole reason it is used: **the layout step touches
Finder at nothing, so this runs headless and in CI.** The obvious alternative,
`create-dmg`, can only set a layout by driving Finder over AppleScript, which
needs a logged-in GUI session and is flaky enough that the tool ships a
five-second sleep to work around `Can't get disk (-1728)`. Its `--skip-jenkins`
escape hatch skips precisely the part worth having.

The background is generated rather than checked in, because its size has to
match the window size -- Finder pins a background at its natural size and crops
the rest -- and both numbers live together in `background.py`. It is rendered at
1x and 2x; `dmgbuild` finds the `@2x` file beside the first and pairs them into
a HiDPI TIFF itself, so a lone PNG never reaches a retina display.

Two conventions worth knowing before editing the layout. `window_rect`'s
position runs **bottom-to-top**, unlike `create-dmg`'s `--window-pos`, which is
why that number looks large. Icon positions do *not*: `icon_locations` uses the
same top-left origin, so the constants in `background.py` are shared with the
drawing directly.

Before any of this the DMG was built with plain `hdiutil`, which writes no
`.DS_Store` at all: the window opened at whatever size Finder defaulted to, with
the icons wherever it chose to put them.

The script signs the bundle itself rather than letting PyInstaller do it, and
that is load-bearing for an unsigned release. Setting
`JOYREAD_CODESIGN_IDENTITY` makes PyInstaller sign with `--options runtime`,
and the Hardened Runtime requires every loaded library to share the main
executable's Team ID. An ad-hoc signature has no Team ID, so the app dies before
reaching Python:

```text
libpython3.12.dylib ... not valid for use in process:
mapping process and mapped file (non-platform) have different Team IDs
```

Hardened Runtime is only needed for notarization, which an ad-hoc build cannot
do anyway. So `build_release.py` runs unsigned and `build_dmg.py` seals the
bundle without it. Pass `--identity` once a Developer ID is available; section 5
still covers notarization, which the script does not attempt.

An ad-hoc build is quarantined on download and reports itself as *damaged*.
The disk image no longer carries a note about that: instructions in a `.txt`
inside a DMG arrive after the moment they were needed, since the error appears
when the app is double-clicked and nobody has opened the text file by then.
That guidance belongs on the release page, where someone can find it after
hitting the error.

## 3b. Linux desktop integration

```bash
python scripts/build_linux_desktop.py --exec /opt/joyread/JoyRead
```

Writes `packaging/linux/joyread.desktop`. Without it Linux has no "Open With
JoyRead" at all -- no file manager can hand the app a document, so the
single-instance forwarding path is not merely slow there, it is unreachable.

The generator is the Linux counterpart of the `CFBundleDocumentTypes` block the
spec builds for macOS, and it reads `src/joyread/core/file_types.py` through
`runpy` for the same reason: the declared types cannot drift from what the app
dispatches. `SUPPORTED_READER_EXTENSIONS` already honours `EPUB_ACCESS_ENABLED`,
so the shipping entry claims no EPUB type and needs no edit when that flag
flips. An extension with no MIME mapping fails the build rather than being
dropped -- an unmapped type is one the desktop silently never offers JoyRead
for, and the app would still open it from the command line, so nothing else
would reveal the gap. `tests/unit/test_linux_desktop_entry.py` holds the same
line at test time and checks the committed file is current.

Three details are load-bearing:

- **`%F`, not `%f`.** A launch can carry several paths, and
  `LaunchCoordinator` merges them into one intent; `%f` would spawn one process
  per file and lose that.
- **`Icon=joyread` is a theme name, not a path.** `--install` copies
  `JoyRead.png` to `~/.local/share/icons/hicolor/512x512/apps/joyread.png` and
  lets the icon theme resolve it. A path would break the moment the app moved.
- **The running app has to claim the entry.** `bootstrap.py` calls
  `app.setDesktopFileName("joyread")`, which is what links a live window back to
  `joyread.desktop`: Wayland derives the `app_id` from it, X11 derives WM_CLASS
  to match `StartupWMClass`. Without it the desktop shows a generic icon and
  opens a second taskbar entry beside the launcher. The call is inert on Windows
  and macOS, so it needs no platform guard.

`--install` is Linux-only and installs for the current user: the desktop entry,
the hicolor icon, and best-effort `update-desktop-database` /
`gtk-update-icon-cache` runs. **It deliberately does not touch
`mimeapps.list`.** Listing a MIME type offers JoyRead in "Open With"; writing
`mimeapps.list` would make it the *default* handler for `.pdf` and `.zip`, which
is the user's choice, not an installer's. That mirrors the "Alternate" handler
rank the macOS bundle declares. Users who want the default run:

```bash
xdg-mime default joyread.desktop application/vnd.comicbook+zip
```

A real distribution package installs the same two files to
`/usr/share/applications` and `/usr/share/icons/hicolor/512x512/apps`, with
`Exec=` pointing at the installed path. The checked-in file assumes
`/opt/joyread/JoyRead`; regenerate with `--exec` for any other layout.
`desktop-file-validate` runs automatically when it is on `PATH` and fails the
generation if the entry is malformed.

Ubuntu desktop acceptance was confirmed by the maintainer for 1.2.0.
Generated desktop entries remain covered by automated shape and MIME tests.

## 3c. Ubuntu/Debian .deb

Build the native package on the Linux architecture it will run on:

```bash
python scripts/build_linux_deb.py
```

This rebuilds the PyInstaller onedir without rerunning tests, validates it, and
produces `dist/JoyRead-<version>-linux-<debian-architecture>.deb`. On the
current x86-64 target that is `dist/JoyRead-1.0.2-linux-amd64.deb`. Once an
existing onedir has already passed the release tests and smoke checks, package
it without rebuilding:

```bash
python scripts/build_linux_deb.py --skip-app-build
```

The package installs the complete runtime under `/opt/joyread`, plus
`joyread.desktop`, the hicolor application icon, the GPL license, and the
third-party notices under the standard `/usr/share` paths. It declares the
system libraries that Qt's Linux wheels still require and supports Debian's
`amd64` and `arm64` architecture names. Install it with APT so dependencies are
resolved:

```bash
sudo apt install ./dist/JoyRead-1.0.2-linux-amd64.deb
```

The desktop entry is generated by `build_linux_desktop.py`, so its MIME list is
the same gate-aware list described in section 3b: CBZ, CBR, CB7, ZIP, RAR, 7Z,
and PDF in this release, but not disabled EPUB. Package configuration refreshes
the desktop and icon caches so JoyRead appears in file managers and their
**Open With** chooser.

The installer deliberately never writes a system or user `mimeapps.list` and
never runs `xdg-mime default`. Installing JoyRead therefore does not replace an
existing default handler. Removing it is equally conventional:

```bash
sudo apt remove joyread
```

Inspect a package without installing it using `dpkg-deb --info` and
`dpkg-deb --contents`. Before public distribution, install, upgrade, exercise
Open With for every advertised type on both X11 and Wayland, and uninstall on a
clean Ubuntu VM. Building and inspecting the archive does not exercise the
desktop's live MIME cache or chooser UI.

## 3d. Windows Inno Setup EXE

Inno Setup 7 builds the production single-file Windows installer from a verified
JoyRead onedir:

```powershell
python scripts/build_windows_inno.py
```

The default source is the production PyInstaller tree at `dist/JoyRead`, and
the result is `dist/JoyRead-<version>-windows-x86_64-setup.exe`. Once that tree
has been independently verified, skip rebuilding it with:

```powershell
python scripts/build_windows_inno.py --skip-app-build
```

`build_windows_inno.py` discovers Inno Setup 7's `ISCC.exe` from the standard
installation locations or from `JOYREAD_INNO_ISCC`. It rejects a partial
onedir before compiling: copying only `JoyRead.exe` would omit its Python
runtime, Qt libraries/plugins, resources, and bundled 7-Zip.

The setup defaults to an elevated `Program Files` install and includes a Start
menu shortcut. Its desktop shortcut task is selected by default. It presents
separate, unselected checkbox tasks for `.cbz`, `.cbr`, `.cb7`, and `.pdf`.
Selecting one adds JoyRead only as an Open With candidate and Default Apps
option; it does not write the user's protected default-app choice. `.zip`,
`.rar`, and `.7z` are always registered as Open With alternatives, so Explorer
can offer JoyRead without making users browse to `Program Files`, but they do
not appear in JoyRead's Default Apps capabilities. Registry cleanup removes
only JoyRead-owned values and empty keys. Inno copies the same dedicated
`JoyReadDocument.ico` beside the app before registering it as the shared
`JoyRead.Document` icon, so PyInstaller's internal layout does not affect
Explorer's appearance. The maintainer confirmed Windows desktop acceptance for 1.2.0, including
the previously pending installation and shell-integration checks.

The setup executable is unsigned. Sign it before public distribution. Always
smoke install, file activation, repair/upgrade, and uninstall on a clean Windows
VM; compiling the setup proves its payload but does not exercise registry
redirection, shell notification, or Windows default-app behavior.

## 4. Platform requirements

The spec refuses to build when the matching bundled 7-Zip helper is absent.
Four targets are vendored, at 7-Zip 26.02:

```text
src/joyread/resources/extractors/7zip/darwin-arm64/7zz
src/joyread/resources/extractors/7zip/linux-x86_64/7zz
src/joyread/resources/extractors/7zip/linux-arm64/7zz
src/joyread/resources/extractors/7zip/windows-x86_64/7z.exe + 7z.dll
```

Windows is the odd one out and the spec knows it. There is no `7zz.exe`: the
only standalone Windows console build is `7za.exe`, which has reduced format
support and cannot read RAR, so Windows vendors the full `7z.exe` together with
`7z.dll` — the executable opens nothing without the library beside it. Both are
listed in `binaries`, and the build fails if either is missing.

The Linux binaries are the `7zzs` (statically linked) builds from the release
tarballs, renamed to `7zz`. The dynamic `7zz` links against the build machine's
glibc and fails on older distributions, which defeats the point of bundling.
CI resolves the helper through the same `ExtractionBackendResolver` production
uses, refuses a PATH fallback, and executes `7zz i` natively on both Ubuntu
x86-64 and Ubuntu ARM64. Windows likewise executes the bundled `7z.exe` beside
its `7z.dll`; macOS executes its bundled universal helper.

Linux also needs system libraries that neither PySide6's wheels nor a
PyInstaller bundle carry. Qt ships its own libraries but links against the
distribution's C libraries, and a minimal image has almost none of them --
`libegl1`, `libgl1`, `libdbus-1-3`, `libxkbcommon0`, `libxkbcommon-x11-0`,
`libfontconfig1`, `libfreetype6`. Even the `offscreen` platform plugin needs
libEGL and libGL, because QtGui links them unconditionally; "headless" does not
mean "no GL". A desktop Ubuntu install has all of these already, which is why
this surfaces on CI runners and container images rather than on a developer
machine. The Linux CI legs install them and then load Qt on its own, before
pytest: `pytest-qt` imports PySide6 while its plugin is still loading, so a
missing library aborts pytest itself and reports "internal error" (exit 3)
without ever naming the library. Loading Qt in a separate step turns that into
the actual `cannot open shared object file` line.

Application icons are platform-native representations of the same artwork:
`JoyRead.icns` for the macOS bundle, `JoyRead.ico` (16 through 256 px) for the
Windows executable, and `JoyRead.png` (512 px RGBA) for Linux, which has no
native multi-size container. The PyInstaller spec selects the matching format
for the *executable* and fails the build if that platform's icon is absent.
Linux leaves the executable icon unset -- an ELF cannot carry one -- and relies
on the desktop entry from section 3b, which installs `JoyRead.png` into the
hicolor theme as `joyread.png`.

At *runtime* the same choice is made by `ResourceLoader.app_icon_path()`, which
is the only place a window icon should come from. Serving `.icns` everywhere
cost 53-69 ms per load against 2-3 ms for the `.ico`, because `QIcon` does not
cache and the `.icns` is 3.76 MB. Windows never displays it. Individual windows
must not call `setWindowIcon`: Qt inherits `QApplication::windowIcon()`, so each
call was decoding the same image again to reach the icon it already had.

The Windows file-association icon is intentionally a different asset:
`src/joyread/ui/resources/icons/JoyReadDocument.svg` is its reviewable vector
source, and `JoyReadDocument.ico` is the checked-in 16–256 px shell container.
Regenerate it after an SVG edit with:

```powershell
python scripts/build_windows_document_icon.py
```

The Windows Inno Setup installer places the ICO at the installation root and
points the single `JoyRead.Document` ProgID at that path. It is not the
application window icon and is not selected by `ResourceLoader.app_icon_path()`.

Windows builds also collect `ffi.dll` and `sqlite3.dll` explicitly from the
required repository Conda prefix's `Library/bin`. PyInstaller does not discover
those two transitive Python runtime DLLs reliably from a prefix environment;
the spec fails with a targeted message if they are absent instead of producing
an executable that only breaks when ctypes or SQLite is first imported.

Two exclusions run on every platform after analysis. **Unused Qt modules**: the
spec drops `qtvirtualkeyboardplugin` and the `Qt6VirtualKeyboard` /
`Qt6Quick` / `Qt6Qml*` libraries it links. JoyRead imports exactly five Qt
modules -- QtCore, QtGui, QtNetwork, QtPdf, QtWidgets -- and PyInstaller already
ships only those Python bindings, but a 34 KB input-context plugin was dragging
the whole QML stack in behind it: ~17 MB of a 180 MB bundle for an on-screen
keyboard that only activates under `QT_IM_MODULE=qtvirtualkeyboard`. The match
is on an explicit list of library stems, normalized across `.dll`/`.dylib`/
`.so.N`, rather than a substring match on "qml" or "quick" that would be one Qt
release away from removing something load-bearing. **Foreign application
icons**: only the container this platform's `ResourceLoader.app_icon_path()`
actually selects is shipped; the other two are inert, and `JoyRead.icns` alone is
3.76 MB.

Both are bundle-size measures, not startup measures -- the removed files were
never opened at runtime, and a before/after benchmark showed no change beyond
noise. Windows was smoke-tested against the trimmed bundle (launch, first paint,
and opening a CBZ in a Reader). **macOS and Linux need the same pass before a
release ships from those platforms.**

The Windows analysis also discards root-level `icuuc.dll` / `icudt*.dll`
discoveries. Qt6Core links against the Windows system ICU shim, but PyInstaller
searches the build process PATH and can otherwise copy an unrelated tool's full
ICU runtime into `_internal`. That private DLL shadows the system shim and makes
`QtCore.pyd` fail with a missing-procedure loader error. Qt libraries that live
under their own package directory are not affected by this filter.

PyInstaller's default Windows manifest declares the executable long-path
aware, but Windows still requires the machine-level `LongPathsEnabled` policy
for ordinary Python/Win32 paths beyond classic `MAX_PATH`. JoyRead does not
change that policy. When a concrete storage, import, Reader, or cache operation
is diagnosed as path-too-long, the application explains how to enable the
policy and restart, or offers moving the file/Library to a shorter directory.
Errors from a backend after the policy is already enabled are not mislabeled as
"please enable" failures.

This is diagnosis and recovery guidance, not a cache-layout migration. Durable
hidden-cache paths intentionally retain full document and page SHA-256 keys, so
an unusually deep Library can still exceed classic `MAX_PATH`; on a machine
where the policy cannot be enabled, the supported recovery is a shorter Library
location. JoyRead does not claim arbitrary over-260 paths work with the policy
disabled.

See `src/joyread/resources/extractors/7zip/README.md` for the update procedure.

Still outstanding before a non-macOS release:

- **No Intel Mac target.** The macOS binary is a universal build, so
  `darwin-x86_64/` only needs the same file copied into place.
- **Packaged non-macOS applications still need manual smoke passes.** CI runs
  the suite and the exact bundled helper on Windows and both Linux
  architectures, but it does not yet launch a packaged GUI artifact there.

## 5. Signing and notarizing a public macOS release

An unsigned, un-notarized app downloaded from the internet is quarantined by
macOS and refused with "JoyRead is damaged and can't be opened". Signing is
therefore not optional for a public build.

The spec reads two environment variables. Set both, then build:

```bash
export JOYREAD_CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export JOYREAD_ENTITLEMENTS="packaging/entitlements.plist"
python scripts/build_release.py
```

PyInstaller signs the executables it produces, but not the outer `.app`, and it
does not sign the bundled `7zz` helper. Sign the helper and the bundle
afterwards, inner to outer:

```bash
codesign --force --options runtime --timestamp \
  --sign "$JOYREAD_CODESIGN_IDENTITY" \
  "dist/JoyRead.app/Contents/Resources/joyread/resources/extractors/7zip/darwin-arm64/7zz"

codesign --force --options runtime --timestamp \
  --entitlements "$JOYREAD_ENTITLEMENTS" \
  --sign "$JOYREAD_CODESIGN_IDENTITY" \
  "dist/JoyRead.app"

codesign --verify --deep --strict --verbose=2 "dist/JoyRead.app"
```

Then notarize and staple:

```bash
ditto -c -k --keepParent "dist/JoyRead.app" "dist/JoyRead.zip"
xcrun notarytool submit "dist/JoyRead.zip" \
  --keychain-profile "JoyReadNotary" --wait
xcrun stapler staple "dist/JoyRead.app"
xcrun stapler validate "dist/JoyRead.app"
```

`--keychain-profile` refers to credentials stored once with
`xcrun notarytool store-credentials`. Ship the stapled `.app` inside a fresh
DMG or ZIP — re-zip *after* stapling, or the ticket is lost.

Finally, verify on a machine that has never seen the build:

```bash
spctl --assess --type execute --verbose=4 "dist/JoyRead.app"
```

`source=Notarized Developer ID` is the passing result. Testing on the build
machine is not sufficient: it trusts your own signing identity locally and will
pass even when a clean machine would refuse.

### Hardened Runtime notes

JoyRead spawns the bundled `7zz` as a child process. If the Hardened Runtime
blocks it, the entitlements file needs
`com.apple.security.cs.allow-unsigned-executable-memory` only as a last resort —
prefer signing the helper properly, as above, which is what makes it loadable.

The macOS bundle registers JoyRead as an alternate viewer for supported manga
archives and PDF files. Qt `QFileOpenEvent` handles Finder Open With requests;
the conditional PyObjC Cocoa dependency handles the exact Dock/Finder reopen
Apple Event without treating Cmd-Tab activation as a Library request.
After rebuilding, launch the new app once (or register it with Launch Services)
before checking the Finder Open With list; registrations from an older bundle
can remain cached temporarily.

JoyRead is single-instance per OS user and support profile. A later process
forwards all supported document paths to the existing process through a
user-only local socket and exits before opening SQLite or caches. Include these
cases in every packaged-app smoke test:

- Cold launch through Open With A: only Reader A appears, never Main as well.
- Cold launch with no document: Main appears.
- Reader A, then Open With B: both Readers remain open.
- Reader A, then reopen JoyRead: A is focused and Main is *not* created.
- Main, then Open With A: Main remains and A opens once.
- Main, then reopen JoyRead: the existing Main is focused.
- Open a Reader from the shelf, then close Main: that Reader closes with it.
- Open With A while Main is open, then close Main: A remains open.
- Open a shelf Reader for A, then Open With A, then close Main: A remains open,
  because the OS request promoted it to a root window.

The app does not technically need to live in `/Applications`: launching it or
registering it explicitly is enough for local builds. Public distributions
should still instruct users to move JoyRead into `/Applications`, which is the
standard location macOS scans and avoids associations pointing at a temporary
or deleted build directory.

## 6. Release checklist

- Commit and tag the exact source used for the build.
- Confirm `pyproject.toml` version and release notes.
- Include the JoyRead license, third-party notices, font OFL, and 7-Zip license.
- Run tests and smoke tests on every advertised platform.
- Verify writable data is outside the installation directory.
- Record known limitations in the release notes.


## 1.0.2 localization release checks

The spec explicitly includes `qtbase_zh_CN.qm` and `qtbase_ja.qm` at Qt's frozen
translation path, and fails if either is absent. This affects application widget
translations only: executable/bundle names, installer names, Linux desktop entry
and registration IDs stay JoyRead. The in-app name follows the active language.

Before releasing 1.0.2, check on macOS, Windows, and Ubuntu (GNOME Wayland and X11
where available): fresh profile language detection; old explicit language retained;
unsupported OS language → English; Chinese → 欣阅 and Japanese → ジョイヨミ;
manual switching with multiple Readers and a password dialog open; native file
picker text; 100%/200% scaling and CJK font coverage; direct document activation;
unchanged taskbar/dock grouping and storage paths. Confirm standard Qt widget
translations are present in each frozen artifact. OS-owned dialog buttons may
follow OS language. System language changes are picked up on next launch.

Build packages run [34670838470](https://github.com/CtfTnk/JoyRead/actions/runs/34670838470)
built the 1.0.2 installers from `88ff97e2e386ab749f5a024ab65fe1c7f662f826`
on all four targets. Shipping-configuration tests passed: macOS arm64 1553
(11 skipped), Windows x64 1544 (20 skipped), and Linux amd64/arm64 1554 each
(10 skipped each). Installer SHA-256 hashes were verified against CI manifests.
See the [three-language release notes](releases/v1.0.2.md).

Desktop-native checks remain pending; successful builds and source/offscreen
tests do not certify those behaviors. The macOS artifact is ad-hoc signed,
not Apple-notarized; the Windows installer has no trusted code signature.

## Manual cloud packages (no Release)

`Build packages` (`.github/workflows/build-packages.yml`) runs only through
`workflow_dispatch`; pushes continue to run Tests without packaging. Once the
workflow is on main, open Actions → Build packages → Run workflow, select a
branch and `all`, `macos`, `windows`, or `ubuntu`. Ubuntu builds both amd64 and
arm64 on Ubuntu **22.04** runners, the Linux compatibility baseline. Every matrix job checks out the dispatch SHA, tests the shipping
configuration, builds the app, and invokes the existing platform installer script.

Cloud builds use Python 3.12.9 from Miniforge and dependencies from
`pyproject.toml`; Windows installs checksum-verified Inno Setup 7.1.0. Each
successful job uploads its installer, `SHA256SUMS`, and `build-info.json` (version,
commit, target, Python) to Actions Artifacts for 14 days. There are no release,
tag, or repository-write steps. macOS uses the existing ad-hoc signing workflow;
these artifacts are candidates for desktop validation, not notarized releases.

### Ubuntu 22.04 compatibility rebuild

The original 1.0.2 Linux packages were built on Ubuntu 24.04. In particular,
ARM64 Shiboken 6.11 needs GLIBC_2.38 and cannot start on Ubuntu 22.04's glibc
2.35. ARM64 PySide6 is constrained to 6.8.0.2 in `pyproject.toml`, the last
release with manylinux_2_31 ARM64 wheels; later wheels require manylinux_2_39.
Other platforms retain their existing dependency requirement. Refresh the
release environment after changing dependencies.
The cover editor forwards signals through Qt signal-to-signal connections so
Qt 6.8 disconnects them with the receiver during teardown. UI tests send a
real `QEnterEvent` rather than a generic event with the Enter type.

Linux source tests and packaging set `LD_LIBRARY_PATH=$CONDA_PREFIX/lib` so
Qt and Conda SQLite/ICU use the same C++ runtime. Otherwise Qt can load the
host's older `libstdc++` first, causing ICU to fail with `CXXABI_1.3.15 not found`.
The spec explicitly bundles Conda's `libstdc++.so.6` and `libgcc_s.so.1`.
The installed-package smoke check removes this environment override and must
resolve the bundled runtime on its own.

The Debian package revision is `1` (for example, package version `1.0.2-1`),
while the app version and download filenames remain `1.0.2`. This lets APT
upgrade the original package without a new cross-platform app release.
The package declares `libc6 (>= 2.35)`; building on a newer distribution does
not by itself preserve that baseline.

After packaging, CI installs the DEB with APT and runs
`python scripts/verify_linux_package.py /opt/joyread`. The verifier rejects ELF
libraries requiring glibc newer than 2.35 and launches the installed executable
with an isolated temporary library until it logs `process.ready`. It removes
build-environment library overrides, so the check cannot accidentally use
Conda's Python or Shiboken. This validates the frozen package's loader/startup,
not interactive reading or desktop integration. Build metadata includes the
Ubuntu release, glibc, PySide6, and Debian package versions.

Candidate run [34746301590](https://github.com/CtfTnk/JoyRead/actions/runs/34746301590)
validated source `ab9e82cf5cd4f39530758d642f9e59c709dfa5d9`: both architectures
passed 1557 tests (10 skipped), APT installation, and isolated installed startup.
All 292 amd64 and 291 arm64 ELF files satisfied the glibc 2.35 baseline.
This is package-level automated validation; interactive desktop checks remain
separate.

For a same-version rebuild, preserve macOS/Windows assets and the existing tag.
Replace Linux assets only after both target jobs pass, and update the combined
`SHA256SUMS`, per-target `build-info.json`, and dated release notes together.
Retain each platform's actual source commit in that metadata.

Windows CD exposed a storage-reset shutdown race: database close previously
returned after a five-second join even with the SQLite actor still alive.
Database shutdown now waits for release; an explicitly supplied timeout raises
instead of reporting success. Storage reset/move must never proceed after a
close timeout. `tests/unit/test_database_shutdown.py` covers slow release and
retrying a timed-out close in addition to the real storage-reset regression.

The Windows spec retains the installed libffi DLL basename (`ffi.dll`,
`ffi-*.dll`, or `libffi*.dll`) so both defaults-based Conda and conda-forge /
Miniforge environments are supported. SQLite's `sqlite3.dll` remains required.


## Historical 1.1.0rc1 local preparation (superseded by 1.1.0)

- App/Python version and artifact filenames: `1.1.0rc1`.
- Proposed GitHub tag: `v1.1.0rc1`; mark the release **Prerelease**, not Latest.
- macOS short version: `1.1.0`; build version: `1.1.0fc1`. `JoyReadVersion`
  retains `1.1.0rc1` for DMG naming and provenance.
- Debian package version: `1.1.0~rc1-1`, which sorts before `1.1.0-1`.
- Windows AppVersion: `1.1.0rc1`; numeric file version: `1.1.0.0`.
- Version transformations live in `packaging/version_info.py`; project version
  still comes from `pyproject.toml`, with the runtime constant checked by tests.
- Scope: custom shelf order/selection/dragging, incremental shelf presentation,
  cached Gaussian-style blur, disabled-switch styling and HiDPI thumbnails.
- Database schema advances to 14. Back up the library before prerelease testing;
  rollback must restore that backup, since 1.0.2 does not expose saved custom order.
- Run Build packages on the exact committed candidate on macOS arm64, Windows
  x64 and Ubuntu amd64/arm64. Keep installer checksums and source metadata.
- Validate real mouse capture, outside release/cancel, imported files, migration,
  per-shelf ordering, fractional scaling, cross-monitor moves, upgrade/uninstall.
- Source tests and macOS synthetic checks do not establish Windows/Linux desktop
  acceptance. Record each actual build/test result in the candidate release notes.
- Existing README download links remain stable until candidate assets exist.

Version rules: [Apple bundle keys](https://developer.apple.com/library/archive/documentation/General/Reference/InfoPlistKeyReference/Articles/CoreFoundationKeys.html),
[Debian version ordering](https://www.debian.org/doc/debian-policy/ch-controlfields.html#version).


## 1.1.0 publication

The user selected 1.1.0 as the published version after local RC1 evaluation.
Application, DMG and Windows versions are 1.1.0; Debian is 1.1.0-1.
Rebuild all four targets from the same frozen source commit. Publish as a normal
GitHub release, with the v1.0.1 macOS first-launch guide included in all three
languages. Do not relabel or upload the older RC1 binaries as 1.1.0.


## 1.2.0 publication

Publish 1.2.0 as a stable release. Rebuild macOS arm64, Windows x64 and
Ubuntu amd64/arm64 from one frozen commit; keep its SHA in build-info.json and
verify each installer against its CI SHA-256 manifest before publishing.
The maintainer confirmed desktop acceptance on 2026-09-18; previous pending
validation statements above are historical records, not the current status.
No database migration is added over 1.1.0. The release includes progress preview
(180 ms hover, 320 ms fade, 80% opacity), shared thumbnail caching, global
preloading, adaptive menus/settings, saved window sizes and sidebar sections.
Chinese and Japanese release notes are complete translations in initially
collapsed details blocks; English remains expanded, with the macOS 1.0.1
first-launch instructions retained in all three languages.

### Publish verified cloud artifacts

`Publish verified release` (`.github/workflows/publish-release.yml`) is an
explicit `workflow_dispatch` step after a successful all-target `Build packages`
run. Pass that run's numeric ID as `build_run_id`. It checks the build workflow,
success and source repository, reads the version from the built commit, requires
all four target manifests to match that commit/version, and verifies installer
SHA-256 hashes. It publishes the complete three-language notes from the dispatch
checkout, so finish the validation tables before dispatching.

The workflow creates the release tag at the built commit, uploads all four
installers plus aggregate checksums/provenance to a draft, compares GitHub's
uploaded asset digests, and only then publishes it as Latest. Existing tags or
releases are never overwritten automatically; inspect a failed draft before
retrying. Builds, pushes and tags still do not trigger publication by themselves.

1.2.0 installers were built from `20eabf1616eda690a293b1640adb3b89ef04a8ee` in
[Build packages 35311131525](https://github.com/CtfTnk/JoyRead/actions/runs/35311131525). Shipping tests passed:
macOS arm64 1754 (12 skipped), Windows x64 1745 (21 skipped), Linux amd64
and arm64 1756 each (10 skipped each). Both Linux packages passed APT and
installed-application validation. See the complete three-language release notes.
