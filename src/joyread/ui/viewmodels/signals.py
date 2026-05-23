"""Small synchronous signal helper for testable ViewModels.

This module defines :class:`Signal`, JoyRead's own lightweight signal type.
It is *not* a Qt signal: subscribers run on the caller's thread, immediately,
no event loop hop. The intent is that ViewModels can publish state changes
in plain Python tests without needing a ``QApplication`` running.

Junior engineers should keep two contrasts in mind:

- Use :class:`Signal` between ViewModel ↔ ViewModel / ViewModel ↔ service
  callbacks. It is synchronous and lock-free.
- Use a ``QtCore.Signal`` between widgets, or whenever a callback must end
  up on the Qt UI thread. Qt signals queue across thread boundaries; this
  one does not.

When a Signal is emitted from a worker thread (e.g. inside a
``TaskService`` callback), the receiver is responsible for re-marshalling
onto the UI thread, typically with ``QTimer.singleShot(0, ...)``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
import weakref
from typing import Generic, TypeAlias, TypeVar

from joyread.infrastructure.logging.logging_service import describe_callback

try:  # pragma: no cover - exercised indirectly by widget tests when present.
    import shiboken6
except ImportError:  # pragma: no cover - keeps the helper usable outside Qt.
    shiboken6 = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


T = TypeVar("T")
_CallbackKey: TypeAlias = tuple[str, int, object | None]


class _Subscriber:
    """Wrap one callback without keeping Qt/Python bound methods alive forever."""

    def __init__(self, callback: Callable[..., None]) -> None:
        self.key = _callback_key(callback)
        self._weak_method: weakref.WeakMethod[object] | None = None
        self._strong_callback: Callable[..., None] | None = None
        try:
            self._weak_method = weakref.WeakMethod(callback)  # type: ignore[arg-type]
        except TypeError:
            self._strong_callback = callback

    def resolve(self) -> Callable[..., None] | None:
        callback = self._weak_method() if self._weak_method is not None else self._strong_callback
        if callback is None:
            return None
        owner = getattr(callback, "__self__", None)
        if owner is not None and _qt_object_is_deleted(owner):
            return None
        return callback


def _callback_key(callback: Callable[..., None]) -> _CallbackKey:
    owner = getattr(callback, "__self__", None)
    function = getattr(callback, "__func__", None)
    if owner is not None and function is not None:
        return ("method", id(owner), function)
    name = getattr(callback, "__name__", None)
    if owner is not None and name is not None:
        return ("builtin_method", id(owner), name)
    return ("callable", id(callback), None)


def _qt_object_is_deleted(value: object) -> bool:
    if shiboken6 is None or not hasattr(value, "metaObject"):
        return False
    try:
        return not shiboken6.isValid(value)
    except RuntimeError:
        return True


class Signal(Generic[T]):
    """Tiny synchronous signal used by ViewModels.

    This is not a Qt signal. It calls subscribers immediately on the caller's
    thread, which keeps pure ViewModel tests simple. When a ViewModel signal is
    emitted from a ``TaskService`` callback, the receiver must still respect
    the thread it is running on.

    Pass ``name`` to make DEBUG log lines self-describing
    (e.g. ``Signal("shelf.state_changed")``). The name is optional and only
    used in logging.
    """

    def __init__(self, name: str | None = None) -> None:
        self._subscribers: list[_Subscriber] = []
        self._name = name or "<anonymous>"

    def connect(self, callback: Callable[..., None]) -> None:
        key = _callback_key(callback)
        self._drop_dead_subscribers()
        if all(subscriber.key != key for subscriber in self._subscribers):
            self._subscribers.append(_Subscriber(callback))
            logger.debug(
                "Signal %s connect receivers=%d callback=%s",
                self._name,
                len(self._subscribers),
                describe_callback(callback),
            )

    def disconnect(self, callback: Callable[..., None]) -> None:
        key = _callback_key(callback)
        before = len(self._subscribers)
        self._subscribers = [
            subscriber for subscriber in self._subscribers if subscriber.key != key
        ]
        if len(self._subscribers) != before:
            logger.debug(
                "Signal %s disconnect receivers=%d callback=%s",
                self._name,
                len(self._subscribers),
                describe_callback(callback),
            )

    def emit(self, *args: object) -> None:
        # Iterate over a snapshot: callbacks are allowed to disconnect or
        # trigger UI teardown while this signal is being delivered.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Signal %s emit receivers=%d", self._name, len(self._subscribers)
            )
        dead_keys: set[_CallbackKey] = set()
        for subscriber in tuple(self._subscribers):
            callback = subscriber.resolve()
            if callback is None:
                dead_keys.add(subscriber.key)
                continue
            callback(*args)
        if dead_keys:
            self._subscribers = [
                subscriber
                for subscriber in self._subscribers
                if subscriber.key not in dead_keys
            ]

    def _drop_dead_subscribers(self) -> None:
        self._subscribers = [
            subscriber for subscriber in self._subscribers if subscriber.resolve() is not None
        ]
