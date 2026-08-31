"""Measure what the platform does to a maximized frameless window during a drag.

The maximized title-bar drag is a negotiation between JoyRead, Qt, and the
window manager, and the three do not agree about who un-maximizes. This probe
records every observable piece of that state while a *human* performs the
gesture, so the answer comes from measurement rather than from reasoning about
what AppKit or Mutter ought to do.

Run it, then perform each gesture the prompt asks for and paste the log back.

    python scripts/window_drag_probe.py --mode delegate

Modes, in the order worth trying:

``delegate``        Hand the maximized window to the platform untouched. If the
                    platform un-maximizes it under the pointer by itself, this
                    is the whole fix and the client-side restore can go. This is
                    what Linux/Mutter does, and it is the decisive experiment on
                    macOS.
``restore``         The current shipping behaviour off Linux: restore
                    client-side on mouse *press*, then start the system move.
``restore-on-drag`` Same restore, but deferred until the pointer has actually
                    moved, so a plain click or a double click never un-maximizes.

Nothing here is a fix. It only reports.
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


def _appkit_state(window: QWidget) -> str:
    native = _nswindow(window)
    if native is None:
        return ""
    try:
        frame = native.frame()
        return (
            f"  ns[zoomed={int(native.isZoomed())} "
            f"frame={frame.origin.x:.0f},{frame.origin.y:.0f} "
            f"{frame.size.width:.0f}x{frame.size.height:.0f} "
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
        f"max={int(window.isMaximized())} qwin={states}]" + _appkit_state(window)
    )


# --- the gesture under test -------------------------------------------------


class ProbeTitleBar(QWidget):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self._mode = mode
        self._press_pos = None
        self._restored = False
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
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()
        QTimer.singleShot(600, lambda: log(f"BUTTON zoom, settled: {snapshot(window)}"))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        window = self.window()
        self._press_pos = event.globalPosition().toPoint()
        self._restored = False
        log(f"PRESS   {snapshot(window)}")

        if self._mode == "restore-on-drag":
            # Nothing on press: a click, and a double click, must not disturb
            # the window at all.
            event.accept()
            return

        self._start(window, "press")
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
        if self._mode == "restore-on-drag" and not self._restored:
            travelled = (cursor - self._press_pos).manhattanLength()
            if travelled >= QApplication.startDragDistance():
                self._restored = True
                self._start(window, f"first move ({travelled}px)")
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        log(f"RELEASE {snapshot(self.window())}")
        QTimer.singleShot(700, lambda: log(f"SETTLED {snapshot(self.window())}"))
        event.accept()

    def _start(self, window: QWidget, when: str) -> None:
        maximized = window.isMaximized()
        if maximized and self._mode != "delegate":
            log(f"  -> client-side restore on {when}")
            window_gestures._restore_under_cursor(window)
            log(f"  -> after restore: {snapshot(window)}")
        elif maximized:
            log(f"  -> handing the maximized window to the platform on {when}")
        started = window_gestures._request_system_move(window)
        log(f"  -> startSystemMove() returned {started}")


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
        choices=("delegate", "restore", "restore-on-drag"),
        default="delegate",
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
