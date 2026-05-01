"""Placeholder thumbnail lookup service."""

from __future__ import annotations

from pathlib import Path

from joyread.core.models.book import Book
from joyread.core.services.cache_service import CacheService


class ThumbnailService:
    def __init__(self, cache_service: CacheService) -> None:
        self._cache_service = cache_service

    def thumbnail_path_for(self, book: Book) -> Path | None:
        cached = self._cache_service.thumbnail_cache.get(book.uuid)
        if cached:
            return Path(cached)
        if book.cover_thumbnail_path:
            self._cache_service.thumbnail_cache.put(book.uuid, book.cover_thumbnail_path)
            return Path(book.cover_thumbnail_path)
        return None
