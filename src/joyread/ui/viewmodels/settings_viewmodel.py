"""Settings page state for the first Figma-backed settings panel."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from joyread.infrastructure.config.settings_store import AppSettings, SettingsStore
from joyread.ui.viewmodels.signals import Signal


class SettingsSectionKey(StrEnum):
    GENERAL = "general"
    TAGS = "tags"
    PRIVATE_SPACE = "private_space"
    ABOUT = "about"


@dataclass(frozen=True)
class SettingsSection:
    key: SettingsSectionKey
    label: str
    lower_group: bool = False


# Cache budgets are clamped to these conservative ranges to avoid surprising
# the user with extreme allocations and to keep the spinner UI manageable.
READER_PAGE_CACHE_MIN_MB = 64
READER_PAGE_CACHE_MAX_MB = 4096
DETAIL_THUMBNAIL_CACHE_MIN_MB = 8
DETAIL_THUMBNAIL_CACHE_MAX_MB = 512
ARCHIVE_POOL_MIN_MB = 128
ARCHIVE_POOL_MAX_MB = 8192


class SettingsViewModel:
    def __init__(self, settings: AppSettings | None = None, settings_store: SettingsStore | None = None) -> None:
        settings = settings or AppSettings(storage_location="~/Documents/JoyRead")
        self.state_changed: Signal[None] = Signal()
        # The cache fields are user-tunable and surface "Clear archive cache"
        # as a one-shot button. AppContext wires the side effects (resize the
        # actual caches, blow away on-disk pool entries) and refreshes the
        # current-usage label via ``refresh_archive_pool_usage``.
        self.cache_budgets_changed: Signal[None] = Signal()
        self.clear_archive_pool_requested: Signal[None] = Signal()

        self._settings_store = settings_store
        self._archive_pool_bytes_provider: Callable[[], int] | None = None
        self.sections = (
            SettingsSection(SettingsSectionKey.GENERAL, "General"),
            SettingsSection(SettingsSectionKey.TAGS, "Tags"),
            SettingsSection(SettingsSectionKey.PRIVATE_SPACE, "Private Space"),
            SettingsSection(SettingsSectionKey.ABOUT, "About", lower_group=True),
        )
        self.current_section = SettingsSectionKey.GENERAL
        self.language = settings.language
        self.import_book_when_opening = settings.import_book_when_opening
        self.individual_read_window = settings.individual_read_window
        self.storage_location = settings.storage_location
        self.reader_page_cache_mb = _clamp_int(
            getattr(settings, "reader_page_cache_mb", 512),
            READER_PAGE_CACHE_MIN_MB,
            READER_PAGE_CACHE_MAX_MB,
        )
        self.detail_thumbnail_cache_mb = _clamp_int(
            getattr(settings, "detail_thumbnail_cache_mb", 64),
            DETAIL_THUMBNAIL_CACHE_MIN_MB,
            DETAIL_THUMBNAIL_CACHE_MAX_MB,
        )
        self.archive_extraction_pool_mb = _clamp_int(
            getattr(settings, "archive_extraction_pool_mb", 1024),
            ARCHIVE_POOL_MIN_MB,
            ARCHIVE_POOL_MAX_MB,
        )
        self.archive_pool_current_bytes = 0

    def set_section(self, section: SettingsSectionKey | str) -> None:
        normalized = SettingsSectionKey(section)
        if normalized == self.current_section:
            return
        self.current_section = normalized
        self.state_changed.emit()

    def set_language(self, language: str) -> None:
        if language == self.language:
            return
        self.language = language
        self._persist(language=language)
        self.state_changed.emit()

    def set_import_book_when_opening(self, enabled: bool) -> None:
        if enabled == self.import_book_when_opening:
            return
        self.import_book_when_opening = enabled
        self._persist(import_book_when_opening=enabled)
        self.state_changed.emit()

    def set_individual_read_window(self, enabled: bool) -> None:
        if enabled == self.individual_read_window:
            return
        self.individual_read_window = enabled
        self._persist(individual_read_window=enabled)
        self.state_changed.emit()

    def set_storage_location(self, path: str) -> None:
        if path == self.storage_location:
            return
        self.storage_location = path
        self._persist(storage_location=path)
        self.state_changed.emit()

    def set_reader_page_cache_mb(self, value: int) -> None:
        clamped = _clamp_int(value, READER_PAGE_CACHE_MIN_MB, READER_PAGE_CACHE_MAX_MB)
        if clamped == self.reader_page_cache_mb:
            return
        self.reader_page_cache_mb = clamped
        self._persist(reader_page_cache_mb=clamped)
        self.cache_budgets_changed.emit()
        self.state_changed.emit()

    def set_detail_thumbnail_cache_mb(self, value: int) -> None:
        clamped = _clamp_int(value, DETAIL_THUMBNAIL_CACHE_MIN_MB, DETAIL_THUMBNAIL_CACHE_MAX_MB)
        if clamped == self.detail_thumbnail_cache_mb:
            return
        self.detail_thumbnail_cache_mb = clamped
        self._persist(detail_thumbnail_cache_mb=clamped)
        self.cache_budgets_changed.emit()
        self.state_changed.emit()

    def set_archive_extraction_pool_mb(self, value: int) -> None:
        clamped = _clamp_int(value, ARCHIVE_POOL_MIN_MB, ARCHIVE_POOL_MAX_MB)
        if clamped == self.archive_extraction_pool_mb:
            return
        self.archive_extraction_pool_mb = clamped
        self._persist(archive_extraction_pool_mb=clamped)
        self.cache_budgets_changed.emit()
        self.state_changed.emit()

    def request_clear_archive_pool(self) -> None:
        self.clear_archive_pool_requested.emit()

    def set_archive_pool_bytes_provider(self, provider: Callable[[], int] | None) -> None:
        """Inject a callable that reports the disk pool's live byte usage.

        The viewmodel stays service-agnostic; the AppContext supplies a thin
        lambda over ``ArchiveExtractionPool.current_bytes``.
        """

        self._archive_pool_bytes_provider = provider
        self.refresh_archive_pool_usage()

    def refresh_archive_pool_usage(self) -> None:
        provider = self._archive_pool_bytes_provider
        if provider is None:
            return
        try:
            usage = max(0, int(provider()))
        except Exception:  # pragma: no cover - provider is host-supplied.
            return
        if usage == self.archive_pool_current_bytes:
            return
        self.archive_pool_current_bytes = usage
        self.state_changed.emit()

    def _persist(self, **changes: object) -> None:
        if self._settings_store is not None:
            self._settings_store.update(**changes)


def _clamp_int(value: object, minimum: int, maximum: int) -> int:
    try:
        coerced = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        coerced = minimum
    return max(minimum, min(maximum, coerced))
