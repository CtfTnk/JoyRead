# JoyRead User Manual

[English](MANUAL.md) · [简体中文](MANUAL.zh-CN.md) · [日本語](MANUAL.ja.md)

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
- [Reporting a problem](#reporting-a-problem)
- [Application language](#application-language)

## Getting started

Download the package for your system from the
[Releases](https://github.com/CtfTnk/JoyRead/releases) page: a DMG for macOS
Apple Silicon, an EXE installer for Windows x64, or a DEB for Ubuntu amd64/arm64.
On macOS, open the DMG and drag JoyRead into Applications. On Windows, run the
installer. On Ubuntu, install from the terminal only: open a terminal in the
download folder and run `sudo apt install ./package.deb`, replacing `package.deb`
with the downloaded filename. The graphical installer gets stuck at
**“Preparing”**; this issue has not been fixed.
See the release notes for signing and first-launch instructions. On first
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

When you open an archive just to read it (Open Book, Open With, or a Read drop
that doesn't import) and fewer than half of its files are pages JoyRead can
show, JoyRead asks before showing anything. This catches archives of phone
photos (HEIC), PSD sources, videos or documents. The question says how many
files are pages and what the rest are. Choose **Read Anyway** to read the pages
JoyRead can show, or **Cancel** to close the Reader. Small archives with fewer
than three other files are not asked about, and `ComicInfo.xml`, text files and
checksums don't count. After Read Anyway, JoyRead doesn't ask about the same
file again until you quit. Library books and Open & Import are never checked.

Password-protected comics can be neither read nor imported; see
[Encrypted archives](#encrypted-archives). Cancel an in-progress Open & Import
from the Library progress dialog; any already completed import remains in the
Library.

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

Some archives, such as solid 7z and nested archives, are expensive to read one
page at a time. During import, JoyRead can convert eligible unencrypted archives
into a CBZ copy in the managed library. The original source remains unchanged.
This import conversion is separate from the rebuildable page cache used while
reading. Password-protected archives can be neither read nor imported.

**Convert archives on import** in Settings → General controls when this happens:

| Setting | Behaviour |
| --- | --- |
| Never | Keep the imported archive in its original format. |
| Expensive and nested formats *(default)* | Convert only the formats that need it. |
| Always | Convert everything on import, including plain ZIP/CBZ. |

Conversion is not required to read a supported archive. Opening an unconverted
book can still use the normal reading cache; performance depends on the source
format and available cached pages.

## The shelf

Each book appears as a cover tile. Click a tile to read it; use the **More**
menu on a tile for everything else:

- **Read** — open the book
- **Favourite** / **Unfavourite** — show it under Favourites
- **Detail** — title, author, language, book type, tags, and the cover editor
- **Add to…** — put it in a collection
- **Export** — copy the managed file back out to a folder you choose
- **Remove** — take it off the shelf, leaving the file alone
- **Hide** / **Unhide** — move it into or out of [Hidden Space](#hidden-space),
  which encrypts hidden books; shown while Hidden Space is unlocked
- **Delete** — remove the library record *and* the app-managed copy

Delete is permanent and asks first. Remove is not — it only drops JoyRead's
record of the book.

**Search** sits in the toolbar and matches titles and authors. **Sort by** sorts
on Add Time, Title, or Author, ascending or descending, or uses **Custom** order.
The **filter** button
narrows the shelf to one file type (CBZ, CBR, ZIP, RAR, 7Z, PDF), and the tag
button filters by language and tag. Click a chip to select it, or Shift-click
to select several. Several languages show books in any of them, while several
tags require every one of them, and language and tags apply together. The
confirm button shows how many books on the current shelf match. Change a
book's language in **Details**. Your sort, file-type, and search choices persist
between launches; the language and tag filter lasts until you quit.

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

In Custom order, clear search, format, language, and tag filters to enable reordering.
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
below shows existing Library tags. The search box only filters existing tags
when you press Enter; it does not create tags.

To create a tag, open **Settings → Tags**, clear any search, and click **+** at
the end of the tag list. Enter the name in the separate tag-name field, then
click **Confirm** or press Enter in that field. Return to the book's tag panel
to assign the new tag.

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
| ↑ / ↓ | Move up / down within an oversized page; scroll continuously in vertical mode |
| Scroll wheel or trackpad | Scroll within an oversized page, or continuously in vertical mode |
| Right-click | Show the controls |
| F | Enter full screen while reading |
| Esc | Leave full screen, close the open panel, or leave the reader |

The reader's chrome — the header and the page bar — fades out shortly after you
stop moving the pointer, so nothing sits on top of the page while you read. Move
the pointer or right-click to bring it back.
Returning from a Reader inside the main window also leaves full screen.
The Reader's maximize button exits full screen when pressed in that state.
Full screen uses square outer corners for the page and reader bars; leaving
full screen restores the window's rounded corners.

### Layout

Open **Reader settings** (the gear in the reader chrome) to change how pages are
laid out. **Horizontal Mode** and **Vertical Mode** choices are remembered per
book. The separate **Preloading** section applies globally to all readers.

- **Reading direction** — Right-to-left (manga), Left-to-right, or Top-to-down.
- **Enable Custom** — horizontal and vertical modes have separate switches;
  their dependent controls apply only while the corresponding switch is on.
- **Single Page** — off allows automatic two-page spreads where they fit. JoyRead detects double-width
  pages and shows them alone, so covers and centrefolds are not split. If a
  spread pairs up wrong — one leading single page throws off every pair after
  it — **Shift spread pairing** re-pairs the whole book by one.
- **Fit Mode** — Auto, Fit to Height, Fit to Width, or Fit to Page.
- **Gap** — the space between stacked pages in vertical mode, from 0 to 200 px.
- **Zoom** — vertical-mode scale, from 25% to 200%; unavailable while vertical
  Fit to Width is enabled.
- **Page transition** — none, or slide.

In horizontal reading, **Fit to Width** starts a tall page at its top. Use the
wheel, trackpad, or ↑ / ↓ to see the rest; holding an arrow key repeats the
movement. Scrolling stops at the page edges. Use ← / → to turn pages as usual;
each new page starts at the top. Resizing the window or entering/leaving full
screen preserves the current position where the page bounds allow it. Pages
that fit vertically remain centered. Up/down reading shortcuts are inactive
while a reader panel or dialog is open.

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

For Library books, JoyRead remembers where you stopped and reopens there.
Files opened without importing do not get Library progress, bookmarks, or
saved per-book settings.

## Bookmarks

For a Library book, add a bookmark from the reader chrome and it is saved against the current page,
named "new bookmark" until you rename it. Bookmarks live in the **Bookmarks**
panel, where you can rename or delete them. They are stored in your library, not
in the book file, and never modify your archives. Include the Library database
in your backups to preserve them.

## Covers

JoyRead uses the first page as the cover. To change it, open a book's **Detail**
view and click **Edit cover**:

- **Choose from book pages** — pick any page in the book.
- **Import img** — use an image file from disk.

Either way you can crop and position before confirming. The original file is
never modified — the cover is stored alongside the library record.

## Hidden Space

Hidden Space keeps books off the shelf and encrypts them. A hidden book leaves
All, Recent, Favourites, normal collections, and search results, and appears
only under **Hidden** in the sidebar. Its file and its cover are stored
encrypted, so they can be read only with your passphrase or your recovery
code.

### Setting it up

Turn on **Show Collections** in Settings → Privacy → Hidden Space. The first
time, JoyRead sets up Hidden Space:

1. **Choose a passphrase** and type it again. It needs at least 4 characters,
   and any characters work. Under 12 characters, JoyRead warns you: a short
   passphrase keeps books out of casual view, but someone who copies your
   library could guess it. Choose **Use It** or **Change It**.
2. **Save the recovery code.** **Copy** it or use **Save…** to write it to a
   file. Keep it somewhere safe, away from your library.
3. **Type the code back** to confirm you saved it. Spaces and letter case do
   not matter.

Cancelling at any step sets nothing up.

The recovery code is the only way back in if you forget your passphrase. There
is no passphrase hint. **If you lose both, nobody can recover your hidden
books**, not even JoyRead's developers. Each library has its own Hidden Space,
with its own passphrase and recovery code.

### Hiding books

**Hide** and **Unhide** appear in a book's **More** menu only while Hidden
Space is shown.

- **Hide:** the book moves into **Hidden** at once, and JoyRead encrypts it in
  the background. Its card says **Encrypting…** until that finishes, and you
  can keep using JoyRead meanwhile.
- **Unhide:** the book is decrypted first. It shows **Decrypting…** and stays
  in Hidden until that finishes, then returns to your shelf.

A Reader showing the book closes first. Hiding also takes the book out of
Favourites, Recent, and normal collections for good; unhiding does not put it
back. The title, tags, reading position, and bookmarks stay.

Hiding changes how JoyRead stores the book, not its pages: an archive is kept
as a CBZ inside the encrypted file and comes back as a CBZ when you unhide it.
A PDF comes back as the same PDF. **Export** works on a hidden book while
Hidden Space is unlocked, and writes a normal, unencrypted CBZ or PDF where you
choose.

**Collections can be hidden too.** Right-click a collection and choose **Make
hidable**. A hidable collection appears only while Hidden Space is shown, and it
can hold hidden books. **Make normal** turns it back into an ordinary
collection.

### Locking and unlocking

Hidden Space locks when you:

- turn off **Show Collections**
- close the Library window
- quit JoyRead
- move the library, select another one, or reset it

Locking closes every Reader showing a hidden book, and JoyRead forgets the
decrypted covers. Hidden Space does not lock by itself after a while or when
your computer sleeps.

If you closed the Library window with Show Collections still on, the next
Library window asks for your passphrase:

- **Unlock** shows Hidden Space.
- **Hide** turns Show Collections off and leaves Hidden Space locked.

Turning Show Collections back on also asks for the passphrase.

**Forgot your passphrase?** Choose **Use recovery code** on that screen, or
**Recovery Code** in the passphrase dialog. Enter the code, then choose a new
passphrase. The recovery code keeps working.

### Hidden Space settings

Settings → Privacy → Hidden Space has:

- **Show Collections:** show or hide Hidden Space. Turning it off also locks
  it.
- **Encryption:** whether every hidden book is encrypted, how many are still
  being encrypted or decrypted, and how many failed. **Retry** tries the failed
  ones again. JoyRead also retries them the next time Hidden Space unlocks and
  the next time JoyRead starts.
- **Change Passphrase:** asks for the current passphrase. Your recovery code
  stays the same.
- **New Recovery Code:** asks for your passphrase. The old code keeps working
  until you confirm the new one.
- **Revert all:** asks for your passphrase, then decrypts and unhides every
  hidden book and makes hidable collections normal again. Each book returns to
  the shelf as it finishes.
- **Reset and Erase:** permanently deletes every hidden book and every hidable
  collection, and removes the passphrase and the recovery code. It cannot be
  undone. It does not ask for the passphrase, so it is how you start over if
  you lost both.

### Upgrading from an earlier version

Earlier versions of JoyRead hid books without encrypting them, behind a Hidden
Space password. Those books stay hidden, but unencrypted, until you upgrade.
The next time you show Hidden Space, JoyRead asks for that password:

1. Your password becomes your passphrase.
2. You save and confirm a recovery code.
3. JoyRead encrypts the books you had hidden.

You can choose a longer passphrase afterwards with **Change Passphrase**.
Cancelling changes nothing.

### What Hidden Space protects, and what it does not

**Protected:**

- hidden book files
- their covers
- pages JoyRead caches while you read them

JoyRead never writes a decrypted page or cover to disk.

**Not protected:**

- **The library database.** Anyone who can read your files can still see each
  hidden book's:
  - title and author
  - tags and collection names
  - original file name and location
  - reading progress and bookmarks

  Importing the same file again is still reported as a duplicate.
- **Copies from before you hid a book.** Hiding deletes JoyRead's unencrypted
  copy, but earlier copies can survive:
  - your original file outside the library
  - backups such as Time Machine or File History
  - file-system snapshots
  - deleted blocks on the disk

  Hiding some RAR and 7z books also unpacks their pages briefly into a
  temporary folder while JoyRead converts them.
- **JoyRead's logs.** The log records the file paths and names of books you
  import, open and export, so entries written before you hid a book still name
  it. After hiding, JoyRead refers to the book by an internal ID. To remove the
  older entries, use **Delete Logs** in
  [Settings → About → Diagnostics](#about).
- **Memory while unlocked.** Decrypted pages and the key are in memory while
  Hidden Space is unlocked. Your operating system may write memory to disk,
  for example when it swaps or hibernates.
- **Short passphrases.** Someone who copies your library can try passphrases
  offline, as fast as their computer allows. A short passphrase can fall within
  days, and a 4-digit PIN within minutes. Use 12 characters or more if that
  matters to you. The recovery code is long enough that it is never the weak
  point.
- **Password-protected archives.** JoyRead cannot encrypt these. They stay
  hidden, marked **Not encrypted**.

If you need everything protected, including the library database, keep your
library on an encrypted disk or use full-disk encryption as well.

## Settings

### General

| Setting | Default | What it does |
| --- | --- | --- |
| Language | Follow system | Follow the system language, or choose English, 日本語, or 简体中文. |
| Import when dropped on Library Read | Off | Also import a single file dropped on the Library Read zone. Open Book and system Open With stay read-only. |
| Verify imported file integrity | On | Hash imported files so JoyRead can detect later corruption. |
| Individual Read Window | Off | Open each book in its own window. |
| Import folder depth | 1 | How deep *Import Folder…* descends. |
| Convert archives on import | Expensive and nested formats | See [Conversion on import](#conversion-on-import). |

This section also holds **Verify Library & Clean Cache**, described under
[When something goes wrong](#when-something-goes-wrong).
Windows background mode and automatic cleanup are described under
[Getting started](#getting-started).

### Reading Defaults

Choose the starting reading direction, horizontal/vertical customization,
single-page preference, fit mode, vertical spacing and zoom, and page
transition. These defaults apply to temporary files and Library books without
saved individual settings. Existing per-book settings take precedence.
Changing defaults affects future opens; it does not overwrite settings in
Readers that are already open. Edit the current book through Reader settings.
Global preloading remains separate; see [Layout](#layout).

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

Create, rename, and delete tags across the whole library. See [Tags](#tags).

### Privacy

Three groups:

**Hidden Space** — Show Collections, Encryption, Change Passphrase, New Recovery
Code, Revert all, and Reset and Erase. See [Hidden Space settings](#hidden-space-settings).

**Storage** — **Library Location** moves your library somewhere else,
**Select Existing Library** points JoyRead at one that already exists, and
**Reset Library** permanently deletes every book, cover, and reading position in
the current library. See [Where your files live](#where-your-files-live).

### About

The About page introduces JoyRead, shows the installed version and credits
CtfTnk. Select the project GitHub link there to open its repository in your
browser.

**Diagnostics** helps when something goes wrong:

- **Log Folder → Open** shows the folder that holds JoyRead's log files.
- **Export Logs** saves every log file into one zip that you can attach to a
  report.
- **Delete Logs** deletes the log files. JoyRead starts a new log straight away.
- **Detailed Logging Until JoyRead Quits** records much more detail while you
  reproduce a problem. It is off again the next time JoyRead starts.

The logs record what JoyRead did, including the real file paths and names of
the books you import, open and export — also books you hid later. They stay on
your computer unless you send them to someone, so read them or think about
those names before you share them. Passwords are never written to the logs.

## Where your files live

These are the installed application's default locations. If you have moved or
selected a Library, its actual location is shown in **Settings → Privacy →
Storage → Library Location**.

### macOS

| What | Where |
| --- | --- |
| Library (books, covers, database, backups) | `~/Library/Application Support/JoyRead-Library` |
| Settings file | `~/Library/Application Support/JoyRead/Config/settings.json` |
| Logs | `~/Library/Application Support/JoyRead/Logs` |
| Rebuildable archive page cache | `~/Library/Caches/JoyRead` |

### Windows

| What | Where |
| --- | --- |
| Library (books, covers, database, backups) | `%APPDATA%\JoyRead\JoyRead-Library` |
| Settings file | `%APPDATA%\JoyRead\JoyRead\Config\settings.json` |
| Logs | `%APPDATA%\JoyRead\JoyRead\Logs` |
| Rebuildable archive page cache | `%LOCALAPPDATA%\JoyRead\JoyRead\Cache` |

Paste these paths into File Explorer's address bar. `%APPDATA%` normally points
to `C:\Users\<username>\AppData\Roaming`, and `%LOCALAPPDATA%` to
`C:\Users\<username>\AppData\Local`. The repeated `JoyRead\JoyRead` in the
support/cache paths is intentional.

### Ubuntu / Linux

| What | Where |
| --- | --- |
| Library (books, covers, database, backups) | `~/.local/share/JoyRead-Library` |
| Settings file | `~/.config/JoyRead/Config/settings.json` |
| Logs | `~/.config/JoyRead/Logs` |
| Rebuildable archive page cache | `~/.cache/JoyRead` |

`~` means your home directory. If set, `XDG_DATA_HOME` replaces
`~/.local/share`, `XDG_CONFIG_HOME` replaces `~/.config`, and `XDG_CACHE_HOME`
replaces `~/.cache`. Logs are under the configuration root shown above.

### Library contents and relocation

The Library contains `Books/` for managed books, `Database/joyread.sqlite3`
for records, `Thumbnails/` for covers and thumbnails, and `Backups/` for backups.
Hidden books and their covers are stored encrypted as `.jrl` files in `Books/`
and `Thumbnails/covers/`. They open only from the Library, and moving the
Library takes Hidden Space with it.
The logs directory contains `joyread.log` and rotated log files; they record
real file paths and book names (see [About](#about)). Custom
translations live in `Config/locales/` beside `settings.json`.

Source-checkout runs and profiles using `JOYREAD_RUNTIME_DIR` use
`JoyRead-Library/`, `.joyread_support/` (configuration/logs), and
`.joyread_cache/` beneath the checkout or selected runtime directory instead.

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

JoyRead does not open or import password-protected ZIP, 7z, or RAR archives.
Opening one says that password-protected archives aren't supported, and JoyRead
never asks for the password. An ordinary archive that contains a
password-protected one still opens; only the pages inside the protected archive
are left out.

To read such a book, extract it with another tool, such as 7-Zip, then open or
import the extracted files. To keep it private, import it and hide it:
[Hidden Space](#hidden-space) encrypts the book inside your library.

JoyRead 1.3.1 and earlier asked for the password, but their page cache kept the
decrypted pages unencrypted on disk, and some formats passed the password to the
7-Zip helper on its command line, where other local processes could see it.
Reading password-protected archives is switched off until that can be done
without either.

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

**Page turns are slow.** The book may be a solid 7z, RAR, or another archive
without prepared conversion/cache data. Set **Convert archives on import** to
*Always* and re-import; also check that the extraction pool has room.

**A hidden book says Not encrypted or Not decrypted.** JoyRead could not
finish encrypting or decrypting it, for example because the disk was full or
another app held the file open. The book stays where it was. Fix the cause and
choose **Retry** in Settings → Privacy → Hidden Space; JoyRead also retries at
the next unlock and the next start. Password-protected archives cannot be
encrypted at all.

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
Your operating system and version, the JoyRead version from Settings → About, and the format of
the book involved make a report much easier to act on. For a problem that is
hard to describe, turn on **Detailed Logging Until JoyRead Quits**, make the
problem happen again, and attach the zip from **Export Logs** (Settings → About
→ Diagnostics). Check what the logs contain first: see [About](#about).


## Application language

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
User-entered titles, tags, paths and technical diagnostic details remain
unchanged.
