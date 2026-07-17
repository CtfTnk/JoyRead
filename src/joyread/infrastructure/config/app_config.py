"""Application-level configuration defaults."""

from __future__ import annotations

from dataclasses import dataclass
from os import cpu_count


@dataclass(frozen=True)
class AppConfig:
    app_name: str = "JoyRead"
    app_author: str = "JoyRead"
    organization_domain: str = "joyread.local"
    max_background_workers: int = max(1, min(4, cpu_count() or 1))
    # Cache budgets are also surfaced via ``AppSettings`` so users can tune
    # them at runtime. The ``AppConfig`` values act as the install-time floor
    # and as the defaults applied when no user setting exists yet.
    reader_page_cache_mb: int = 512
    thumbnail_cache_mb: int = 64
    archive_extraction_pool_mb: int = 1024
    cover_index_max_items: int = 1024
    page_prefetch_before: int = 2
    page_prefetch_after: int = 4
