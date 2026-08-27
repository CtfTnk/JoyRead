# JoyRead Packaging Guide

This guide builds JoyRead v1.0.0 with PyInstaller. Build each target on its own
operating system; PyInstaller does not cross-compile desktop apps.

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

See `src/joyread/resources/extractors/7zip/README.md` for the update procedure.

Still outstanding before a non-macOS release:

- **Windows needs a `.ico`.** Only `JoyRead.icns` exists, and the spec passes
  `icon=None` off macOS, so a Windows build currently gets PyInstaller's default
  icon. The build succeeds; it just is not branded.
- **No Intel Mac target.** The macOS binary is a universal build, so
  `darwin-x86_64/` only needs the same file copied into place.
- **Each non-macOS binary is unexecuted by CI**, which runs on `macos-14` and
  smoke-tests the Darwin helper only. Run the archive tests on the target
  platform before advertising it.

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
