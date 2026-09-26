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

Download the package for your system from the
[Releases](https://github.com/CtfTnk/JoyRead/releases) page: a DMG for macOS
Apple Silicon, an EXE installer for Windows x64, or a DEB for Ubuntu amd64/arm64.
On macOS, open the DMG and drag JoyRead into Applications. On Windows, run the
installer. On Ubuntu, install from the terminal only: open a terminal in the
download folder and run `sudo apt install ./package.deb`, replacing `package.deb`
with the downloaded filename. The graphical installer gets stuck at
**“Preparing”**; this issue has not been fixed.
See the release notes for signing and platform validation status. On first
launch JoyRead creates an empty library and shows the shelf.
The Library window appears first, then loads its books in the background.
Settings and **Actions → Open Book** remain available while it loads.
Opening a file through your system's Open With instead starts directly in its
Reader. It does not load the Library or import the file. You can open the
Library later without closing that Reader.

On Windows, **Enable background mode** is on by default under Settings →
General → **Faster Subsequent Windows**. Closing the last window first explains that
JoyRead will stay ready for faster later opens. Choose **Keep running** to close
the window while retaining the background process, or **Quit JoyRead** to exit
completely. Check **Don't show this again** to suppress future notices when
choosing Keep running; pressing Esc keeps the window open. The taskbar
notification icon can reopen JoyRead. Its
right-click menu opens the Library, picks a comic or PDF to read without
importing, shows JoyRead's current physical memory use in MB below the Clear
command, clears rebuildable memory caches, or quits JoyRead completely. Cache
cleanup waits until every window and background task is finished. Turning the
setting off restores normal exit on the last close. Turning it on again resets
the reminder, so the next final close explains background mode once more.
The same Windows-only group has **Automatically clear memory caches** and an
**Idle cleanup delay** in seconds. Automatic cleanup is on by default after
60 seconds with no windows; the delay is adjustable from 1 to 3600 seconds.
Turning automatic cleanup off leaves the tray's manual Clear command available.
The automatic-cleanup switch is unavailable while background mode is off;
the delay control becomes available when both switches are on. Dependent
settings in Reading Defaults and Archive & Cache also fade and become
unavailable while their controlling switch is off.
JoyRead does not start automatically when you sign in to Windows.

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

**Import a book into the library.** *Actions → Open & Import* opens the original
file immediately and imports a managed copy in the background. The Library
shows a progress dialog with **Cancel** for that import; cancelling or failing
to import does not close the Reader. *Actions → Import → Import Files…* /
*Import Folder…* add books without opening them. An imported book is copied
into JoyRead's managed library folder, so moving or deleting the original
afterwards does not affect it.

*Import Folder…* only descends one level by default. Raise **Import folder
depth** in Settings → General if your books are nested more deeply.

You can also drag supported files onto the Library window. Drop one file onto
**Read** to open it directly. Turn on **Import when dropped on Library Read**
in Settings → General if you also want that specific drop to import a copy.
Drop files or folders onto **Import** to add managed copies without reading.
The Read zone does not accept multiple files or a folder. If the Library is
unavailable, Read is labelled read-only and Import is disabled.

Your operating system's **Open With** menu can select JoyRead once its file
associations are registered. It only reads the file, regardless of the drag
preference. The *Actions → Open Book* command also remains read-only.

Encrypted comics can be read with a password, but cannot be imported. If an
Open & Import or Read-zone import encounters one, the import reports a failure
while the Reader stays open. Cancel an in-progress Open & Import from the
Library progress dialog; any already completed import remains in the Library.

### Metadata and book details

Imported comic archives containing supported `meta.json` or `ComicInfo.xml`
metadata can supply titles, authors, tags, and languages. JoyRead uses the
recognised fields, falling back to the filename or defaults when information is
missing or unreadable. This is embedded metadata import, not an online lookup.

In **Detail**, double-click the title or author to edit and press Enter to save;
Escape or moving focus away cancels. Double-click the language to select it,
and double-click the cover to open the cover editor. Cover edits are stored
separately from the source book. Use the toolbar's card/list toggle to choose
between cover browsing and a compact shelf.

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
on Add Time, Title, or Author, ascending or descending, or uses **Custom** order.
The **filter** button
narrows the shelf to one file type (CBZ, CBR, ZIP, RAR, 7Z, PDF), and the tag
button filters by tag. Your sort, filter, and search choices persist between
launches.

### Custom order and selection

All, Favourites, Hidden Space, and each collection remember their own sort
preference and custom order. Recent always uses reading time. Choosing Custom
for the first time starts with newest library additions first; returning to it
restores your order. The ascending/descending button is disabled in Custom.
New members go at the front. Removing and later re-adding a favourite or
collection member also puts it at the front. Search, filters, and privacy
visibility do not erase saved positions.

Click a book to select it alone. Shift-click adds a book or removes it from the
selection. Click blank shelf space or the toolbar's blank/title area to clear
selection; Shift-clicking blank
space keeps it. Drag from blank space to draw a selection rectangle. Shift-drag
starts a rectangle even on a book and adds intersecting books to the selection
you had when you started; it never starts reordering. You can use
the wheel while drawing; Esc restores the selection from before the gesture.

In Custom order, clear search, format, and tag filters to enable reordering.
Drag a selected card or list row to move the whole selection in its displayed
order. Drag an unselected book to move it alone. Release Shift before dragging
books to reorder them. One dashed placeholder shows where the group will land. The pointer
preview shows up to three cards and the total selection count.

Only the top and bottom 15% of the shelf auto-scroll during dragging, with speed
increasing toward the edge. The preview uses 40% card size and sits 5 px from
the pointer. The wheel also works. Release inside the book viewport to save the
order; release outside, press Esc, or right-click while holding the left button
to cancel. Leaving the viewport temporarily permits returning before release.
Switching shelves, sorting, or view mode, deactivating the window, or a background
membership/order change cancels the gesture. Saving briefly disables further
reordering; if it fails, JoyRead restores the last saved order and shows an error.

### Display scaling

JoyRead remembers the normal sizes of the Library and independent Reader
windows separately. Maximizing/fullscreen and automatic screen fitting do not
overwrite these preferences. **Settings → General → Window sizes → Restore
defaults** clears both preferences and resizes open windows to their defaults
(1200×860), adjusted to the current screen.

On a smaller screen, windows fit the available desktop area and align at its
top-left corner, below system menus and outside reserved taskbar/Dock space.
They retain their minimum sizes (Library 738×600; Reader 500×706). If the screen
is smaller than a minimum, the right/bottom may extend beyond it while the title
bar stays accessible. Window positions and maximized/fullscreen state are not
saved. Embedded reading shares the Library window.

The expanded shelf search narrows with the window while keeping format and tag
filters available. Settings rows place their controls below the label when the
horizontal layout no longer fits; widening restores the original arrangement.

Book covers and thumbnails in Detail and the Reader's Topic panel adapt to
display scaling automatically.
Moving JoyRead between screens refreshes their resolution without changing card
sizes or selection. Existing custom cover images keep their chosen crop; if an
older custom cover looks soft on a high-density screen, reopen the cover editor
from the original source and save it again on that screen. Missing source files
continue to use their available cached covers.

Hover over the Reader progress bar for 180 ms to preview the page at the pointer,
or drag its handle to preview immediately. The bubble uses 80% opacity and shows the thumbnail above
its 1-based page number and stays within the Reader. While waiting for an image,
it shrinks to a Loading label and page number. Topic and progress previews share
their thumbnail cache, so either can reuse images prepared by the other.

Release a drag inside the application window to jump once and close the preview.
Esc, a right-click while holding the left button, losing window activation, or
releasing outside the window cancels without jumping. Normal hover exit fades
the bubble over 320 ms; returning during the fade restores it immediately.
Solid or unknown archive previews only use existing caches: a distant page may
keep showing Loading until normal reading or background cache preparation makes
it available. Previewing itself does not start archive extraction or warmup.

Detail and Topic thumbnails load visible pages first and adjust preloading as
you scroll. Recently viewed thumbnails are reused when you scroll back, within
a fixed memory limit; long books do not load all their page images at once.
Detail, Topic and the cover picker show page numbers starting at 1 below each
thumbnail, even before its image loads. Click the image or its number to select
that page; gaps between items are not clickable.

Context menus open toward available space inside the current window. Near its
right or bottom edge they flip left or upward; button dropdowns can open above
their button. Long menus scroll, and clipped labels expose their full text in a
tooltip. Moving or resizing the window closes an open menu.

## Collections

Click the **Book Shelf** or **Collections** header to collapse or expand its
navigation entries. The arrow points right when collapsed and down when expanded;
focused headers also respond to Enter or Space.
Headers darken while pressed; dragging outside removes that feedback and
releasing outside cancels the click.
Both sections start expanded, and JoyRead remembers their state across restarts.
Collapsing a section keeps the
current shelf and selection; Collections also hides its New Collection entry.
Settings remains visible at the bottom. Hidden Space visibility still follows
its privacy setting, including after reopening a collapsed section.

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
| F | Enter full screen while reading |
| Esc | Leave full screen, close the open panel, or leave the reader |

The reader's chrome — the header and the page bar — fades out shortly after you
stop moving the pointer, so nothing sits on top of the page while you read. Move
the pointer or right-click to bring it back.
Returning from a Reader inside the main window also leaves full screen.
The Reader's maximize button exits full screen when pressed in that state.

### Layout

Open **Reader settings** (the gear in the reader chrome) to change how pages are
laid out. **Horizontal Mode** and **Vertical Mode** choices are remembered per
book. The separate **Preloading** section applies globally to all readers.

- **Reading direction** — Right-to-left (manga), Left-to-right, or Top-to-down.
- **Single Page** — off gives two-page spreads. JoyRead detects double-width
  pages and shows them alone, so covers and centrefolds are not split. If a
  spread pairs up wrong — one leading single page throws off every pair after
  it — **Shift spread pairing** re-pairs the whole book by one.
- **Fit Mode** — Auto, Fit to Height, Fit to Width, or Fit to Page.
- **Gap** — the space between the two pages of a spread.
- **Zoom** — magnify beyond the fit mode.
- **Page transition** — none, or slide.

**Preloading** defaults to **4 previous pages and 8 following pages**, with
ranges **0–10** and **0–20**. Previous/following means smaller/larger page
numbers, regardless of left-to-right or right-to-left display. In two-page mode
the counts start outside the displayed spread; vertical mode uses the current
page anchor while still loading pages needed for its visible layout. A value of
0 disables background preloading on that side, not pages needed for display.
Changes update all open readers and persist across restarts. Reducing the window
replaces pending demand but keeps useful cached pages and the open book. The
existing page-cache memory budget still applies; these counts are requested
preload windows, not a guarantee that every page is already cached.

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
| Import when dropped on Library Read | Off | Also import a single file dropped on the Library Read zone. Open Book and system Open With stay read-only. |
| Verify imported file integrity | On | Hash imported files so JoyRead can detect later corruption. |
| Individual Read Window | Off | Open each book in its own window. |
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
| Rebuildable archive page cache | `~/Library/Caches/JoyRead` |

Books you opened without importing are not here — they stay wherever you keep
them.

**Library Location** in Settings → Privacy → Storage moves your books, covers,
database, and backups somewhere else, an external drive included. The page
cache stays in the application's cache location and is rebuilt when needed.
JoyRead closes Readers opened from the Library first, saving your place in each,
then moves the files. Independently opened Readers stay open. **Select Existing Library** points
JoyRead at a library folder that already exists — useful if you moved it by hand
or want to switch between two.
Readers using managed Library files or its saved progress also close during
this operation, even if Open With previously activated their existing window.

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
extraction it can be read via `ps` by another process running as you. This
covers 7z, RAR, and ZIP files using the legacy ZipCrypto cipher.

ZIP files using AES encryption are unaffected — JoyRead decrypts those itself,
without launching anything, and for AES that is also the faster route. It is
only ZipCrypto that has to go outside: decrypting it inside JoyRead runs about
forty times slower, which is the difference between a page appearing at once
and taking a second.

Neither matters much on a machine only you use. Both matter on a shared one.

## When something goes wrong

**The Library does not load.** If its folder is unavailable, the Bookshelf
shows the saved path and reason with **Retry** and **Skip this time**. If the
database fails to load, the same two choices appear in a dialog. Skipping
keeps your saved location and lets you open external files. Use Settings →
Privacy → Storage → **Select Existing Library** to choose a different valid
library. JoyRead does not erase or reset an existing damaged library at launch.

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
Cache** checks the selected library — changed files, duplicates, missing files,
orphaned files, and unused generated covers — shows you what it found, and asks
before changing anything. The shared archive page cache has its own size limit
and can be cleared in Settings → Archive & Cache.

**EPUB does not open.** EPUB support is written but switched off until it is
finished. See the Known limitations section of the
[README](../README.md).

## Reporting a problem

Open an issue at
[github.com/CtfTnk/JoyRead/issues](https://github.com/CtfTnk/JoyRead/issues).
Your macOS version, the JoyRead version from Settings → About, and the format of
the book involved make a report much easier to act on.


## Application language (1.0.2)

In Settings → General → Language, choose Follow system, English, 中文, or 日本語.
New installations follow the system UI language preferences on Windows, macOS,
and Ubuntu/Linux. If none is supported, English is used. Chinese regional and
script variants currently share Simplified Chinese. Existing saved selections
are preserved during upgrade. Follow system detects at launch and when selected;
restart to pick up later operating-system language changes.

Language changes apply to open application windows and their controls. The
in-app name is 欣阅 in Chinese, JoyRead in English, and ジョイヨミ in Japanese.
Installed shortcuts, application filenames and library/configuration directories
keep their existing names. System-native file dialog buttons may use OS language.

Custom JSON translations in the stable `Config/locales` directory override
individual keys. Omitted/empty entries keep the bundled language; missing bundled
translations fall back to English. Broken override files are ignored and logged.
User-entered titles, tags, hints, paths and technical diagnostic details remain
unchanged.
