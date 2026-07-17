# JoyRead Packaging Guide

This guide builds a local v0.1.0 preview with PyInstaller. Build each target
on its own operating system; PyInstaller does not cross-compile desktop apps.

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

EPUB code remains in the repository but access is intentionally disabled for
this release until the reader is complete.

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

This is an unsigned local preview. Open it once from Finder and repeat the
smoke test against a new Library. Inspect `Contents/MacOS` and
`Contents/Frameworks` if startup fails; JoyRead logs remain under the normal
platform user log directory.

## 4. Platform requirements

The spec intentionally refuses to build when the matching bundled 7-Zip
helper is absent. Add the executable and its license files under:

```text
src/joyread/resources/extractors/7zip/darwin-arm64/7zz
src/joyread/resources/extractors/7zip/darwin-x86_64/7zz
src/joyread/resources/extractors/7zip/windows-x86_64/7zz.exe
src/joyread/resources/extractors/7zip/linux-x86_64/7zz
```

The repository currently contains only the Darwin ARM64 target directory.
Windows also needs a proper `.ico` application icon before public release.

## 5. Public macOS release

The current artifact is suitable for local testing, not public distribution.
A public macOS build still needs:

1. Apple Developer ID Application signing for the app and bundled helper.
2. Hardened Runtime validation and any required entitlements.
3. Apple notarization and stapling.
4. A DMG or ZIP distribution artifact.
5. A clean-machine Gatekeeper test.

The macOS bundle registers JoyRead as an alternate viewer for supported manga
archives and PDF files. The Qt `QFileOpenEvent` bridge handles Finder
Open With requests both during startup and while JoyRead is already running.
After rebuilding, launch the new app once (or register it with Launch Services)
before checking the Finder Open With list; registrations from an older bundle
can remain cached temporarily.

The app does not technically need to live in `/Applications`: launching it or
registering it explicitly is enough for local builds. Public distributions
should still instruct users to move JoyRead into `/Applications`, which is the
standard location macOS scans and avoids associations pointing at a temporary
or deleted build directory.

## 6. Release checklist

- Commit and tag the exact source used for the build.
- Confirm `pyproject.toml` version and release notes.
- Include the JoyRead license, font OFL, and 7-Zip license.
- Run tests and smoke tests on every advertised platform.
- Verify writable data is outside the installation directory.
- Record known limitations in the release notes.
