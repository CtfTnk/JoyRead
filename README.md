# JoyRead

A fast, local-first desktop reader and library manager for manga, comics, and
PDFs. No accounts, no cloud, no telemetry — your books stay on your machine.

JoyRead is built with Python and PySide6 (Qt), and is designed so that archive
extraction, image decoding, and thumbnail generation never block the UI thread.

## Features

**Reading**
- ZIP / CBZ, 7z / CB7, RAR / CBR, and PDF
- Single-page and two-page spread layouts, with automatic spread detection
- Configurable fit modes, reading direction (including right-to-left), and page
  transitions
- Bookmarks, table of contents, and a thumbnail navigation panel
- Resume where you left off, per book

**Library**
- Import books into a managed library, or open files in place without importing
- Collections, tags, favourites, and reading history
- Cover thumbnails with a cover editor
- Search, sort, and filter across your shelf
- Hidden Space for books you would rather not show on the shelf

**Performance**
- Archives that are expensive to read at random (solid 7z, RAR, encrypted zips)
  are converted once into a page cache in the background, turning multi-second
  page turns into sub-100 ms ones
- PDF rendering runs off the GIL through Qt's asynchronous page renderer, so
  decoding a large page never freezes the window
- Bounded memory: page caches, thumbnail caches, and the extraction pool all
  have configurable budgets

**Other**
- English, Japanese, and Simplified Chinese interfaces
- Opens books directly from Finder without loading the full library first

## Requirements

- macOS 13 or later, Apple Silicon
- Or, to run from source: Python 3.11+ on macOS, Windows, or Linux

Windows and Linux builds are not yet published — see
[Platform support](#platform-support).

## Installing

Download the latest `JoyRead.app` from the
[Releases](https://github.com/CtfTnk/JoyRead/releases) page, and drag it to your
Applications folder.

## Running from source

```bash
git clone https://github.com/CtfTnk/JoyRead.git
cd JoyRead
python -m pip install -e '.[dev]'
python -m joyread.app.main
```

To work on the (disabled) novel reader, install its extra as well —
`python -m pip install -e '.[dev,epub]'` — which adds `lxml` and enables the
`tests/novel/` suite.

To run the test suite:

```bash
python -m pytest -q
```

## Platform support

JoyRead's cross-platform code paths are complete, but a packaged build requires
a bundled 7-Zip helper for the target platform, and only `darwin-arm64` is
currently vendored. Adding Windows, Linux, or Intel Mac builds is a matter of
supplying the corresponding `7zz` binary — see [docs/PACKAGING.md](docs/PACKAGING.md).

## Known limitations

- **EPUB is not enabled.** The novel reader lives in `src/joyread/novel/` and
  is switched off until it is finished. It is a separable feature: the app is
  built and tested without it, and its `lxml` dependency is the optional
  `joyread[epub]` extra.
- **Cached pages from encrypted archives are stored unencrypted.** To keep
  encrypted archives fast, decrypted pages are written to the extraction pool in
  the clear. Settings → Privacy → "Delete cached pages when closing" is enabled
  by default so they do not outlive the session, but while a book is open those
  pages are readable by anything that can read your cache directory.
- **Archive passwords are visible to local processes.** The bundled 7-Zip
  executable only accepts a password as a command-line argument, so while an
  encrypted 7z or RAR is being extracted the password can be read via `ps` by
  another process running as you. Encrypted ZIP is unaffected — it is decrypted
  in-process.
- **RAR is read-only**, and by the terms of the bundled unRAR code, JoyRead's
  RAR support may not be used to build a RAR-compatible archiver.

## Documentation

- [docs/MANUAL.md](docs/MANUAL.md) — user manual
- [docs/PACKAGING.md](docs/PACKAGING.md) — building a release
- [docs/i18n.md](docs/i18n.md) — adding or updating a translation

## License

JoyRead is free software, licensed under the
[GNU General Public License v3.0](LICENSE).

It redistributes third-party components — including Qt/PySide6, 7-Zip, and
Pillow — under their own licenses. See
[packaging/THIRD_PARTY_NOTICES.txt](packaging/THIRD_PARTY_NOTICES.txt).
