# macOS maximized-drag handoff

**Temporary.** Delete this file once the behaviour below is confirmed on a Mac.

The measurement this file used to ask for has been taken. What is left is one
confirmation run, described in Part 1. Everything else is the record of what was
found, so that nobody re-derives it.

---

## Part 1 — The one thing left to do

```bash
conda activate ./.conda/joyread-py312
python scripts/window_drag_probe.py --mode shipping --log /tmp/mac-shipping.txt
```

`shipping` routes the gesture through `SystemMoveGesture` itself, so the run
exercises what the app actually does rather than a re-implementation of it.

Maximize with the probe's own button, then:

| | Gesture | What should happen |
| --- | --- | --- |
| **G1** | Single-click the title bar, no movement | Nothing at all. |
| **G2** | Double-click the title bar | Restores once, cleanly. |
| **G3** | Press and hold ~1s without moving, release | Nothing. No flash, no bounce, no resize. |
| **G4** | Press and drag away in one motion | Un-maximizes under the pointer, and the spot you grabbed stays under the cursor for the whole drag. |
| **G5** | Not maximized: drag it around | Moves normally. |

**G4 is the one that matters**, and specifically whether the grab offset holds
constant. The log prints `grab_offset=` on every move; in a good run the numbers
stop changing once the drag is under way.

---

## Part 2 — What was measured, and what changed

Measured on macOS 26.6.1, PySide6 6.11.1, against a real cocoa window.

### `showNormal()` costs 350ms, every time

On Cocoa `showNormal()` on a zoomed window is `-[NSWindow zoom:]`, which
animates the un-zoom inside a nested run loop and **blocks its caller**:

| call | blocked for |
| --- | --- |
| `showNormal()` | 350.6 ms |
| `showNormal()` with `NSWindowAnimationBehaviorNone` | 352.2 ms |
| `setGeometry()` alone | **0.9 ms** |

Blocking a third of a second inside a mouse handler is the flicker, the bounce,
and the "click and drag feel combined into one action". Setting
`animationBehavior` does not help — that was the obvious fix and it is wrong.

**The frame change alone is enough on Cocoa.** After a plain `setGeometry()` on
a maximized window, all three records agree: `isMaximized()` 0,
`QWindow.windowStates()` `WindowNoState`, `NSWindow.isZoomed()` 0 — and
`normalGeometry()` survives. This is the opposite of the offscreen/X11
behaviour that `_restore_under_cursor` was originally written around, which is
why the call is now conditional on `_GEOMETRY_CLEARS_MAXIMIZED`.

### AppKit does not un-zoom on drag, and `delegate` loses the remembered size

`performWindowDragWithEvent:` drags a zoomed window at full size. It does not
"shake loose" the way Mutter does, so `_COMPOSITOR_RESTORES_ON_DRAG` must not be
widened to macOS. Worse, Qt then clears `WindowMaximized` while overwriting
`normalGeometry()` with the maximized size:

```
PRESS    qt[geom=0,33 1512x949 normal=900x600  max=1 qwin=WindowMaximized]
MOVE     qt[geom=0,56 1512x949 normal=1512x949 max=0 qwin=WindowNoState]
```

`normal=900x600` becomes `normal=1512x949` and never comes back — after one such
drag the zoom button restores to full size. That alone rules the mode out.

### The move loop anchors on the event it is handed

An `NSEvent`'s location is relative to the window frame *as it was when the
event was created*, and `performWindowDragWithEvent:` takes its grab point from
there. Restoring and starting the move from the same event therefore anchors
the drag to a frame that has already been discarded. In the `restore` logs, two
separate press-and-holds — restored to x=317 and x=267 — both came to rest at
Cocoa origin `(0,0)`, which is the *maximized* frame's origin, and is the
"window moves to the lower-left" in the report.

The fix lets one more mouse event arrive before handing over, so the location
the move loop reads was measured against the frame the window actually has. It
costs one pointer sample.

---

## Part 3 — What could not be verified here

**The G4 grab offset has not been confirmed end-to-end.** Driving a real
held-button drag needs synthesised input, which needs Accessibility permission
this environment does not have. The two changes are each justified by direct
measurement, and the deferral is correct under either reading of how AppKit
picks its anchor — but Part 1 is what turns that into evidence.

`tests/conftest.py` forces `QT_QPA_PLATFORM=offscreen`, where there is no window
manager and where `setGeometry()` behaves the X11 way rather than the Cocoa way.
**The suite cannot see any of this and passes either way.** The tests added with
the fix pin the call ordering and the platform confinement, not the behaviour.

---

## Part 4 — Traps

- **Never assert `isMaximized()`** for this behaviour — the widget flag is the
  thing that desyncs. Assert
  `window.windowHandle().windowStates() & Qt.WindowState.WindowMaximized`.
- **Do not add timers, polling, or retry loops.** Tried and removed once
  already. The deferral in `SystemMoveGesture.move()` is event-driven, not
  timed, and that distinction is the point.
- **Do not flip `_MOVE_STARTS_ON_DRAG` to include macOS.** It governs *all*
  drags, so it would change non-maximized dragging too and lose
  `performWindowDragWithEvent:`'s handling of the user's title-bar double-click
  preference. The maximized case is handled separately in `press()`.
- **Do not widen `_GEOMETRY_CLEARS_MAXIMIZED` or `_MOVE_ANCHORS_ON_ITS_EVENT`.**
  Both are properties of AppKit, and both are wrong everywhere else.
- `window_drag.py`'s overlay path still calls `begin_system_move()` straight
  from a press with no move tracking. Known, deliberately deferred.

## Files

- `src/joyread/ui/widgets/window_gestures.py` — all of the behaviour
- `tests/unit/test_window_gestures.py` — the tests
- `scripts/window_drag_probe.py` — the probe described above
