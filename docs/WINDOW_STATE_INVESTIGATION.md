# Window sizing: what is actually wrong

Investigation record for the maximize/restore/drag/resize behaviour on macOS.
Everything here was measured against a real cocoa window on macOS 26.6.1 with
PySide6 6.11.1, not inferred.

**Conclusion up front: this cannot be patched.** There are four separate records
of "is this window maximized", the code reads whichever is nearest to hand, and
three of the four are unreliable. Every fix so far has consisted of picking a
different one to trust.

---

## 1. Four records, none of them authoritative

| record | written by | fails when |
| --- | --- | --- |
| `QWidget.isMaximized()` | Qt's widget layer | `QWidget.setGeometry()` clears it without telling anyone |
| `QWindow.windowStates()` | Qt's platform layer | is sometimes never told about a platform-initiated zoom |
| `NSWindow.isZoomed()` | AppKit | always correct, and invisible to every Qt-level check |
| `QWidget.normalGeometry()` | Qt | destroyed by any animated platform resize — see §2 |

The window code keys off the first one almost everywhere: `_restore_would_run`,
`begin_system_move`, `begin_system_resize`, `SystemResizeBorder._sync`, and both
zoom-button handlers. That is the single deepest problem.

## 2. The remembered size is destroyed by the maximize animation

`normalGeometry()` sampled every 25ms across a drag-to-top fill:

```
t+0      frame=300,182 900x600     qwin=WindowNoState   normalGeometry=900x600
t+50     frame=216,131 1070x697    qwin=WindowNoState   normalGeometry=1070x697
t+150    frame=32,19  1446x912     qwin=WindowNoState   normalGeometry=1446x912
t+250    frame=3,2    1504x945     qwin=WindowNoState   normalGeometry=1504x945   <- isZoomed flips to 1
t+350    frame=0,0    1512x949     qwin=WindowNoState   normalGeometry=1512x949
```

AppKit animates the fill over ~350ms. Qt receives **19 intermediate frame
changes** and, because it does not yet know a maximize is happening, records
every one of them as the new "normal" geometry. By the time the animation ends,
Qt's memory of the size to restore to *is* the maximized size. 900x600 is gone
and never comes back.

This is the whole of the reported "it first tries to return to the initial size
then changes back", and of the zoom button restoring to full screen.

## 3. After drag-to-top, the three records disagree permanently

Tiling the window (`_zoomFill_`, which is what dragging to the top edge does),
then asking the module to drag it as the title bar would:

```
after tiling            widget.isMaximized=1  qwin=WindowMaximized  ns.isZoomed=1
after begin_system_move widget.isMaximized=0  qwin=WindowMaximized  ns.isZoomed=1
```

The restore sets the geometry, which clears the *widget* flag and nothing else.
So afterwards:

* our code asks `isMaximized()`, is told "no", and never tries to restore again
  — **the window can no longer be dragged out of maximized**;
* AppKit still considers the window zoomed and refuses to move or resize it —
  **the resize edge shows its cursor and then does nothing**;
* the zoom button asks `isMaximized()`, is told "no", and therefore calls
  `showMaximized()` — which forces Qt to re-assert the state and resyncs all
  three records. **That is why the button is the only thing that fixes it.**

Every reported symptom follows from this one table.

## 4. "On changing" versus "on settle" is a race, not two modes

Qt sometimes learns the zoom state and sometimes does not. In one run `qwin`
stayed `WindowNoState` for 475ms after a fill had completed; in another it read
`WindowMaximized` immediately. Which one you get depends on whether the next
gesture arrives before or after Qt reconciles — exactly the "quick and
continuous" versus "wait a while" split in the report.

## 5. What is *not* wrong

* The anchor deferral and the skipped `showNormal()` from the previous fix are
  both still correct, and both still needed. They addressed a real 350ms block
  and a real stale-event anchor. They just sit on top of the broken ownership.
* Linux is untouched by all of this: it delegates the whole gesture to Mutter.

---

## What was built: one owner per fact

`ui/widgets/window_state.py` owns both answers. Everything else -- the drag
gesture, the resize border, both zoom buttons -- asks it rather than deciding.

**Who is maximized?** The platform. `is_maximized()` reads `NSWindow.isZoomed()`
where it can and falls back to the widget flag only when it cannot. An NSWindow
that cannot be read returns `None`, not `False`: "no answer" must not be
mistaken for "not maximized", or a maximized window gets stranded exactly the
way the widget flag stranded it.

**What size does it return to?** Ours. `remember_restore_geometry()` latches it
from *user intent* -- at the start of a title-bar drag, at the start of a
resize, before the zoom button maximizes -- never from observed geometry. Those
are the moments when nothing is animating. `normalGeometry()` remains the
fallback for a window that has never been touched, and the only source off
macOS.

**Restoring** is `leave_maximized()`, the single way out, shared by the button
and the drag. On Cocoa it is a bare `setGeometry`: measured at 1.2ms with all
three records agreeing afterwards, against ~350ms for `showNormal()`, which is
`-[NSWindow zoom:]` and animates inside a nested run loop.
`NSWindowAnimationBehaviorNone` does not shorten it, and `zoom:` restores to
AppKit's own saved frame, which the same animation has already overwritten.

**Only shrink what is actually big.** `fills_the_screen()` gates the restore.
macOS keeps a native resize edge on a tiled window which bypasses our grips
entirely, so a window can be `isZoomed` at a size the user chose deliberately;
restoring it then throws that size away. Stated in geometry we can see rather
than in what the platform means by its flags, so it holds whatever tiling turns
out to mean.

### Gating

Every workaround keys off `on_cocoa()` -- the QPA backend, never `sys.platform`.
macOS running the offscreen plugin behaves the X11 way for all of it, and
`winId()` there is not an NSView pointer: handing it to `objc_object()`
**segfaults rather than raising**, straight past any `except`.

The behaviour predicates are kept separate from `on_cocoa()` itself even though
they only return it, because `on_cocoa()` is also that safety guard. A test
that wants to exercise macOS behaviour must not be able to switch the guard off
by accident -- which is exactly what happened once, and cost a segfault.

---

## Confirming a change

```bash
python scripts/window_drag_probe.py --mode shipping --log /tmp/run.txt
```

`shipping` routes the gesture through `SystemMoveGesture` itself, so the run
exercises what the app does rather than a copy of it. Maximize with the probe's
own button, then: single-click the title bar (nothing should happen), double
click it (restores once), press and hold a second (nothing), drag away in one
motion (un-maximizes under the pointer, grab point stays put), and drag a
non-maximized window around. The log prints `grab_offset=` on every move; in a
good run it stops changing once the drag is under way.

## Traps

- **The suite cannot see any of this.** `tests/conftest.py` forces
  `QT_QPA_PLATFORM=offscreen`, where there is no window manager and where
  `setGeometry()` behaves the X11 way. The tests pin call ordering, gating and
  arithmetic; they cannot pin the behaviour. A green run is not verification.
- **Never assert `isMaximized()`** for this behaviour -- the widget flag is the
  thing that desyncs. Assert
  `window.windowHandle().windowStates() & Qt.WindowState.WindowMaximized`, or
  ask `is_maximized()`.
- **No timers, polling, or retry loops.** Tried and removed once already. The
  one-event deferral in `SystemMoveGesture.move()` is event-driven, and that
  distinction is the point.
- **Do not fold `_MOVE_STARTS_ON_DRAG` into the macOS path.** It governs *all*
  drags, so it would change non-maximized dragging too and lose
  `performWindowDragWithEvent:`'s handling of the user's title-bar double-click
  preference.
- `window_drag.py` hands over from a press with no move tracking, because an
  overlay sees the press and nothing after it. Dragging a maximized window from
  an overlay is therefore still anchored to the discarded frame. Documented at
  the call site, deliberately not fixed.

## Known remaining

- The grab point drifts a few pixels on a fast drag: Qt and AppKit anchor at
  different instants, and closing that needs sub-frame synchronisation neither
  exposes.
- A flash when a drag begins very shortly after a maximize animation ends --
  the reconcile race in §4. The fixes are a timer or private API, both worse
  than the symptom.
- A zoom/restore size mismatch remains reachable by some path; reported after
  the `fills_the_screen` fix, cause not established.

## Private API

`_zoomFill_` and `_currentZoomState` appear in this document because they
reproduce a drag-to-top on demand. **Nothing shipped uses them**, and nothing
should.
