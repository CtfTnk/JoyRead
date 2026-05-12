"""Cache-related app model values."""

from __future__ import annotations

from enum import StrEnum


class ArchiveCacheStrategy(StrEnum):
    ZIP_BUNDLE = "zip_bundle"
    HIDDEN_IMAGE_FILES = "hidden_image_files"


ARCHIVE_CACHE_STRATEGY_LABELS: dict[ArchiveCacheStrategy, str] = {
    ArchiveCacheStrategy.ZIP_BUNDLE: "Zip bundle",
    ArchiveCacheStrategy.HIDDEN_IMAGE_FILES: "Hidden image files",
}


def normalize_archive_cache_strategy(value: object) -> ArchiveCacheStrategy:
    text = str(value or ArchiveCacheStrategy.ZIP_BUNDLE.value).strip().lower()
    for strategy, label in ARCHIVE_CACHE_STRATEGY_LABELS.items():
        if text in {strategy.value, label.lower()}:
            return strategy
    return ArchiveCacheStrategy.ZIP_BUNDLE
