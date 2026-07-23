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
from joyread.core.archive.limits import ArchiveOpenLimits, GIB, MEGAPIXEL
from joyread.core.services.hidden_space_service import (
    HiddenSpacePasswordError,
    HiddenSpaceService,
)
from joyread.infrastructure.config.settings_store import AppSettings, SettingsStore
from joyread.infrastructure.i18n import locale_service
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
THUMBNAIL_CACHE_MIN_MB = 8
THUMBNAIL_CACHE_MAX_MB = 512
DETAIL_THUMBNAIL_CACHE_MIN_MB = THUMBNAIL_CACHE_MIN_MB
DETAIL_THUMBNAIL_CACHE_MAX_MB = THUMBNAIL_CACHE_MAX_MB
ARCHIVE_POOL_MIN_MB = 128
ARCHIVE_POOL_MAX_MB = 8192
IMPORT_FOLDER_DEPTH_MIN = 1
IMPORT_FOLDER_DEPTH_MAX = 5
NESTED_ARCHIVE_DEPTH_MIN = 1
NESTED_ARCHIVE_DEPTH_MAX = 5
ARCHIVE_GLOBAL_FILE_DEPTH_MIN = 1
ARCHIVE_GLOBAL_FILE_DEPTH_MAX = 1000
UNLIMITED_DEPTH = -1
ARCHIVE_MAX_SOURCE_SIZE_MIN_GB = 1
ARCHIVE_MAX_SOURCE_SIZE_MAX_GB = 15
ARCHIVE_MAX_EXTRACTED_ITEM_MIN_GB = 1
ARCHIVE_MAX_EXTRACTED_ITEM_MAX_GB = 16
ARCHIVE_MAX_OPERATION_DATA_MIN_GB = 1
ARCHIVE_MAX_OPERATION_DATA_MAX_GB = 64
ARCHIVE_MAX_IMAGE_MEGAPIXELS_MIN = 1
ARCHIVE_MAX_IMAGE_MEGAPIXELS_MAX = 1000
ARCHIVE_EXTERNAL_COMMAND_TIMEOUT_MIN_SECONDS = 1
ARCHIVE_EXTERNAL_COMMAND_TIMEOUT_MAX_SECONDS = 3600
ARCHIVE_CACHE_STRATEGY_OPTIONS = tuple(ARCHIVE_CACHE_STRATEGY_LABELS.values())


class SettingsViewModel:
    def __init__(
        self,
        settings: AppSettings | None = None,
        settings_store: SettingsStore | None = None,
        hidden_space_service: HiddenSpaceService | None = None,
    ) -> None:
        settings = settings or AppSettings(storage_location="~/Documents/JoyRead-Library")
        self.state_changed: Signal[None] = Signal()
        # Emitted after the locale has been reloaded so the UI can refresh labels.
        self.language_changed: Signal[None] = Signal()
        # The cache fields are user-tunable and surface "Clear archive cache"
        # as a one-shot button. AppContext wires the side effects (resize the
        # actual caches, blow away on-disk pool entries) and refreshes the
        # current-usage label via ``refresh_archive_pool_usage``.
        self.cache_budgets_changed: Signal[None] = Signal()
        self.archive_depth_limits_changed: Signal[None] = Signal()
        self.archive_open_limits_changed: Signal[None] = Signal()
        self.clear_archive_pool_requested: Signal[None] = Signal()
        self.import_integrity_changed: Signal[None] = Signal()
        # The ViewModel owns only the user intent. AppContext/MainWindow owns
        # task scheduling, confirmation, and the maintenance service itself.
        self.library_maintenance_requested: Signal[None] = Signal()
        # Hidden Space side effects: surface password-setup outcome and
        # reset/revert events so MainWindow can refresh the shelf + sidebar
        # without poking VM internals.
        self.hidden_space_changed: Signal[None] = Signal()
        self.hidden_space_error: Signal[str] = Signal()

        self._settings_store = settings_store
        self._hidden_space_service = hidden_space_service
        self._archive_pool_bytes_provider: Callable[[], int] | None = None
        self.sections = (
            SettingsSection(SettingsSectionKey.GENERAL, locale_service.t("settings.section_general")),
            SettingsSection(SettingsSectionKey.TAGS, locale_service.t("settings.section_tags")),
            SettingsSection(SettingsSectionKey.PRIVACY, locale_service.t("settings.section_privacy")),
            SettingsSection(SettingsSectionKey.ABOUT, locale_service.t("settings.section_about"), lower_group=True),
        )
        self.current_section = SettingsSectionKey.GENERAL
        self.language = settings.language
        self.import_book_when_opening = settings.import_book_when_opening
        self.verify_imported_file_integrity = bool(
            getattr(settings, "verify_imported_file_integrity", True)
        )
        self.individual_read_window = settings.individual_read_window
        self.inspect_non_native_title_control = False
        self.storage_location = settings.storage_location
        self.reader_page_cache_mb = _clamp_int(
            getattr(settings, "reader_page_cache_mb", 512),
            READER_PAGE_CACHE_MIN_MB,
            READER_PAGE_CACHE_MAX_MB,
        )
        self.thumbnail_cache_mb = _clamp_int(
            settings.thumbnail_cache_mb,
            THUMBNAIL_CACHE_MIN_MB,
            THUMBNAIL_CACHE_MAX_MB,
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
        self.nested_archive_max_depth = _normalize_depth_limit(
            getattr(settings, "nested_archive_max_depth", 2),
            default=2,
            maximum=NESTED_ARCHIVE_DEPTH_MAX,
        )
        self.archive_global_file_max_depth = _normalize_depth_limit(
            getattr(settings, "archive_global_file_max_depth", 100),
            default=100,
            maximum=ARCHIVE_GLOBAL_FILE_DEPTH_MAX,
        )
        self.archive_max_source_size_enabled = bool(
            getattr(settings, "archive_max_source_size_enabled", True)
        )
        self.archive_max_source_size_gb = _clamp_int(
            getattr(settings, "archive_max_source_size_gb", 5),
            ARCHIVE_MAX_SOURCE_SIZE_MIN_GB,
            ARCHIVE_MAX_SOURCE_SIZE_MAX_GB,
        )
        self.archive_resource_guardrails_enabled = bool(
            getattr(settings, "archive_resource_guardrails_enabled", True)
        )
        self.archive_max_extracted_item_gb = _normalize_limit(
            getattr(settings, "archive_max_extracted_item_gb", 1),
            default=1,
            maximum=ARCHIVE_MAX_EXTRACTED_ITEM_MAX_GB,
        )
        self.archive_max_operation_data_gb = _normalize_limit(
            getattr(settings, "archive_max_operation_data_gb", 4),
            default=4,
            maximum=ARCHIVE_MAX_OPERATION_DATA_MAX_GB,
        )
        self.archive_max_image_megapixels = _normalize_limit(
            getattr(settings, "archive_max_image_megapixels", 400),
            default=400,
            maximum=ARCHIVE_MAX_IMAGE_MEGAPIXELS_MAX,
        )
        self.archive_external_command_timeout_seconds = _normalize_limit(
            getattr(settings, "archive_external_command_timeout_seconds", 300),
            default=300,
            maximum=ARCHIVE_EXTERNAL_COMMAND_TIMEOUT_MAX_SECONDS,
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
        locale_service.load_language(language)
        self.language_changed.emit()
        self.state_changed.emit()

    def set_import_book_when_opening(self, enabled: bool) -> None:
        if enabled == self.import_book_when_opening:
            return
        self.import_book_when_opening = enabled
        self._persist(import_book_when_opening=enabled)
        self.state_changed.emit()

    def set_verify_imported_file_integrity(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.verify_imported_file_integrity:
            return
        self.verify_imported_file_integrity = enabled
        self._persist(verify_imported_file_integrity=enabled)
        self.import_integrity_changed.emit()
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

    def set_thumbnail_cache_mb(self, value: int) -> None:
        clamped = _clamp_int(value, THUMBNAIL_CACHE_MIN_MB, THUMBNAIL_CACHE_MAX_MB)
        if clamped == self.thumbnail_cache_mb:
            return
        self.thumbnail_cache_mb = clamped
        self._persist(thumbnail_cache_mb=clamped)
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

    def set_nested_archive_max_depth(self, value: int) -> None:
        normalized = _normalize_depth_limit(
            value,
            default=self.nested_archive_max_depth,
            maximum=NESTED_ARCHIVE_DEPTH_MAX,
        )
        if normalized == self.nested_archive_max_depth:
            return
        self.nested_archive_max_depth = normalized
        self._persist(nested_archive_max_depth=normalized)
        self._emit_archive_limits_changed()

    def set_archive_global_file_max_depth(self, value: int) -> None:
        normalized = _normalize_depth_limit(
            value,
            default=self.archive_global_file_max_depth,
            maximum=ARCHIVE_GLOBAL_FILE_DEPTH_MAX,
        )
        if normalized == self.archive_global_file_max_depth:
            return
        self.archive_global_file_max_depth = normalized
        self._persist(archive_global_file_max_depth=normalized)
        self._emit_archive_limits_changed()

    @property
    def archive_open_limits(self) -> ArchiveOpenLimits:
        """Convert persisted UI values into the core's ``None``-based API."""

        resource_enabled = self.archive_resource_guardrails_enabled
        return ArchiveOpenLimits(
            nested_archive_max_depth=_depth_to_core_limit(self.nested_archive_max_depth),
            global_file_max_depth=_depth_to_core_limit(self.archive_global_file_max_depth),
            max_source_bytes=(
                self.archive_max_source_size_gb * GIB
                if self.archive_max_source_size_enabled
                else None
            ),
            max_extracted_item_bytes=(
                _limit_to_bytes(self.archive_max_extracted_item_gb, GIB)
                if resource_enabled
                else None
            ),
            max_operation_bytes=(
                _limit_to_bytes(self.archive_max_operation_data_gb, GIB)
                if resource_enabled
                else None
            ),
            max_image_pixels=(
                _limit_to_bytes(self.archive_max_image_megapixels, MEGAPIXEL)
                if resource_enabled
                else None
            ),
            external_command_timeout_seconds=(
                _limit_to_bytes(self.archive_external_command_timeout_seconds, 1)
                if resource_enabled
                else None
            ),
        )

    def set_archive_max_source_size_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.archive_max_source_size_enabled:
            return
        self.archive_max_source_size_enabled = enabled
        self._persist(archive_max_source_size_enabled=enabled)
        self._emit_archive_limits_changed()

    def set_archive_max_source_size_gb(self, value: int) -> None:
        normalized = _clamp_int(
            value,
            ARCHIVE_MAX_SOURCE_SIZE_MIN_GB,
            ARCHIVE_MAX_SOURCE_SIZE_MAX_GB,
        )
        if normalized == self.archive_max_source_size_gb:
            return
        self.archive_max_source_size_gb = normalized
        self._persist(archive_max_source_size_gb=normalized)
        self._emit_archive_limits_changed()

    def set_archive_resource_guardrails_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.archive_resource_guardrails_enabled:
            return
        self.archive_resource_guardrails_enabled = enabled
        self._persist(archive_resource_guardrails_enabled=enabled)
        self._emit_archive_limits_changed()

    def set_archive_max_extracted_item_gb(self, value: int) -> None:
        self._set_resource_limit(
            "archive_max_extracted_item_gb",
            value,
            default=self.archive_max_extracted_item_gb,
            maximum=ARCHIVE_MAX_EXTRACTED_ITEM_MAX_GB,
        )

    def set_archive_max_operation_data_gb(self, value: int) -> None:
        self._set_resource_limit(
            "archive_max_operation_data_gb",
            value,
            default=self.archive_max_operation_data_gb,
            maximum=ARCHIVE_MAX_OPERATION_DATA_MAX_GB,
        )

    def set_archive_max_image_megapixels(self, value: int) -> None:
        self._set_resource_limit(
            "archive_max_image_megapixels",
            value,
            default=self.archive_max_image_megapixels,
            maximum=ARCHIVE_MAX_IMAGE_MEGAPIXELS_MAX,
        )

    def set_archive_external_command_timeout_seconds(self, value: int) -> None:
        self._set_resource_limit(
            "archive_external_command_timeout_seconds",
            value,
            default=self.archive_external_command_timeout_seconds,
            maximum=ARCHIVE_EXTERNAL_COMMAND_TIMEOUT_MAX_SECONDS,
        )

    def _set_resource_limit(self, attribute: str, value: int, *, default: int, maximum: int) -> None:
        normalized = _normalize_limit(value, default=default, maximum=maximum)
        if normalized == getattr(self, attribute):
            return
        setattr(self, attribute, normalized)
        self._persist(**{attribute: normalized})
        self._emit_archive_limits_changed()

    def _emit_archive_limits_changed(self) -> None:
        self.archive_depth_limits_changed.emit()
        self.archive_open_limits_changed.emit()
        self.state_changed.emit()

    def request_clear_archive_pool(self) -> None:
        self.clear_archive_pool_requested.emit()

    def request_library_maintenance(self) -> None:
        self.library_maintenance_requested.emit()

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


def _normalize_depth_limit(value: object, *, default: int, maximum: int) -> int:
    try:
        coerced = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if coerced == UNLIMITED_DEPTH:
        return UNLIMITED_DEPTH
    if coerced < 1:
        return default
    return min(maximum, coerced)


def _normalize_limit(value: object, *, default: int, maximum: int) -> int:
    return _normalize_depth_limit(value, default=default, maximum=maximum)


def _limit_to_bytes(value: int, multiplier: int) -> int | None:
    return None if value == UNLIMITED_DEPTH else value * multiplier


def _depth_to_core_limit(value: int) -> int | None:
    """Convert the settings-only ``-1`` sentinel at the core boundary."""

    return None if value == UNLIMITED_DEPTH else value
