"""Measure what the platform does to a maximized frameless window during a drag.

The maximized title-bar drag is a negotiation between JoyRead, Qt, and the
window manager, and the three do not agree about who un-maximizes. This probe
records every observable piece of that state while a *human* performs the
gesture, so the answer comes from measurement rather than from reasoning about
what AppKit or Mutter ought to do.

Run it, then perform each gesture the prompt asks for and paste the log back.

    python scripts/window_drag_probe.py --mode delegate

Two modes:

``shipping``  Route the gesture through :class:`SystemMoveGesture` itself, so
              the run exercises what the app actually does. Use this to confirm
              a fix. This is the default.
``delegate``  Hand the maximized window to the platform untouched, skipping the
              client-side restore entirely. This is what Linux/Mutter can be
              left to do; on macOS it was measured as wrong, and the mode is
              kept so that stays checkable rather than remembered.

Earlier revisions carried two more modes that reimplemented restore strategies
the code no longer has. They were frozen copies with nothing to keep them
honest, so they are gone; ``docs/WINDOW_STATE_INVESTIGATION.md`` records what
they measured.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from joyread.ui.widgets import window_gestures
from joyread.ui.widgets.window_gestures import SystemMoveGesture
from joyread.ui.widgets.window_state import (
    is_maximized,
    restore_geometry,
    toggle_maximized,
)

_T0 = time.monotonic()
_LINES: list[str] = []


def log(message: str) -> None:
    line = f"{(time.monotonic() - _T0) * 1000:9.1f}ms  {message}"
    _LINES.append(line)
    print(line, flush=True)


# --- AppKit introspection ---------------------------------------------------
#
# Qt's window state and AppKit's are separate records and the whole question is
# whether they agree. pyobjc is already a hard dependency on darwin, so reading
# the NSWindow directly costs nothing; everywhere else this degrades to "".


def _nswindow(window: QWidget):
    if sys.platform != "darwin":
        return None
    try:
        import objc

        view = objc.objc_object(c_void_p=ctypes.c_void_p(int(window.winId())))
        return view.window()
    except Exception as error:  # pragma: no cover - diagnostic only
        log(f"    (could not reach the NSWindow: {error!r})")
        return None


def _rect(frame) -> str:
    # Reached through a raw pointer, pyobjc has no signature for -frame and
    # hands back a plain ((x, y), (w, h)) tuple rather than an NSRect. Reading
    # it as an NSRect is what made every ns[...] field in the first round of
    # logs come back "unreadable", losing the AppKit half of the measurement.
    if isinstance(frame, tuple):
        (x, y), (width, height) = frame
    else:
        x, y = frame.origin.x, frame.origin.y
        width, height = frame.size.width, frame.size.height
    return f"{x:.0f},{y:.0f} {width:.0f}x{height:.0f}"


def _appkit_state(window: QWidget) -> str:
    native = _nswindow(window)
    if native is None:
        return ""
    try:
        return (
            f"  ns[zoomed={int(native.isZoomed())} "
            f"frame={_rect(native.frame())} "
            f"anim={native.animationBehavior()}]"
        )
    except Exception as error:  # pragma: no cover - diagnostic only
        return f"  ns[unreadable: {error!r}]"


def snapshot(window: QWidget) -> str:
    handle = window.windowHandle()
    geometry = window.geometry()
    normal = window.normalGeometry()
    states = str(handle.windowStates()).replace("WindowState.", "") if handle else "?"
    return (
        f"qt[geom={geometry.x()},{geometry.y()} {geometry.width()}x{geometry.height()} "
        f"normal={normal.width()}x{normal.height()} "
        f"max={int(window.isMaximized())} qwin={states}] "
        f"own[maximized={int(is_maximized(window))} "
        f"restore={restore_geometry(window).width()}x{restore_geometry(window).height()}]"
        + _appkit_state(window)
    )


# --- the gesture under test -------------------------------------------------


class ProbeTitleBar(QWidget):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self._mode = mode
        self._gesture = SystemMoveGesture()
        self.setObjectName("ProbeTitleBar")
        self.setFixedHeight(48)
        self.setStyleSheet("#ProbeTitleBar { background: #2f5d8a; } QLabel { color: white; }")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 8, 0)
        layout.addWidget(QLabel(f"drag me  —  mode: {mode}"))
        layout.addStretch(1)
        zoom = QPushButton("Maximize / Restore")
        zoom.clicked.connect(self._toggle_zoom)
        layout.addWidget(zoom)
        close = QPushButton("Quit")
        close.clicked.connect(lambda: self.window().close())
        layout.addWidget(close)

    def _toggle_zoom(self) -> None:
        window = self.window()
        log(f"BUTTON zoom, before: {snapshot(window)}")
        toggle_maximized(window)
        QTimer.singleShot(600, lambda: log(f"BUTTON zoom, settled: {snapshot(window)}"))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        window = self.window()
        log(f"PRESS   {snapshot(window)}")
        if self._mode == "shipping":
            log(f"  -> gesture.press() returned {self._gesture.press(self, event)}")
        else:
            log(f"  -> handing the window to the platform untouched")
            log(f"  -> startSystemMove() returned {window_gestures._request_system_move(window)}")
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return
        window = self.window()
        cursor = event.globalPosition().toPoint()
        geometry = window.geometry()
        log(
            f"  MOVE  cursor={cursor.x()},{cursor.y()}  "
            f"grab_offset={cursor.x() - geometry.x()},{cursor.y() - geometry.y()}  "
            f"{snapshot(window)}"
        )
        if self._mode == "shipping":
            was_maximized = is_maximized(window)
            took = self._gesture.move(self, event)
            if was_maximized:
                log(f"  -> gesture.move() returned {took}: {snapshot(window)}")
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._gesture.release()
        log(f"RELEASE {snapshot(self.window())}")
        QTimer.singleShot(700, lambda: log(f"SETTLED {snapshot(self.window())}"))
        event.accept()

class ProbeWindow(QMainWindow):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(ProbeTitleBar(mode))
        body = QLabel(
            "1. Click “Maximize / Restore”.\n"
            "2. Press and HOLD on the blue bar without moving. Wait a second.\n"
            "3. Release. Note what you saw.\n"
            "4. Maximize again, then drag off in ONE continuous motion.\n"
            "5. Quit, and paste the log."
        )
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.setStyleSheet("background:#f4f4f4; color:#333; font-size:14px;")
        layout.addWidget(body, 1)
        self.setCentralWidget(central)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("shipping", "delegate"),
        default="shipping",
    )
    parser.add_argument("--log", type=Path, default=None, help="also write the log here")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    window = ProbeWindow(args.mode)
    window.resize(900, 600)
    window.show()

    log(f"platform={sys.platform}  qpa={app.platformName()}  mode={args.mode}")
    log(f"screen available={app.primaryScreen().availableGeometry()}")
    QTimer.singleShot(400, lambda: log(f"START   {snapshot(window)}"))

    code = app.exec()
    if args.log:
        args.log.write_text("\n".join(_LINES) + "\n")
        print(f"\nwrote {args.log}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
