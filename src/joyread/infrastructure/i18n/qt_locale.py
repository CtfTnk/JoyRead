"""Qt application presentation locale, separate from stable application identity."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLibraryInfo, QObject, QTranslator, Signal


class LanguageEvents(QObject):
    changed = Signal()


def language_events() -> LanguageEvents | None:
    app = QCoreApplication.instance()
    if app is None:
        return None
    events = getattr(app, "_joyread_language_events", None)
    if events is None:
        events = LanguageEvents(app)
        app._joyread_language_events = events
    return events


def refresh_application_language() -> None:
    """Called on the UI thread; retained translators outlive their Qt install."""
    from joyread.infrastructure.i18n.locale_service import active_language_code, app_display_name

    app = QCoreApplication.instance()
    if app is None:
        return
    # setApplicationName remains JoyRead: paths, WM_CLASS and IPC use it.
    if hasattr(app, "setApplicationDisplayName"):
        app.setApplicationDisplayName(str(app_display_name()))
    previous = getattr(app, "_joyread_qt_translator", None)
    if previous is not None:
        app.removeTranslator(previous)
        previous.deleteLater()
    app._joyread_qt_translator = None
    code = {"zh": "zh_CN", "ja": "ja"}.get(active_language_code())
    if code:
        translator = QTranslator(app)
        directory = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
        if translator.load(str(directory / f"qtbase_{code}.qm")):
            app.installTranslator(translator)
            app._joyread_qt_translator = translator
        else:
            translator.deleteLater()
    events = language_events()
    if events is not None:
        events.changed.emit()
