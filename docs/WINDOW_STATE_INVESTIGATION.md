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

## Proposed rebuild: one owner per fact

**Who is maximized?** The platform, never the widget. One `is_maximized()` that
reads `NSWindow.isZoomed()` on macOS and `QWindow.windowStates()` elsewhere,
used by the gestures, the resize border and the zoom buttons alike.

**What size does it restore to?** Ours, never `normalGeometry()`. Latched from
*user intent* rather than from observed frames: at first show, before we
maximize, and at the start of a user-initiated resize or move. All three are
moments when nothing is animating, so a platform animation cannot poison it.

**Who performs the state change?** The platform. `-[NSWindow zoom:]` is public
API and was measured to correctly untile a filled window, which
`setGeometry()` does not. Restoring to a geometry we own also means the frame
ends up genuinely small, which is what clears `isZoomed` — so owning the size
fixes the stuck-tiled case as a side effect.

pyobjc is already a declared runtime dependency (`pyproject.toml`) and the
PyInstaller spec already bundles `objc`/`Foundation`/`AppKit` on darwin, so
reading AppKit directly costs nothing new.

**Private API is for diagnosis only.** `_zoomFill_` and `_currentZoomState`
appear in this document because they reproduce the user's gesture on demand.
Nothing proposed above uses them.
