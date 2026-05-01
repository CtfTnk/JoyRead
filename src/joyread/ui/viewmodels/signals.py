"""Small synchronous signal helper for testable ViewModels."""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar


T = TypeVar("T")


class Signal(Generic[T]):
    def __init__(self) -> None:
        self._subscribers: list[Callable[..., None]] = []

    def connect(self, callback: Callable[..., None]) -> None:
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def disconnect(self, callback: Callable[..., None]) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def emit(self, *args: object) -> None:
        for callback in tuple(self._subscribers):
            callback(*args)
