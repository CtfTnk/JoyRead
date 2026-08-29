# Packaged Startup, Bundle Trimming, and Windows MSI

Date: 2026-08-29

Startup commit: `21efd194acaebe01e0808cc91eb7500902b8a84b`

MSI commit: `c796ccfe991d06d05aef2991eebd2bb6624ae688`

## Summary

| Problem | Root cause | Outcome |
| --- | --- | --- |
| An already-running JoyRead still took about a second to handle Open With | The secondary process imported the primary runtime before single-instance arbitration | Primary-only imports now occur after the process role is known |
| Packaged Library startup took about three seconds warm | Import work, duplicate icon decoding, and romanizer import contention all sat on the critical path | Warm Windows first paint now measures about 1.03 seconds from process spawn |
| The Windows bundle carried unused Qt QML/Quick binaries and foreign icon containers | Qt's virtual-keyboard plugin pulled in a transitive QML stack; all three icon formats were copied as data | The final Windows onedir is 396 files / 164.94 MiB, down from the earlier 398 files / about 180 MiB baseline |
| Windows had no reproducible installer or shell registration | Only the PyInstaller onedir artifact existed | A pinned WiX build now produces a 71.07 MiB MSI with Start menu and Open With integration |
| The release bundle omitted JoyRead's GPL license | Only third-party notices were passed to PyInstaller | `LICENSE` and `THIRD_PARTY_NOTICES.txt` are both present in onedir and MSI payloads |

Headline measurements on this Windows host:

| Scenario | Earlier packaged baseline | Final packaged build |
| --- | ---:| ---: |
| Warm Library startup | 3.04–3.13 s to `process.ready` | 1.03 s median from spawn to first paint |
| Open With, primary already running | 0.88–1.06 s | 471.2 ms median process lifetime |
| Single-instance dispatch inside the primary | 3–4 ms | Unchanged; it was not the bottleneck |
| CBZ document-open lifecycle | 39–47 ms | Unchanged; it was not the bottleneck |

The final Library figure is the sum of the external `spawn → origin` median
(369.8 ms) and the internal `origin → first_paint` median (664.2 ms), over five
warm runs. It is a stricter endpoint than the old `process.ready` measurement:
the first Qt paint has actually occurred.

## Background

JoyRead uses a single-instance process model. A second process launched through
Open With only needs to send a typed launch intent to the existing primary and
exit. It must still create enough Qt state to support the cross-platform launch
gate and local socket, but it must not construct storage services or windows.

Python imports execute module-level code before `run()` can arbitrate the
process role. The old flow therefore looked efficient inside `run()` while
still paying for the whole primary import graph before `run()` was callable:

```text
main.py
  └─ import bootstrap
       └─ import AppContext, MainWindow, reader/archive stack, PIL, py7zr ...
            └─ run()
                 └─ determine PRIMARY or SECONDARY
```

The corrected flow treats module scope as part of the startup budget:

```text
main.py records trace origin
  └─ import lightweight arbitration surface
       └─ determine PRIMARY or SECONDARY
            ├─ SECONDARY: forward and exit
            └─ PRIMARY: import AppContext, windows, recovery UI and services
```

## Startup work

### Startup tracing

`startup_trace.py` is stdlib-only and captures the earliest JoyRead-controlled
timestamp. It buffers milestones until logging is available and records:

```text
origin → bootstrap_imported → qt_app_created → role_resolved
       → context_ready → resources_ready → window_constructed
       → window_shown → first_paint
```

The external benchmark records process spawn separately. That is necessary
because the PyInstaller bootloader and interpreter initialization happen before
any Python code can observe itself.

The benchmark validates behavior as well as time. A document launch is a failed
measurement if it falls back to the Library, and an Open With run is failed if
the primary does not dispatch and finish the forwarded intent. The primary-side
check waits for the terminal delivery record and reads only log bytes appended
for that run; otherwise an earlier successful run could mask a later failure.

### Lightweight secondary imports

`bootstrap.py` now leaves only arbitration dependencies at module scope.
AppContext, the window manager, launch coordinator, recovery UI, novel provider,
and tag romanizer enter through function-local imports on primary-only paths.

`SUPPORTED_READER_EXTENSIONS` is imported from the Qt-free
`core.file_types` owner. Importing the identical re-export from `core.reader`
would bring the archive stack back into every secondary process. Subprocess
tests guard both module import and a complete simulated SECONDARY execution.

### Icon and warm-up changes

`ResourceLoader` chooses the native icon container:

| Platform | Runtime icon |
| --- | --- |
| Windows | `JoyRead.ico` |
| macOS | `JoyRead.icns` |
| Linux/other | `JoyRead.png` |

Windows previously decoded the 3.76 MiB ICNS at roughly 53–69 ms per `QIcon`
construction, compared with 2–3 ms for the ICO. Individual windows no longer
decode the icon again; they inherit `QApplication.windowIcon()`.

The romanizer warm-up remains asynchronous but begins after the first window is
shown. Moving primary imports to function scope had placed them beside the
romanizer's large dictionary imports, producing about 295 ms of import-lock and
disk contention. The new ordering keeps that work off window construction.

### Why hidden views were not made lazy

Direct profiling invalidated the initial assumption. SettingsView took about
13 ms and CoverEditorOverlay about 8 ms. Most of the first MainWindow cost was a
one-time Qt style/font/layout realization triggered at `QScrollArea.setWidget`,
not the intrinsic construction of the hidden views. A second MainWindow in the
same process measured about 185 ms.

Lazifying those views would move roughly 21 ms while adding lifecycle and signal
wiring complexity. It was rejected rather than shipped as an unmeasured patch.

## What “trimmed” means

`trimmed` does not mean minified Python, compressed source, or a partial JoyRead
feature set. It means post-processing PyInstaller's Analysis tables before the
PYZ and COLLECT stages.

### Unused Qt dependency chain

PyInstaller correctly found only the five Qt Python bindings JoyRead imports:

```text
QtCore  QtGui  QtNetwork  QtPdf  QtWidgets
```

However, Qt also collected `qtvirtualkeyboardplugin`. That 34 KiB input-context
plugin links `Qt6VirtualKeyboard`, which links the Quick/QML chain. JoyRead does
not enable `QT_IM_MODULE=qtvirtualkeyboard` and has no QML UI, so the plugin and
its private dependency chain are unreachable in this desktop application.

The spec removes only this explicit allow-list of destination library stems:

```text
qtvirtualkeyboardplugin
qt6virtualkeyboard
qt6quick
qt6qml
qt6qmlmeta
qt6qmlmodels
qt6qmlworkerscript
```

`_library_stem()` normalizes platform naming before comparison:

```text
Qt6Quick.dll       → qt6quick
libQt6Quick.so.6   → qt6quick
libQt6Quick.dylib  → qt6quick
```

It removes an optional leading `lib`, case-folds the name, and strips everything
from the first dot onward. The comparison is exact against the set above. It is
deliberately not a substring rule such as `if "qml" in name`, because that could
silently delete a future load-bearing Qt library.

### Platform icon trimming

PyInstaller receives the entire icon directory as data, so all three application
containers would otherwise ship. The spec keeps exactly one basename according
to the build platform and removes the other two from `a.datas`:

```text
Windows → JoyRead.ico
macOS   → JoyRead.icns
Linux   → JoyRead.png
```

This is safe because `ResourceLoader.app_icon_path()` makes the same platform
decision at runtime, and the spec fails the build if the required executable
icon is missing.

### What is not trimmed

The final Windows bundle was inspected to confirm these remain:

```text
Qt6Core.dll  Qt6Gui.dll  Qt6Widgets.dll  Qt6Network.dll  Qt6Pdf.dll
ffi.dll      sqlite3.dll
7z.exe       7z.dll
JoyRead.ico  LICENSE     THIRD_PARTY_NOTICES.txt
```

The separate Windows ICU filter is a loader-safety rule, not a size trim. It
removes only root-level `icuuc.dll`/`icudt*.dll` accidentally discovered from
the build process PATH. Such a foreign ICU previously shadowed Windows' system
shim and made `QtCore.pyd` fail with a missing-procedure error.

### Trim result and limitation

The earlier comparison build was about 180 MiB / 398 files. The final release,
after also adding the GPL license, is 164.94 MiB / 396 files. The QML chain is
absent, as are the two foreign icon containers.

This is primarily a bundle-size and scanning-surface improvement. Those Qt DLLs
were not loaded, so trimming them did not produce a measurable warm-startup win.
Windows launch, first paint, and CBZ Reader activation were smoke-tested. Native
macOS and Linux trimmed builds still need their own smoke passes.

## Windows MSI

`build_windows_msi.py` wraps the verified `dist/JoyRead` tree rather than
rebuilding or rearranging application files inside the installer definition.
The repository pins WiX 5.0.2 as a local .NET tool.

The MSI:

- is x64 and embeds its cabinet;
- is dual-purpose but defaults to per-user installation;
- creates a Start menu shortcut;
- registers JoyRead as an Open With/Default Apps candidate for `.cbz`, `.cbr`,
  `.cb7`, and `.pdf`;
- does not claim generic `.zip`, `.rar`, or `.7z`;
- does not write the protected user-default association;
- supports major upgrades through a stable UpgradeCode;
- includes the GPL license and third-party notices.

WiX 5 was selected because WiX 6 and 7 binary releases introduced the Open
Source Maintenance Fee/EULA. WiX 5 no longer receives consumer security
updates. Moving to a current WiX release therefore requires an explicit release
owner licensing decision, not a silent dependency bump.

The final unsigned artifact is:

```text
dist/JoyRead-1.0.0-windows-x86_64.msi
size:   74,518,016 bytes (71.07 MiB)
SHA256: 67AE7B7EA967F19AFB399CB865752AAEE471BEB10D696C5CD41D91F74D0AF307
```

WiX ICE validation reports no errors. It reports four ICE60 warnings for the
four bundled Noto OTF files because they are unversioned application data with
no MSI language metadata. Explicitly assigning a fake neutral language was
tested and rejected: WiX then warned that the authored language disagreed with
the real file and could produce incorrect repair/patch versioning.

Administrative extraction completed with exit code 0. The extracted EXE hash
matched the onedir EXE exactly:

```text
A3078900F785F8619C9559764C8AAF6B5EEAC728705B784E98EE98EDA74769F2
```

The extracted payload passed Library, direct-file, and existing-primary Open
With startup smoke tests. Administrative extraction does not install shell
registrations, so a real install/repair/uninstall pass remains required on a
clean Windows VM before public release.

## Results and tests

- Full suite: **1431 passed, 17 skipped, 1 expected duplicate-ZIP warning**.
- Startup/import/resource targeted suite: passed.
- MSI authoring tests: 6 passed.
- Final Windows onedir: 396 files, 172,952,226 bytes.
- Final MSI: 74,518,016 bytes; administrative extraction exit 0.
- Extracted EXE and source onedir EXE: identical SHA-256.
- WiX ICE: 0 errors, 4 explained OTF ICE60 warnings.

## Rejected alternatives

| Alternative | Why rejected |
| --- | --- |
| PyInstaller `onefile` | It extracts the Qt application on every launch and normally makes startup slower |
| Flat onedir layout | Measured within 1.1% of `_internal`; it changes organization, not file count or loading |
| Lazy Settings/Cover overlays | Their measured combined cost was about 21 ms; Qt's first realization dominated |
| Broad `qml`/`quick` substring deletion | Too easy to remove a future required library; exact normalized stems are auditable |
| Removing all optional-looking Qt files | Static appearance is not proof of runtime reachability |
| Assigning fake MSI language metadata to OTF files | Replaced ICE60 with a more meaningful repair/versioning warning |
| Registering `.zip/.rar/.7z` in Windows | Would make a reader compete with archive managers for generic containers |
| Setting JoyRead as the default during install | Modern Windows requires the user to choose defaults through system UI |

## Known-open items

- MSI and EXE are unsigned. Public distribution requires code signing.
- Run install, file activation, repair/upgrade, and uninstall on a clean Windows
  VM. This work deliberately did not change the current developer's registry.
- Run trimmed bundle smoke tests on native macOS and Linux builds.
- Decide whether to accept the current WiX EULA/maintenance terms or move to a
  different maintained installer tool before treating the MSI pipeline as a
  long-term public-release dependency.
- Cold-cache startup remains variable. The single earlier 8.46-second result is
  not a percentile and Defender was not independently isolated.
- A QCoreApplication-only secondary remains a possible Windows/Linux follow-up,
  but cannot replace QApplication universally because macOS Finder activation
  depends on Qt file-open events.

## How to verify

From the activated repository Conda environment:

```powershell
python -m pytest -q
python scripts/build_release.py --skip-tests
python scripts/build_windows_msi.py --skip-app-build
dotnet tool run wix -- msi validate dist/JoyRead-1.0.0-windows-x86_64.msi
python scripts/bench_startup.py --exe dist/JoyRead/JoyRead.exe --runs 5
python scripts/bench_startup.py --exe dist/JoyRead/JoyRead.exe `
  --scenario openwith --runs 5 --document C:\path\to\book.cbz
```

The ICE validation command requires access to the Windows Installer service.
Run it from a normal developer shell rather than a restricted sandbox.

## Glossary

- **Analysis TOC:** PyInstaller's table of collected binaries and data files
  before the final executable/archive is assembled.
- **ICE:** Windows Installer Internal Consistency Evaluator. It checks MSI
  database rules that ordinary WiX compilation does not fully validate.
- **Open With secondary:** A short-lived JoyRead process that forwards a file
  path to an existing primary process and exits.
- **Primary runtime:** AppContext, services, repositories, ViewModels and top-
  level windows needed only by the process that owns the application session.
- **PYZ:** PyInstaller's compressed Python-module archive.
- **Warm run:** A launch performed after OS filesystem and security-scanner
  caches may already contain the application files.

## File map

| File | Role |
| --- | --- |
| [`../../src/joyread/app/main.py`](../../src/joyread/app/main.py) | Earliest trace entry and application command entrypoint |
| [`../../src/joyread/app/startup_trace.py`](../../src/joyread/app/startup_trace.py) | Buffered startup milestones |
| [`../../src/joyread/app/bootstrap.py`](../../src/joyread/app/bootstrap.py) | Lightweight arbitration and deferred primary composition |
| [`../../src/joyread/app/windows/manager.py`](../../src/joyread/app/windows/manager.py) | Window construction/show milestone seam |
| [`../../src/joyread/infrastructure/resources/resource_loader.py`](../../src/joyread/infrastructure/resources/resource_loader.py) | Platform-native runtime icon selection |
| [`../../packaging/joyread.spec`](../../packaging/joyread.spec) | PyInstaller collection and trimming rules |
| [`../../scripts/bench_startup.py`](../../scripts/bench_startup.py) | Behavior-aware startup benchmark |
| [`../../packaging/windows/JoyRead.wxs`](../../packaging/windows/JoyRead.wxs) | MSI directories, payload, shortcut and file-association authoring |
| [`../../scripts/build_windows_msi.py`](../../scripts/build_windows_msi.py) | Reproducible MSI build entrypoint |
| [`../../tests/unit/test_startup_import_cost.py`](../../tests/unit/test_startup_import_cost.py) | Secondary import-boundary regression guards |
| [`../../tests/unit/test_windows_msi_packaging.py`](../../tests/unit/test_windows_msi_packaging.py) | MSI scope and association policy guards |
