"""Settings page state for the first Figma-backed settings panel."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from joyread.core.models.cache import (
    ARCHIVE_CACHE_STRATEGY_LABELS,
    ArchiveCacheStrategy,
    normalize_archive_cache_strategy,
)
from joyread.core.services.hidden_space_service import (
    HiddenSpacePasswordError,
    HiddenSpaceService,
)
from joyread.infrastructure.config.settings_store import AppSettings, SettingsStore
from joyread.ui.viewmodels.signals import Signal


logger = logging.getLogger(__name__)


class SettingsSectionKey(StrEnum):
    GENERAL = "general"
    TAGS = "tags"
    PRIVACY = "privacy"
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
IMPORT_FOLDER_DEPTH_MIN = 1
IMPORT_FOLDER_DEPTH_MAX = 5
ARCHIVE_INTERNAL_DEPTH_MIN = 1
ARCHIVE_INTERNAL_DEPTH_MAX = 5
ARCHIVE_CACHE_STRATEGY_OPTIONS = tuple(ARCHIVE_CACHE_STRATEGY_LABELS.values())


class SettingsViewModel:
    def __init__(
        self,
        settings: AppSettings | None = None,
        settings_store: SettingsStore | None = None,
        hidden_space_service: HiddenSpaceService | None = None,
    ) -> None:
        settings = settings or AppSettings(storage_location="~/Documents/JoyRead")
        self.state_changed: Signal[None] = Signal()
        # The cache fields are user-tunable and surface "Clear archive cache"
        # as a one-shot button. AppContext wires the side effects (resize the
        # actual caches, blow away on-disk pool entries) and refreshes the
        # current-usage label via ``refresh_archive_pool_usage``.
        self.cache_budgets_changed: Signal[None] = Signal()
        self.clear_archive_pool_requested: Signal[None] = Signal()
        # Hidden Space side effects: surface password-setup outcome and
        # reset/revert events so MainWindow can refresh the shelf + sidebar
        # without poking VM internals.
        self.hidden_space_changed: Signal[None] = Signal()
        self.hidden_space_error: Signal[str] = Signal()

        self._settings_store = settings_store
        self._hidden_space_service = hidden_space_service
        self._archive_pool_bytes_provider: Callable[[], int] | None = None
        self.sections = (
            SettingsSection(SettingsSectionKey.GENERAL, "General"),
            SettingsSection(SettingsSectionKey.TAGS, "Tags"),
            SettingsSection(SettingsSectionKey.PRIVACY, "Privacy"),
            SettingsSection(SettingsSectionKey.ABOUT, "About", lower_group=True),
        )
        self.current_section = SettingsSectionKey.GENERAL
        self.language = settings.language
        self.import_book_when_opening = settings.import_book_when_opening
        self.individual_read_window = settings.individual_read_window
        self.inspect_non_native_title_control = False
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
        self.archive_cache_strategy = normalize_archive_cache_strategy(
            getattr(settings, "archive_cache_strategy", ArchiveCacheStrategy.ZIP_BUNDLE.value)
        )
        self.import_folder_max_depth = _clamp_int(
            getattr(settings, "import_folder_max_depth", 1),
            IMPORT_FOLDER_DEPTH_MIN,
            IMPORT_FOLDER_DEPTH_MAX,
        )
        self.archive_internal_max_depth = _clamp_int(
            getattr(settings, "archive_internal_max_depth", 2),
            ARCHIVE_INTERNAL_DEPTH_MIN,
            ARCHIVE_INTERNAL_DEPTH_MAX,
        )
        self.archive_pool_current_bytes = 0
        # Hidden Space surface state. The service is the source of truth;
        # mirroring these on the VM keeps the settings page renders synchronous.
        self.hidden_space_initialized = settings.hidden_space_password_hash is not None
        self.show_hidden_collection = bool(settings.show_hidden_collection)
        self.hidden_space_hint = settings.hidden_space_password_hint

    @property
    def archive_cache_strategy_label(self) -> str:
        return ARCHIVE_CACHE_STRATEGY_LABELS[self.archive_cache_strategy]

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

    def set_inspect_non_native_title_control(self, enabled: bool) -> None:
        if enabled == self.inspect_non_native_title_control:
            return
        self.inspect_non_native_title_control = enabled
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

    def set_archive_cache_strategy(self, value: str) -> None:
        strategy = normalize_archive_cache_strategy(value)
        if strategy == self.archive_cache_strategy:
            return
        self.archive_cache_strategy = strategy
        self._persist(archive_cache_strategy=strategy.value)
        self.cache_budgets_changed.emit()
        self.state_changed.emit()

    def set_import_folder_max_depth(self, value: int) -> None:
        clamped = _clamp_int(value, IMPORT_FOLDER_DEPTH_MIN, IMPORT_FOLDER_DEPTH_MAX)
        if clamped == self.import_folder_max_depth:
            return
        self.import_folder_max_depth = clamped
        self._persist(import_folder_max_depth=clamped)
        self.state_changed.emit()

    def set_archive_internal_max_depth(self, value: int) -> None:
        clamped = _clamp_int(value, ARCHIVE_INTERNAL_DEPTH_MIN, ARCHIVE_INTERNAL_DEPTH_MAX)
        if clamped == self.archive_internal_max_depth:
            return
        self.archive_internal_max_depth = clamped
        self._persist(archive_internal_max_depth=clamped)
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
        except Exception as exc:  # pragma: no cover - provider is host-supplied.
            logger.warning("archive_pool_bytes_provider failed: %s", exc)
            return
        if usage == self.archive_pool_current_bytes:
            return
        self.archive_pool_current_bytes = usage
        self.state_changed.emit()

    def set_hidden_space_service(self, service: HiddenSpaceService | None) -> None:
        # Storage reconfiguration rebuilds the service; the VM holds a
        # reference so the settings page can still call methods after a
        # storage swap without re-binding the widget tree.
        self._hidden_space_service = service

    def initialize_hidden_space(self, password: str, confirm: str, hint: str | None) -> bool:
        service = self._require_hidden_space_service()
        if service is None:
            return False
        try:
            service.initialize(password, confirm, hint)
        except HiddenSpacePasswordError as exc:
            self.hidden_space_error.emit(str(exc))
            return False
        self._refresh_hidden_space_state()
        self.hidden_space_changed.emit()
        return True

    def verify_hidden_space_password(self, password: str) -> bool:
        service = self._require_hidden_space_service()
        if service is None:
            return False
        return service.verify(password)

    def change_hidden_space_password(
        self,
        old_password: str,
        new_password: str,
        confirm: str,
        hint: str | None = None,
    ) -> bool:
        service = self._require_hidden_space_service()
        if service is None:
            return False
        try:
            service.change_password(old_password, new_password, confirm, hint)
        except HiddenSpacePasswordError as exc:
            self.hidden_space_error.emit(str(exc))
            return False
        self._refresh_hidden_space_state()
        self.hidden_space_changed.emit()
        return True

    def set_show_hidden_collection(self, enabled: bool) -> None:
        # Caller (settings page) is responsible for password gating before
        # turning the toggle on. We just persist + mirror here. No
        # early-return on "same value" because the mirrored ``self.show_hidden_collection``
        # can lag the persisted state (e.g. when the service was initialised
        # after VM construction); cheap settings_store.update is preferable
        # to a stale-cache no-op.
        service = self._require_hidden_space_service()
        if service is None:
            return
        service.set_show_hidden_collection(bool(enabled))
        self._refresh_hidden_space_state()
        self.hidden_space_changed.emit()

    def revert_hidden_space(self) -> None:
        service = self._require_hidden_space_service()
        if service is None:
            return
        service.revert_all()
        self.hidden_space_changed.emit()

    def reset_hidden_space(self) -> None:
        service = self._require_hidden_space_service()
        if service is None:
            return
        service.reset_and_erase()
        self._refresh_hidden_space_state()
        self.hidden_space_changed.emit()

    def _refresh_hidden_space_state(self) -> None:
        if self._settings_store is None:
            return
        current = self._settings_store.load()
        self.hidden_space_initialized = current.hidden_space_password_hash is not None
        self.show_hidden_collection = bool(current.show_hidden_collection)
        self.hidden_space_hint = current.hidden_space_password_hint
        self.state_changed.emit()

    def _require_hidden_space_service(self) -> HiddenSpaceService | None:
        if self._hidden_space_service is None:
            logger.warning("HiddenSpaceService not wired to SettingsViewModel; ignoring request")
            return None
        return self._hidden_space_service

    def _persist(self, **changes: object) -> None:
        if self._settings_store is not None:
            self._settings_store.update(**changes)


def _clamp_int(value: object, minimum: int, maximum: int) -> int:
    try:
        coerced = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        coerced = minimum
    return max(minimum, min(maximum, coerced))
