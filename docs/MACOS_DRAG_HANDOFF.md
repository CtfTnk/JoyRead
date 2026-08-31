# macOS maximized-drag handoff

**Temporary.** Delete this file once the macOS behaviour is fixed.

Dragging a *maximized* window by its custom title bar is fixed on Linux and
still wrong on macOS. The remaining fix depends on one fact about AppKit that
can only be measured on a Mac. This note says how to measure it.

`docs/technical/` and `docs/reports/` are gitignored, so nothing in them reaches
the Mac. Everything needed is repeated here.

---

## Part 1 — What to run

Set the environment up as usual (activate **by path**; a bare name will not work):

```bash
conda env create --prefix .conda/joyread-py312 -f environment-release.yml   # first time only
conda activate ./.conda/joyread-py312
```

Then run the probe three times, one per mode. It opens a frameless window and
logs Qt's state *and* the real `NSWindow` while **you** perform the gestures by
hand. It changes nothing and fixes nothing — it only reports.

```bash
python scripts/window_drag_probe.py --mode delegate --log /tmp/mac-delegate.txt
```

```bash
python scripts/window_drag_probe.py --mode restore --log /tmp/mac-restore.txt
```

```bash
python scripts/window_drag_probe.py --mode restore-on-drag --log /tmp/mac-ondrag.txt
```

**Start with `delegate`.** It is the decisive one — see Part 4.

---

## Part 2 — What to do in each run

Use the probe's own "Maximize / Restore" button to maximize, and "Quit" to end
the run (quitting is what writes the log file).

Run all five in `--mode delegate`. For the other two modes, G3 and G4 are enough.

| | Gesture | What *should* happen |
| --- | --- | --- |
| **G1** | Maximize, then single-click the title bar and release **without moving** | Nothing. The window stays maximized. |
| **G2** | Maximize, then **double-click** the title bar | It restores to its normal size, cleanly, once. |
| **G3** | Maximize, press and **hold ~1 second** without moving, then release | Nothing. No flash, no resize, no bounce. |
| **G4** | Maximize, then press and **drag away in one continuous motion** | It un-maximizes *under the pointer*, and the exact spot you grabbed stays under the cursor for the whole drag. |
| **G5** | With the window **not** maximized, drag it around | Moves normally. |

G4 is the one that matters most.

---

## Part 3 — What to write down and paste back

For each of the three runs, note in plain words what you actually **saw** —
especially:

- Did a plain click (G1) disturb the window at all?
- Did anything flicker, flash, or bounce?
- In G4, did the window end up under the cursor, or off to one side? If off to
  one side, roughly how far, and did that gap stay constant while you dragged?

Then paste back:

1. The three log files: `/tmp/mac-delegate.txt`, `/tmp/mac-restore.txt`,
   `/tmp/mac-ondrag.txt`
2. Your notes for each
3. The output of `sw_vers` and `python -c "import PySide6; print(PySide6.__version__)"`

---

## Part 4 — Context for an assistant working on the Mac

Everything below is **already established**. Do not re-derive it, and do not
"fix" it again.

### What was wrong, and what is already fixed

`QWidget.setGeometry()` clears `Qt::WindowMaximized` from the widget's own
`data.window_state` **without telling the `QWindow`**. So calling it before
`showNormal()` makes `showNormal()` compare equal to the state the widget
already believes it is in, return early, and never reach the platform — leaving
the window manager owning the geometry of a window it still thinks is maximized.
Reproduced identically under the `offscreen` and `minimal` QPA plugins, so this
is cross-platform `QWidget` behaviour, not one backend's quirk. **Fixed** —
`showNormal()` now runs first (commit `fc189b7`).

On Linux the actual resolution was to stop restoring client-side altogether:
Mutter implements "shake loose" for `_NET_WM_MOVERESIZE` itself, un-maximizing
under the pointer with its own animation. That is what
`_COMPOSITOR_RESTORES_ON_DRAG` selects. Verified with real X11 input: the grab
offset stays constant for the whole drag. **This must not regress.**

Most recently: `SystemMoveGesture.press()` no longer starts the move on a bare
press when doing so would trigger the client-side restore. It arms, and the
restore runs from the first mouse *move*. That is why a plain click no longer
un-maximizes and why double-click-to-zoom works again.

### What is still wrong on macOS

Reported by the maintainer, after all of the above:

- The window flickers, and on press-and-hold it resizes small and then bounces
  back. AppKit animates the un-zoom, so the frame between `showNormal()` and
  `setGeometry()` **is** painted. A comment in the source previously claimed the
  pair was atomic; it is not, on macOS.
- During the drag the window is re-centred on the cursor rather than keeping the
  grab point. Note that `--mode restore` reproduces a misaligned drag **on Linux
  too**, even with the ordering fixed — so `setGeometry()` followed by
  `startSystemMove()` is racy on its own: the WM grabs at the old frame while the
  client moves it to a new one.

### The one question the probe answers

**Does `-[NSWindow performWindowDragWithEvent:]` un-zoom a zoomed window by
itself, the way Mutter does?**

- **If yes** — `--mode delegate` shows the window un-maximizing under the pointer
  with a constant grab offset — then the fix is to widen
  `_COMPOSITOR_RESTORES_ON_DRAG` to cover macOS, and `_restore_under_cursor()`
  becomes Windows-only. The whole class of bugs disappears.
- **If no**, then `--mode restore-on-drag` shows whether deferring the restore to
  first movement, plus suppressing AppKit's un-zoom animation, is enough.

Answer it from the probe's log. Do not answer it by reasoning about what AppKit
ought to do — two previous attempts at this bug failed exactly that way.

Also worth checking in the log: does `ns[zoomed=...]` report `1` after Qt's
`showMaximized()`? If AppKit does not consider the window zoomed at all, it has
no reason to un-zoom it on drag, and that settles the question immediately.

### Traps

- **`tests/conftest.py` forces `QT_QPA_PLATFORM=offscreen`.** There is no window
  manager under it, so the suite **cannot** see any of this and will pass either
  way. A green test run is not verification here. This is precisely how the
  original bug shipped with all its tests passing.
- **Never assert `isMaximized()`** for this behaviour — the widget flag is the
  thing that desyncs. Assert
  `window.windowHandle().windowStates() & Qt.WindowState.WindowMaximized`.
- **Do not add timers, polling, or retry loops.** That was tried and removed; it
  papers over the symptom and does not fix the cause.
- **Do not flip `_MOVE_STARTS_ON_DRAG` to include macOS.** It governs *all*
  drags, so that would change non-maximized dragging too, and lose
  `performWindowDragWithEvent:`'s handling of the user's title-bar double-click
  preference. The maximized case is already handled separately in `press()`.
- `window_drag.py`'s overlay path still calls `begin_system_move()` straight from
  a press with no move tracking. Known, deliberately deferred — do not fix it in
  the same change.

### Files

- `src/joyread/ui/widgets/window_gestures.py` — all of the behaviour
- `tests/unit/test_window_gestures.py` — the tests
- `scripts/window_drag_probe.py` — the diagnostic described above
