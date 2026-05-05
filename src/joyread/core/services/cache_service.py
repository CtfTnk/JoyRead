"""Placeholder cache service for thumbnail and future reader caches."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Generic, TypeVar


K = TypeVar("K")
V = TypeVar("V")


@dataclass(frozen=True)
class CacheConfig:
    memory_limit_mb: int


class MemoryCache(Generic[K, V]):
    def __init__(self, max_items: int = 256) -> None:
        self._max_items = max_items
        self._items: OrderedDict[K, V] = OrderedDict()

    def get(self, key: K) -> V | None:
        if key not in self._items:
            return None
        value = self._items.pop(key)
        self._items[key] = value
        return value

    def put(self, key: K, value: V) -> None:
        if key in self._items:
            self._items.pop(key)
        self._items[key] = value
        while len(self._items) > self._max_items:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()


class CacheService:
    def __init__(self, thumbnail_limit_mb: int, page_limit_mb: int) -> None:
        self.thumbnail_config = CacheConfig(thumbnail_limit_mb)
        self.page_config = CacheConfig(page_limit_mb)
        self.thumbnail_cache: MemoryCache[str, str] = MemoryCache()
        self.page_thumbnail_cache: MemoryCache[str, bytes] = MemoryCache()
