# JoyRead Packaging Guide

This guide builds the production JoyRead v1.0.1 artifacts with PyInstaller.
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

Produces `dist/JoyRead-<version>-macos-arm64.dmg` containing the app, an
`Applications` symlink, and the trilingual first-launch note from
`packaging/dmg/`.

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
That is what the note in the disk image explains, in English, Japanese and
Simplified Chinese.

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

**This has not been verified on a real Ubuntu desktop yet.** The file is
generated, validated by shape, and guarded by tests, but nobody has right-clicked
a `.cbz` in Nautilus and watched a Reader open.

## 3c. Ubuntu/Debian .deb

Build the native package on the Linux architecture it will run on:

```bash
python scripts/build_linux_deb.py
```

This rebuilds the PyInstaller onedir without rerunning tests, validates it, and
produces `dist/JoyRead-<version>-linux-<debian-architecture>.deb`. On the
current x86-64 target that is `dist/JoyRead-1.0.1-linux-amd64.deb`. Once an
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
sudo apt install ./dist/JoyRead-1.0.1-linux-amd64.deb
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
Explorer's appearance. A clean Windows VM/Sandbox still needs to exercise
installation, Open With, upgrade, and uninstall before this becomes a release
artifact.

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
