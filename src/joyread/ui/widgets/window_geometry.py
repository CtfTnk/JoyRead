"""Window sizing and screen adaptation, independent of library/reader content."""

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, QTimer
from PySide6.QtWidgets import QApplication
import shiboken6

from joyread.ui.resources.styles.theme import Theme


def fitted_window_rect(wanted: QSize, minimum: QSize, available: QRect) -> QRect:
    size = QSize(max(minimum.width(), min(wanted.width(), available.width())),
                 max(minimum.height(), min(wanted.height(), available.height())))
    rect = QRect(QPoint(), size)
    if wanted.width() > available.width() or wanted.height() > available.height() or not available.contains(QRect(available.topLeft(), size)):
        rect.moveTopLeft(available.topLeft())
    else:
        rect.moveCenter(available.center())
    return rect


class WindowGeometryController(QObject):
    """Remember deliberate normal resizes, never maximized or screen-fit sizes."""

    def __init__(self, window, viewmodel, kind: str):
        super().__init__(window)
        self._window = window
        self._vm = viewmodel
        self._kind = kind
        self._default = QSize(Theme.window_width, Theme.window_height) if kind == "library" else QSize(Theme.reader_width, Theme.reader_height)
        saved = viewmodel.window_size(kind)
        self._wanted = QSize(*saved) if saved else QSize(self._default)
        self._applying = False
        self._pending = None
        self._screen = None
        self._handle = None
        self._last_applied = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(Theme.window_size_save_delay_ms)
        self._timer.timeout.connect(self._save)
        window.installEventFilter(self)
        viewmodel.window_sizes_reset.connect(self.reset)
        # EventHook is Qt-free; explicitly unsubscribe before the Qt child dies.
        window.destroyed.connect(lambda: viewmodel.window_sizes_reset.disconnect(self.reset))

    def place(self):
        window = self._window
        screen = window.screen() or QApplication.primaryScreen()
        if screen is None or window.isMaximized() or window.isFullScreen():
            return
        rect = fitted_window_rect(self._wanted, window.minimumSize(), screen.availableGeometry())
        if screen.availableGeometry().width() < self._default.width() or screen.availableGeometry().height() < self._default.height():
            rect.moveTopLeft(screen.availableGeometry().topLeft())
        self._applying = True
        try:
            self._last_applied = rect.size()
            window.setGeometry(rect)
        finally:
            self._applying = False

    def _bind_screen(self):
        screen = self._window.screen()
        if screen is self._screen:
            return
        if self._screen is not None and shiboken6.isValid(self._screen):
            self._screen.availableGeometryChanged.disconnect(self._screen_changed)
        self._screen = screen
        if screen is not None:
            screen.availableGeometryChanged.connect(self._screen_changed)

    def _screen_changed(self, *_):
        self._save()
        self._bind_screen()
        self.place()

    def reset(self):
        self._timer.stop()
        self._pending = None
        self._wanted = QSize(self._default)
        self._applying = True
        try:
            self._window.showNormal()
        finally:
            self._applying = False
        self.place()

    def _save(self):
        self._timer.stop()
        pending, self._pending = self._pending, None
        if pending is not None:
            self._vm.remember_window_size(self._kind, pending)

    def _fit_restored_window(self):
        screen = self._window.screen()
        if screen is not None and not screen.availableGeometry().contains(self._window.geometry()):
            self.place()

    def eventFilter(self, watched, event):
        kind = event.type()
        window = self._window
        if kind == QEvent.Type.Show:
            if self._handle is None:
                self._handle = window.windowHandle()
                if self._handle is not None:
                    self._handle.screenChanged.connect(self._screen_changed)
                self._bind_screen()
                # Apply again after native creation (notably Cocoa frame offsets).
                self.place()
        elif kind == QEvent.Type.Resize and window.isVisible() and not self._applying and not window.isMaximized() and not window.isFullScreen() and not window.isMinimized():
            if window.size() != self._last_applied:
                self._wanted = QSize(window.size())
                self._last_applied = QSize(window.size())
                self._pending = (window.width(), window.height())
                self._timer.start()
        elif kind == QEvent.Type.Close:
            self._save()
        elif kind == QEvent.Type.WindowStateChange and not window.isMaximized() and not window.isFullScreen() and not window.isMinimized():
            QTimer.singleShot(0, self, self._fit_restored_window)
        return False
