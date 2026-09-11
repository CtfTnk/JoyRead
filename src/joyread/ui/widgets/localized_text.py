"""In-place translation bindings for Qt text properties, never input values.

Bindings belong to the receiving QObject. Qt disconnects the receiver when it
is destroyed; no global widget registry or window reconstruction is needed.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Slot
from PySide6.QtWidgets import QLabel, QPushButton, QLineEdit

from joyread.infrastructure.i18n.locale_service import TranslatedText
from joyread.infrastructure.i18n.qt_locale import language_events


class _TextBindings(QObject):
    def __init__(self, target: QObject) -> None:
        super().__init__(target)
        self.values: dict[str, TranslatedText] = {}
        events = language_events()
        if events is not None:
            events.changed.connect(self.refresh)

    @Slot()
    def refresh(self) -> None:
        target = self.parent()
        for setter, value in tuple(self.values.items()):
            getattr(target, setter)(value.resolve())


def set_localized(target: QObject, setter: str, value: str) -> None:
    """Assign text and retain its key only when it came from the locale service.

    Reassigning plain text clears a previous binding, so e.g. a real book title
    is never replaced with a stale loading label on the next language change.
    """
    bindings = getattr(target, "_joyread_text_bindings", None)
    if isinstance(value, TranslatedText) and not (isinstance(target, QLineEdit) and setter == "setText"):
        if bindings is None:
            bindings = _TextBindings(target)
            target._joyread_text_bindings = bindings
        bindings.values[setter] = value
    elif bindings is not None:
        bindings.values.pop(setter, None)
    getattr(target, setter)(value.resolve() if isinstance(value, TranslatedText) else value)


class LocalizedLabel(QLabel):
    def __init__(self, text="", parent=None, **kwargs) -> None:
        if isinstance(text, str):
            super().__init__(parent, **kwargs)
            set_localized(self, "setText", text)
        else:
            super().__init__(text, **kwargs)


class LocalizedPushButton(QPushButton):
    def __init__(self, text="", parent=None, **kwargs) -> None:
        if isinstance(text, str):
            super().__init__(parent, **kwargs)
            set_localized(self, "setText", text)
        else:
            super().__init__(text, **kwargs)
