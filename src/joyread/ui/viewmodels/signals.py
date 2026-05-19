"""Small synchronous signal helper for testable ViewModels."""

from __future__ import annotations

from collections.abc import Callable
import weakref
from typing import Generic, TypeAlias, TypeVar

try:  # pragma: no cover - exercised indirectly by widget tests when present.
    import shiboken6
except ImportError:  # pragma: no cover - keeps the helper usable outside Qt.
    shiboken6 = None  # type: ignore[assignment]


T = TypeVar("T")
_CallbackKey: TypeAlias = tuple[str, int, object | None]


class _Subscriber:
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
    def __init__(self) -> None:
        self._subscribers: list[_Subscriber] = []

    def connect(self, callback: Callable[..., None]) -> None:
        key = _callback_key(callback)
        self._drop_dead_subscribers()
        if all(subscriber.key != key for subscriber in self._subscribers):
            self._subscribers.append(_Subscriber(callback))

    def disconnect(self, callback: Callable[..., None]) -> None:
        key = _callback_key(callback)
        self._subscribers = [
            subscriber for subscriber in self._subscribers if subscriber.key != key
        ]

    def emit(self, *args: object) -> None:
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
