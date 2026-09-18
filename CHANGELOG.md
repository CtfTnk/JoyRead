# Changelog

All notable changes to JoyRead are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Independently collapse/expand the Bookshelf and Collections sidebar sections
  by clicking their headers or pressing Enter/Space. Remember state across
  restarts without changing the active shelf, selection or privacy visibility.
- Remember normal Library and independent Reader window sizes separately;
  General settings can restore both defaults. Small screens fit the window to
  available space, respecting its minimum size and keeping the title bar at
  the available area's top-left edge.
- Show 1-based page numbers below Detail, Reader Topic and cover-picker
  thumbnails, including unloaded pages; numbers also select their page.
- Keep custom menus within the visible application window, flipping at edges.
  Button dropdowns retain their anchor, and oversized menus scroll with
  ellipsis/tooltips for clipped labels.

### Performance

- Reprioritize thumbnail loading as the viewport moves, with visible pages
  first and directional prefetch next. Finish one bounded in-flight batch
  instead of repeatedly cancelling and duplicating work on scroll.
- Deliver unchanged visible thumbnails only once; retain a bounded LRU of
  decoded offscreen thumbnails/widgets for reverse scrolling and cache the
  DPR-aware checkerboard placeholder.

### Fixes

- Give sidebar section headers distinct hover/pressed feedback, clear the fill
  when dragging outside, and avoid refreshing unchanged sections on toggles.
- Allow the expanded shelf search to shrink while retaining format/tag filters.
- Stack settings controls below their labels when a row is too narrow, with
  wrapping labels and content-sized action buttons.
- Let Qt process native window mouse events before intercepting widget gestures,
  preventing stale pressed/hover state and a stuck hand cursor after book clicks.
- Keep ordinary shelf clicks on Qt's implicit mouse capture; acquire explicit
  viewport capture only after rectangle selection or reorder starts. Release it
  immediately on cancellation and recover from a missing trailing release.
- Clear shelf selection when clicking toolbar blank space or its title, while
  preserving Shift-click, search input and button behavior.

## [1.1.0] — 2026-09-17

[Release notes — English / 中文 / 日本語](docs/releases/v1.1.0.md)

### HiDPI

- Generate bookshelf covers and Detail/Reader Topic thumbnails at the window's device
  pixel ratio, including fractional scaling, while preserving logical layout.
- Refresh image requests when moving between displays; retain current images
  until replacements arrive and ignore results from older density requests.
- Reuse sufficiently large generated covers and retain cached density variants.
  Existing custom crops stay authoritative; newly saved crops use the current
  density and the Detail cover's resolution.

### Bug Fixes

- Share a downsampled Pillow Gaussian approximation between the file-drop
  background and book-card/list-row drag placeholders,
  with logical-pixel radii that remain consistent on HiDPI screens.
- Cache the normal and confirming blur states once per snapshot; painting,
  hover, re-entry, and fade-out do not rerun image filtering.
- Smoothly blend the backdrop and confirmation scrim without exposing the sharp
  shelf midway through the transition. Preserve opacity on an early drop and
  freeze the current mix when dismissing.

### Library

- Add Custom sorting with independent saved order and sort preferences for All,
  Favourites, Hidden Space, and each collection. Recent keeps reading-time order.
- Add rectangle selection and single/multiple book dragging in both grid and
  list views, with a cached card-stack preview, one insertion placeholder,
  scrolling, and cancellation without changing saved order.
- Save order atomically in SQLite migration 14; retain legacy sort defaults,
  prepend newly added members, and restore the committed order if saving fails.
- Translate the new sorting controls and failure message into English, Chinese,
  and Japanese. Native Windows/Linux gesture validation remains pending.

### Performance

- Reuse bookshelf cards and list rows when sorting or changing search results;
  create/remove controls only for books entering/leaving the result.
- Update only changed metadata and selection styling instead of refreshing all
  card contents and covers after each click.
- Reuse displayed cover pixmaps, while explicitly refreshing same-path cover
  edits; coalesce cover resolution requests for changed books.

## [1.0.2] — 2026-09-12

[Release notes — English / 中文 / 日本語](docs/releases/v1.0.2.md)

### Linux packaging rebuild — 2026-09-13

- Rebuilt amd64 and arm64 packages on Ubuntu 22.04 as Debian version `1.0.2-1`;
  the application version remains `1.0.2`.
- Use Ubuntu 22.04-compatible ARM64 PySide6 wheels and bundle the matching
  Conda C++ runtime to resolve GLIBC_2.38 and CXXABI_1.3.15 startup failures.
- Fix cover-editor signal forwarding during teardown on Qt 6.8 and use a
  correctly typed Qt enter event in the UI test.
- Both architectures passed 1557 tests (10 skipped), APT installation, glibc
  baseline validation, and installed-executable startup in run `34746301590`.
- The graphical installer Preparing issue remains unresolved; use APT.

### Localization

- Added Follow system as the default language preference on Windows, macOS,
  and Ubuntu/Linux. Supported UI language preferences resolve in order to
  English, Simplified Chinese, or Japanese, with English fallback. Existing
  explicit language selections are preserved.
- Localized the in-app display name: 欣阅, JoyRead, and ジョイヨミ. Internal
  identifiers, user-data paths, executable names, and desktop/installer names
  remain unchanged.
- Fixed untranslated Hidden Space startup controls, storage-recovery UI,
  tag-operation results, reader password/error messages, and view/sort tooltips.
- Retranslate open controls, dialogs, menus, and Readers in place without
  replacing password input, user text, reading state, or active tasks.
- Merge custom locale files per key over bundled translations and retain
  English fallback. Include Qt standard-widget translations for Chinese/Japanese.

### Validation

- Build packages run `34670838470` built all four installers from `88ff97e`:
  macOS arm64 (1553 tests passed, 11 skipped), Windows x64 (1544/20), Linux
  amd64 and arm64 (1554/10 each). These are shipping-configuration offscreen
  checks; native desktop validation remains pending as listed in the release notes.

### Fixes and packaging

- Improved archive cache ownership, staging, and resource cleanup.
- Wait for SQLite to release its files before resetting the library, fixing
  the Windows shutdown/reset race.
- Added manual CI package builds for macOS arm64, Windows x64, and Linux
  amd64/arm64, with source provenance and installer checksums.
- Fixed UTF-8 locale fixture loading and Miniforge libffi DLL packaging on Windows.
- Reworked the README with the app icon, download links, illustrated usage,
  and English, Chinese, and Japanese introductions.

## [1.0.1] — 2026-09-01

### Bug Fixes

- Fixed title-bar clicks restoring a maximized window before a drag began,
  which also broke double-click zoom behavior.
- Fixed maximized-window dragging flickering, pausing, or jumping away from the
  grab point on macOS.
- Let Linux and Windows restore remembered window geometry through their window
  manager instead of racing it with a second client-side placement.
- Preserved the user-selected size of a resized tiled window when starting a
  later title-bar drag instead of snapping back to stale normal geometry.
- Increased the minimum Library width so the shelf no longer collapses to one
  column with the sidebar and vertical scrollbar open.

### Maintenance

- Removed the development-only Windows/Linux title-control preview from
  Settings while preserving each platform's production window controls.
- Removed the development JSON-manifest picker from the production import menu;
  manifest import remains available to scripts and internal tooling.

### Known limitations

Supersedes the 1.0.0 list, which is left below as it stood at the time.

- EPUB reading is present in the codebase but disabled; the novel reader is not
  finished.
- Pages extracted from encrypted archives are cached unencrypted. Settings →
  Privacy → "Delete cached pages when closing" is on by default so they do not
  outlive the session.
- Archive passwords are passed to the bundled 7-Zip executable on its command
  line and are therefore readable by same-user processes during extraction.
  This covers 7z, RAR, and ZIP using the legacy ZipCrypto cipher. AES-encrypted
  ZIP is unaffected, being decrypted in-process.
- Solid-RAR performance is unverified; no solid RAR fixture was available. The
  7z thread policy is applied to RAR by extrapolation.
- **None of the three builds is code-signed.** macOS blocks the first launch
  with "Apple could not verify JoyRead is free of malware"; it is allowed
  through System Settings → Privacy & Security → Open Anyway. Windows
  SmartScreen warns before running the installer. Unlike 1.0.0, the macOS disk
  image no longer carries a note explaining this — the instructions are on the
  release page instead, where someone meets them after hitting the message
  rather than before.
- macOS ships for Apple Silicon only; there is no Intel build.

## [1.0.0] — 2026-08-27

First public release.

### Reading

- Read ZIP/CBZ, 7z/CB7, RAR/CBR, and PDF.
- Single-page and two-page spreads, with automatic spread detection based on
  page shrink cost rather than raw area.
- Fit modes, reading direction including right-to-left, and page transitions.
- Bookmarks, table of contents, and a thumbnail navigation panel.
- Per-book resume, reading progress, and reader settings.

### Library

- Import into a managed library, or open files in place without importing.
- Collections, tags, favourites, and reading history.
- Cover thumbnails with a cover editor.
- Search, sort, and filter.
- Hidden Space for books kept off the main shelf.
- Storage location can be moved, re-pointed, or reset; transitions quiesce all
  background work first and abandon safely if the drain cannot be proven.

### Performance

- Archives that are expensive to read at random — solid 7z, RAR, and encrypted
  archives — are bulk-converted once into a shared extraction pool in the
  background. On a 124 MB solid 7z this moved page-turn p95 from 355 ms to
  58 ms and cut process-tree peak memory from 995 MB to 771 MB.
- The bundled 7-Zip executable replaces py7zr as the primary 7z read path,
  which alone moved page-prepare p95 on that sample from ~1410 ms to ~300 ms.
  py7zr remains the fallback where no 7-Zip backend resolves.
- PDF pages render through Qt's asynchronous `QPdfPageRenderer` instead of a
  synchronous `QPdfDocument.render()` call, which held the GIL for its full
  duration and stalled every Python thread including the GUI's event loop.
- PDF thumbnails for unread pages render directly at thumbnail size instead of
  rendering a full-size page and round-tripping it through a PNG encode and
  decode — roughly 9x faster per page.
- Page caches, thumbnail caches, and the extraction pool all carry configurable
  memory and disk budgets.

### Platform

- macOS 13+ on Apple Silicon.
- Opens books directly from Finder without loading the full library.
- Restores the most recently active window on Dock reopen.
- English, Japanese, and Simplified Chinese interfaces.

### Known limitations

- EPUB reading is present in the codebase but disabled; the novel reader is not
  finished.
- Pages extracted from encrypted archives are cached unencrypted. Settings →
  Privacy → "Delete cached pages when closing" is on by default so they do not
  outlive the session.
- Archive passwords are passed to the bundled 7-Zip executable on its command
  line and are therefore readable by same-user processes during extraction.
  This covers 7z, RAR, and ZIP using the legacy ZipCrypto cipher. AES-encrypted
  ZIP is unaffected, being decrypted in-process — which for AES is also the
  faster path. ZipCrypto is routed outside because decrypting it in Python runs
  at ~2.6 MB/s against ~99 MB/s through the helper.
- Solid-RAR performance is unverified; no solid RAR fixture was available. The
  7z thread policy is applied to RAR by extrapolation.
- Windows and Linux builds are not published. Their 7-Zip helpers are vendored
  and the code paths are complete; Windows still needs an application icon and
  neither platform has installer packaging yet.
- The macOS build is ad-hoc signed rather than notarised, so macOS reports it as
  damaged on first launch until the quarantine attribute is removed. The disk
  image explains this in English, Japanese, and Simplified Chinese.
