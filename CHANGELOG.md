# Changelog

All notable changes to JoyRead are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — unreleased

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
  Encrypted ZIP is unaffected, being decrypted in-process.
- Solid-RAR performance is unverified; no solid RAR fixture was available. The
  7z thread policy is applied to RAR by extrapolation.
- Windows and Linux builds are not published, pending their 7-Zip helper
  binaries.
