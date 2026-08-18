"""Small Qt-free synchronous event hook for application workflows."""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar
import weakref


T = TypeVar("T")


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
        if self._weak_method is not None:
            return self._weak_method()
        return self._strong_callback


def _callback_key(callback: Callable[..., None]) -> tuple[str, int, object | None]:
    owner = getattr(callback, "__self__", None)
    function = getattr(callback, "__func__", None)
    if owner is not None and function is not None:
        return "method", id(owner), function
    name = getattr(callback, "__name__", None)
    if owner is not None and name is not None:
        return "builtin_method", id(owner), name
    return "callable", id(callback), None


class EventHook(Generic[T]):
    """Publish synchronous application events without depending on Qt or UI."""

    def __init__(self) -> None:
        self._subscribers: list[_Subscriber] = []

    def connect(self, callback: Callable[..., None]) -> None:
        self._drop_dead_subscribers()
        key = _callback_key(callback)
        if all(subscriber.key != key for subscriber in self._subscribers):
            self._subscribers.append(_Subscriber(callback))

    def disconnect(self, callback: Callable[..., None]) -> None:
        key = _callback_key(callback)
        self._subscribers = [
            subscriber for subscriber in self._subscribers if subscriber.key != key
        ]

    def emit(self, *args: object) -> None:
        dead: set[tuple[str, int, object | None]] = set()
        for subscriber in tuple(self._subscribers):
            callback = subscriber.resolve()
            if callback is None:
                dead.add(subscriber.key)
                continue
            callback(*args)
        if dead:
            self._subscribers = [
                subscriber for subscriber in self._subscribers if subscriber.key not in dead
            ]

    def _drop_dead_subscribers(self) -> None:
        self._subscribers = [
            subscriber for subscriber in self._subscribers if subscriber.resolve() is not None
        ]
