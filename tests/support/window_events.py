from contextlib import contextmanager

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QWidget


@contextmanager
def record_top_level_shows():
    """Catch even windows that are reparented before processEvents returns."""
    shown = []

    class ShowObserver(QObject):
        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.Show and isinstance(watched, QWidget) and watched.isWindow():
                shown.append((type(watched).__name__, watched.objectName()))
            return False

    app = QApplication.instance()
    observer = ShowObserver()
    app.installEventFilter(observer)
    try:
        yield shown
    finally:
        app.removeEventFilter(observer)
