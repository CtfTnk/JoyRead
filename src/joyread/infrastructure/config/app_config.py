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
    thumbnail_cache_memory_limit_mb: int = 128
    page_cache_memory_limit_mb: int = 512
    page_prefetch_before: int = 2
    page_prefetch_after: int = 4
