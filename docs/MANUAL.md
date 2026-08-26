# JoyRead User Manual

JoyRead is a local-first reader and library manager for manga, comics, and PDFs.
Everything it knows about your books lives on your own machine — there is no
account to create and nothing is sent anywhere.

- [Getting started](#getting-started)
- [Adding books](#adding-books)
- [The shelf](#the-shelf)
- [Collections](#collections)
- [Tags](#tags)
- [Reading](#reading)
- [Bookmarks](#bookmarks)
- [Covers](#covers)
- [Hidden Space](#hidden-space)
- [Settings](#settings)
- [Where your files live](#where-your-files-live)
- [Encrypted archives](#encrypted-archives)
- [When something goes wrong](#when-something-goes-wrong)

## Getting started

Download `JoyRead.app` from the
[Releases](https://github.com/CtfTnk/JoyRead/releases) page and drag it to your
Applications folder. On first launch JoyRead creates an empty library and shows
the shelf.

The window is split into a sidebar on the left and the shelf on the right. The
sidebar holds **Book Shelf** (with **All**, **Recent**, **Favourites**, and
**Hidden**), your **Collections**, and **Settings**. The toolbar button at the
top-left toggles the sidebar away when you want more room.

## Adding books

JoyRead reads ZIP/CBZ, 7z/CB7, RAR/CBR, and PDF. There are two ways in, and the
difference matters:

**Open a book without importing it.** *Actions → Open Book* opens a file from
wherever it already is. JoyRead does not copy it, and does not add it to your
shelf. Use this for something you want to read once.

**Import a book into the library.** *Actions → Open & Import* opens a file and
adds it, and *Actions → Import → Import Files…* / *Import Folder…* add books
without opening them. An imported book is copied into JoyRead's managed library
folder, so moving or deleting the original afterwards does not affect it.

*Import Folder…* only descends one level by default. Raise **Import folder
depth** in Settings → General if your books are nested more deeply.

If you would rather every book you open be imported automatically, turn on
**Import book when opening** in Settings → General.

### Conversion on import

Some archives are slow to read a page at a time — solid 7z, RAR, and encrypted
ZIP all have to do substantial work to reach an arbitrary page. JoyRead converts
those into a page cache once, in the background, which turns multi-second page
turns into sub-100 ms ones.

**Convert archives on import** in Settings → General controls when this happens:

| Setting | Behaviour |
| --- | --- |
| Never | Never convert. Expensive archives stay slow. |
| Expensive and nested formats *(default)* | Convert only the formats that need it. |
| Always | Convert everything on import, including plain ZIP/CBZ. |

Conversion is not required — a book that has not been converted still opens and
reads correctly, just more slowly on the formats above.

## The shelf

Each book appears as a cover tile. Click a tile to read it; use the **More**
menu on a tile for everything else:

- **Read** — open the book
- **Favourite** / **Unfavourite** — show it under Favourites
- **Detail** — title, author, language, book type, tags, and the cover editor
- **Add to…** — put it in a collection
- **Export** — copy the managed file back out to a folder you choose
- **Remove** — take it off the shelf, leaving the file alone
- **Hide** — move it into [Hidden Space](#hidden-space)
- **Delete** — remove the library record *and* the app-managed copy

Delete is permanent and asks first. Remove is not — it only drops JoyRead's
record of the book.

**Search** sits in the toolbar and matches titles and authors. **Sort by** sorts
on Add Time, Title, or Author, ascending or descending. The **filter** button
narrows the shelf to one file type (CBZ, CBR, ZIP, RAR, 7Z, PDF), and the tag
button filters by tag. Your sort, filter, and search choices persist between
launches.

## Collections

Collections are manual groupings — a series, a run, a to-read pile. Create one
with **New Collection** in the sidebar, then add books through a book's
**Add to…** menu. A book can be in as many collections as you like.

Deleting a collection removes only the grouping. The books stay in your library.

Collections can be hidden along with their books; see
[Hidden Space](#hidden-space).

## Tags

Tags are the flexible half of organising. Open a book's **Detail** view to
attach them: the tray at the top shows what is already on the book, and the list
below shows every tag you have. Type in the search box and press Enter to create
a tag that does not exist yet.

Tag the same way across books and the toolbar's tag filter becomes a real index.
Settings → Tags manages the whole vocabulary — rename a tag everywhere at once,
or delete one. Deleting a tag removes it from every book that carries it; the
books themselves are untouched.

JoyRead caps a library at 5,000 tags. That is far past what a personal library
needs, and the cap exists because tag dialogs stop feeling instant beyond it.

## Reading

Click a book to open it. By default books open in the main window; turn on
**Individual Read Window** in Settings → General to give each book its own
window instead, which is what you want when comparing two books side by side.

### Turning pages

| Input | Action |
| --- | --- |
| ← / → | Previous / next page, following your reading direction |
| Scroll wheel or trackpad | Scroll, in vertical mode |
| Right-click | Show the controls |
| Esc | Close the open panel, or leave the reader |

The reader's chrome — the header and the page bar — fades out shortly after you
stop moving the pointer, so nothing sits on top of the page while you read. Move
the pointer or right-click to bring it back.

### Layout

Open **Reader settings** (the gear in the reader chrome) to change how pages are
laid out. The panel is split into **Horizontal Mode** and **Vertical Mode**, and
your choices are remembered per book.

- **Reading direction** — Right-to-left (manga), Left-to-right, or Top-to-down.
- **Single Page** — off gives two-page spreads. JoyRead detects double-width
  pages and shows them alone, so covers and centrefolds are not split. If a
  spread pairs up wrong — one leading single page throws off every pair after
  it — **Shift spread pairing** re-pairs the whole book by one.
- **Fit Mode** — Auto, Fit to Height, Fit to Width, or Fit to Page.
- **Gap** — the space between the two pages of a spread.
- **Zoom** — magnify beyond the fit mode.
- **Page transition** — none, or slide.

### Getting around a long book

Three panels open from the reader chrome:

- **Contents** — the book's table of contents, when it has one. PDFs usually do;
  most CBZ files do not.
- **Thumbnails** — every page as a small image. The fastest way to find a
  remembered page.
- **Bookmarks** — your own marks; see below.

JoyRead remembers where you stopped in each book and reopens there.

## Bookmarks

Add a bookmark from the reader chrome and it is saved against the current page,
named "new bookmark" until you rename it. Bookmarks live in the **Bookmarks**
panel, where you can rename or delete them. They are stored in your library, not
in the book file, so they survive re-importing and never modify your archives.

## Covers

JoyRead uses the first page as the cover. To change it, open a book's **Detail**
view and click **Edit cover**:

- **Choose from book pages** — pick any page in the book.
- **Import img** — use an image file from disk.

Either way you can crop and position before confirming. The original file is
never modified — the cover is stored alongside the library record.

## Hidden Space

Hidden Space keeps books off the shelf. Hide a book from its **More** menu, and
it moves out of All, Recent, Favourites, and search results into **Hidden** in
the sidebar. Collections can be hidden too — right-click a collection and choose
**Make hidable**, which hides the collection and everything in it. **Make
normal** puts it back.

The first time you open Hidden it asks you to set a password, and asks for it
again each session. Everything else lives in Settings → Privacy → Hidden Space:
**Show Collections** (whether hidable collections appear in the sidebar),
**Change Password**, and **Revert all**, which un-hides everything at once.

**What Hidden Space is:** a way to keep books off a shelf that someone else
might glance at.

**What it is not:** encryption. The files are stored exactly like any other book
in your library, and anyone with access to your account and a file browser can
read them. The password gates JoyRead's UI, nothing more. If you need real
secrecy, use an encrypted disk image or full-disk encryption.

**Reset and Erase**, also under Settings → Privacy, permanently deletes every
hidden book from disk, deletes every hidable collection, and clears the
password. It cannot be undone, and it asks you to type `delete` first.

## Settings

### General

| Setting | Default | What it does |
| --- | --- | --- |
| Language | English | English, 日本語, or 简体中文. |
| Import book when opening | Off | Import every book you open, instead of opening it in place. |
| Verify imported file integrity | On | Hash imported files so JoyRead can detect later corruption. |
| Individual Read Window | Off | Open each book in its own window. |
| Inspect Windows/Linux Title Control | Off | Draw the Windows/Linux window buttons on macOS, for checking that layout without a second machine. |
| Import folder depth | 1 | How deep *Import Folder…* descends. |
| Convert archives on import | Expensive and nested formats | See [Conversion on import](#conversion-on-import). |

This section also holds **Verify Library & Clean Cache**, described under
[When something goes wrong](#when-something-goes-wrong).

### Archive & Cache

Resource limits. The defaults suit a normal library; raise them if you have
unusually large books, lower them if you want JoyRead to use less of the machine.

| Setting | Default | What it does |
| --- | --- | --- |
| Limit archive size / Maximum archive size | On, 5 GB | Refuse to open archives larger than this. |
| Resource guardrails | On | Master switch for the four limits below. |
| Maximum extracted item | 1 GB | Largest single page JoyRead will extract. |
| Maximum extracted data per operation | 4 GB | Ceiling on one extraction, however many pages. |
| Maximum image size | 400 MP | Refuse images above this many megapixels. |
| External extraction timeout | 300 s | How long the bundled 7-Zip may run before being stopped. |
| Nested archive depth | 2 | How many archives-inside-archives to follow. |
| Archive global file depth | 100 | Ceiling on total nesting across a whole book. |
| Reader page cache (in-memory) | 512 MB | Decoded pages held in RAM. |
| Thumbnail cache (in-memory) | 64 MB | Thumbnails held in RAM. |
| Archive extraction pool (disk) | 5 GB | Disk budget for converted page caches. |
| Archive cache strategy | Zip bundle | How a converted book is stored: one zip per book, or loose hidden image files. |

The guardrails exist because an archive's own description of itself is a claim,
not a fact: a file can declare a 2 KB page that expands to gigabytes. These
limits mean a malformed or hostile archive fails with a message instead of
exhausting your memory or disk.

**Archive pool usage** shows how much of the disk budget is in use, with a
**Clear** button that empties it. When the pool is full, JoyRead evicts the
least recently used converted book, so clearing it by hand is rarely necessary —
converted books are rebuilt on demand.

### Tags

Rename and delete tags across the whole library. See [Tags](#tags).

### Privacy

Three groups:

**Hidden Space** — Show Collections, Change Password, Revert all, and Reset and
Erase. See [Hidden Space](#hidden-space).

**Storage** — **Library Location** moves your library somewhere else,
**Select Existing Library** points JoyRead at one that already exists, and
**Reset Library** permanently deletes every book, cover, and reading position in
the current library. See [Where your files live](#where-your-files-live).

**Encrypted Archives** — **Delete cached pages when closing** (on by default)
erases the extracted pages of encrypted archives when the book closes. See
[Encrypted archives](#encrypted-archives).

### About

Version and license information.

## Where your files live

On macOS:

| What | Where |
| --- | --- |
| Library (books, covers, database) | `~/Library/Application Support/JoyRead-Library` |
| Settings and logs | `~/Library/Application Support/JoyRead` |

Books you opened without importing are not here — they stay wherever you keep
them.

**Library Location** in Settings → Privacy → Storage moves the whole library
somewhere else, an external drive included. JoyRead closes any open readers first, saving
your place in each, then moves the files. **Select Existing Library** points
JoyRead at a library folder that already exists — useful if you moved it by hand
or want to switch between two.

## Encrypted archives

JoyRead can read password-protected 7z, RAR, and ZIP. You are asked for the
password when the book opens, and it is used for that session only — JoyRead
never stores it.

Two limitations are worth knowing about, because neither is fixable in JoyRead
alone:

**Cached pages are stored unencrypted.** Making an encrypted archive fast means
extracting its pages to disk, and they land there as plaintext. Settings →
Privacy → **Delete cached pages when closing** is on by default so they do not
outlive the session, but while the book is open those pages are readable by
anything that can read your cache directory.

**Passwords are briefly visible to other local processes.** The bundled 7-Zip
executable accepts a password only as a command-line argument, so during
extraction it can be read via `ps` by another process running as you. Encrypted
ZIP is unaffected — JoyRead decrypts it in-process.

Neither matters much on a machine only you use. Both matter on a shared one.

## When something goes wrong

**"This archive exceeds the current resource limits."** The book is larger than
one of the Archive & Cache limits. Raise the specific limit named, or turn off
**Resource guardrails** if you trust the file.

**A book shows as missing.** JoyRead could not find the file. For an imported
book this usually means the library folder moved; check
[Library Location](#where-your-files-live). You will be offered the choice to
delete the record or keep it.

**A book shows as unavailable.** An integrity check found the file changed since
import. JoyRead will not open it, because a file that changed unexpectedly may
be corrupt. Remove it from its More menu and re-import.

**Page turns are slow.** The book is probably an unconverted solid 7z, RAR, or
encrypted ZIP. Set **Convert archives on import** to *Always* and re-import it,
and check that the extraction pool has room.

**Something is inconsistent.** Settings → General → **Verify Library & Clean
Cache** checks the whole managed library — changed files, duplicates, missing
files, orphaned files, reclaimable cache — shows you what it found, and asks
before changing anything.

**EPUB does not open.** EPUB support is written but switched off until it is
finished. See the Known limitations section of the
[README](../README.md).

## Reporting a problem

Open an issue at
[github.com/CtfTnk/JoyRead/issues](https://github.com/CtfTnk/JoyRead/issues).
Your macOS version, the JoyRead version from Settings → About, and the format of
the book involved make a report much easier to act on.
