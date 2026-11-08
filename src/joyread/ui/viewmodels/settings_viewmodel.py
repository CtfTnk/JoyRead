"""Settings page state for the first Figma-backed settings panel."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Callable, TypeVar

from joyread.core.models.reader_prefetch import (
    PREFETCH_BEFORE_DEFAULT, PREFETCH_AFTER_DEFAULT,
    PREFETCH_BEFORE_MAX, PREFETCH_AFTER_MAX, prefetch_count,
)

from joyread.core.models.cache import (
    ARCHIVE_CACHE_STRATEGY_LABELS,
    ArchiveCacheStrategy,
    normalize_archive_cache_strategy,
)
from joyread.core.models.import_policy import (
    DEFAULT_CANONICAL_IMPORT_POLICY,
    CanonicalImportPolicy,
    normalize_canonical_import_policy,
)
from joyread.core.archive.limits import ArchiveOpenLimits, GIB, MEGAPIXEL
from joyread.core.file_types import ARCHIVE_PASSWORD_ACCESS_ENABLED
from joyread.core.reader.models import ReaderDirection, ReaderFitMode, ReaderSettings, ReaderTransitionMode
from joyread.core.services.hidden_space_service import (
    HiddenSpaceService,
    HiddenSpaceState,
)
from joyread.infrastructure.config.settings_store import (
    AppSettings,
    SettingsStore,
    WINDOWS_BACKGROUND_CLEANUP_SECONDS_MAX,
    WINDOWS_BACKGROUND_CLEANUP_SECONDS_MIN,
)
from joyread.app.open_policy import LibraryState, OpenDisposition, OpenOrigin, decide_open
from joyread.infrastructure.i18n import locale_service
from joyread.app.tasking import TaskExecutor, TaskPriority
from joyread.core.repositories.sqlite_sealing_repository import UnhideOutcome
from joyread.core.models.vault import BookSealState, book_seal_states
from joyread.core.services.vault_service import RecoveryCodeDraft, VaultDraft, confirm_recovery_code
from joyread.core.vault import (
    PASSPHRASE_RECOMMENDED_LENGTH,
    RecoveryCode,
    VaultError,
    VaultLockedError,
    normalize_passphrase,
    validate_new_passphrase,
)
from joyread.ui.viewmodels.hidden_space_text import hidden_space_error_text
from joyread.ui.viewmodels.signals import Signal


logger = logging.getLogger(__name__)


class SettingsSectionKey(StrEnum):
    GENERAL = "general"
    READING = "reading"
    # Resource-budget tuning: archive traversal limits and cache sizes. Split
    # out of GENERAL, which had become the default home for anything that did
    # not obviously belong to Tags or Privacy.
    ARCHIVE = "archive"
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
ARCHIVE_POOL_MIN_GB = 1
ARCHIVE_POOL_MAX_GB = 50
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

#: Locale key per policy. The *value* persisted is always the enum's own
#: string; only the text shown to the user is translated, so switching
#: language can never rewrite the setting.
_CANONICAL_IMPORT_POLICY_KEYS: dict[CanonicalImportPolicy, str] = {
    CanonicalImportPolicy.NEVER: "settings.canonical_import_never",
    CanonicalImportPolicy.EXPENSIVE_AND_NESTED: "settings.canonical_import_expensive",
    CanonicalImportPolicy.ALWAYS: "settings.canonical_import_always",
}


def canonical_import_policy_label(policy: CanonicalImportPolicy) -> str:
    return locale_service.t(_CANONICAL_IMPORT_POLICY_KEYS[policy])


def canonical_import_policy_options() -> tuple[str, ...]:
    """Display labels in the enum's own order: least to most eager."""

    return tuple(canonical_import_policy_label(policy) for policy in CanonicalImportPolicy)


def canonical_import_policy_from_label(value: str) -> CanonicalImportPolicy:
    """Map a shown label back to a policy.

    Falls through to the model's normaliser, which understands the enum
    values and the English labels -- so a value read from settings, or one
    chosen before the language changed, still resolves.
    """

    for policy in CanonicalImportPolicy:
        if value == canonical_import_policy_label(policy):
            return policy
    return normalize_canonical_import_policy(value)


_T = TypeVar("_T")

#: Unhide outcomes that leave a book in Hidden until the worker decrypts it.
_STILL_DECRYPTING = frozenset({UnhideOutcome.UNSEALING, UnhideOutcome.BUSY})


@dataclass(frozen=True)
class HiddenSpaceStatus:
    """Outstanding Hidden Space work, for the Privacy status line."""

    encrypting: int = 0
    decrypting: int = 0
    failed: int = 0

    @classmethod
    def from_states(cls, states) -> HiddenSpaceStatus:  # noqa: ANN001 - iterable of BookSealState.
        counts = {state: 0 for state in BookSealState}
        for state in states:
            counts[state] += 1
        return cls(
            encrypting=counts[BookSealState.ENCRYPTING],
            decrypting=counts[BookSealState.DECRYPTING],
            failed=counts[BookSealState.ENCRYPT_FAILED] + counts[BookSealState.DECRYPT_FAILED],
        )


class SettingsViewModel:
    def __init__(
        self,
        settings: AppSettings | None = None,
        settings_store: SettingsStore | None = None,
        hidden_space_service: HiddenSpaceService | None = None,
        *,
        task_service: TaskExecutor | None = None,
    ) -> None:
        settings = settings or AppSettings(storage_location="~/Documents/JoyRead-Library")
        self.state_changed: Signal[None] = Signal()
        self.window_sizes_reset: Signal[None] = Signal()
        self.prefetch_window_changed: Signal[None] = Signal()
        self.page_prefetch_before = prefetch_count(settings.page_prefetch_before, default=PREFETCH_BEFORE_DEFAULT, maximum=PREFETCH_BEFORE_MAX)
        self.page_prefetch_after = prefetch_count(settings.page_prefetch_after, default=PREFETCH_AFTER_DEFAULT, maximum=PREFETCH_AFTER_MAX)
        self._window_sizes = {"library": settings.library_window_size, "reader": settings.reader_window_size}
        # Emitted after the locale has been reloaded so the UI can refresh labels.
        self.language_changed: Signal[None] = Signal()
        self.windows_background_changed: Signal[bool] = Signal()
        self.windows_background_cleanup_changed: Signal[tuple[bool, int]] = Signal()
        # The cache fields are user-tunable and surface "Clear archive cache"
        # as a one-shot button. AppContext wires the side effects (resize the
        # actual caches, blow away on-disk pool entries) and refreshes the
        # current-usage label via ``refresh_archive_pool_usage``.
        self.cache_budgets_changed: Signal[None] = Signal()
        self.archive_depth_limits_changed: Signal[None] = Signal()
        self.archive_open_limits_changed: Signal[None] = Signal()
        self.clear_archive_pool_requested: Signal[None] = Signal()
        # Pool usage moves on its own, reported from the caching workers, and
        # it feeds exactly one label. It gets its own signal so the settings
        # page can update that label in place instead of rebuilding the
        # section it sits in -- see ``refresh_archive_pool_usage``.
        self.archive_pool_usage_changed: Signal[None] = Signal()
        self.import_integrity_changed: Signal[None] = Signal()
        self.canonical_import_policy_changed: Signal[None] = Signal()
        # The ViewModel owns only the user intent. AppContext/MainWindow owns
        # task scheduling, confirmation, and the maintenance service itself.
        self.library_maintenance_requested: Signal[None] = Signal()
        # Hidden Space: ``hidden_space_changed`` follows every state change
        # (set up, unlocked, locked, reset) so MainWindow can refresh the
        # shelf and sidebar; ``hidden_space_status_changed`` only the
        # encryption status line, which moves on its own as books seal.
        self.hidden_space_changed: Signal[None] = Signal()
        self.hidden_space_status_changed: Signal[None] = Signal()

        self._settings_store = settings_store
        self._hidden_space_service = hidden_space_service
        # Hidden Space work is slow (scrypt) or waits on the sealing worker,
        # so it runs here and reports back on the UI thread.
        self._task_service = task_service
        self._archive_pool_bytes_provider: Callable[[], int] | None = None
        self.sections = (
            SettingsSection(SettingsSectionKey.GENERAL, locale_service.t("settings.section_general")),
            SettingsSection(SettingsSectionKey.READING, locale_service.t("settings.section_reading")),
            SettingsSection(SettingsSectionKey.ARCHIVE, locale_service.t("settings.section_archive")),
            SettingsSection(SettingsSectionKey.TAGS, locale_service.t("settings.section_tags")),
            SettingsSection(SettingsSectionKey.PRIVACY, locale_service.t("settings.section_privacy")),
            SettingsSection(SettingsSectionKey.ABOUT, locale_service.t("settings.section_about"), lower_group=True),
        )
        self.current_section = SettingsSectionKey.GENERAL
        self.language = settings.language
        self.import_on_read_drop = settings.import_on_read_drop
        self.verify_imported_file_integrity = bool(
            getattr(settings, "verify_imported_file_integrity", True)
        )
        self.individual_read_window = settings.individual_read_window
        self.windows_background_enabled = settings.windows_background_enabled
        self.windows_background_auto_cleanup_enabled = settings.windows_background_auto_cleanup_enabled
        self.windows_background_auto_cleanup_seconds = _clamp_int(
            settings.windows_background_auto_cleanup_seconds,
            WINDOWS_BACKGROUND_CLEANUP_SECONDS_MIN,
            WINDOWS_BACKGROUND_CLEANUP_SECONDS_MAX,
        )
        self.default_reader_settings = settings.default_reader_settings
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
        self.archive_extraction_pool_gb = _clamp_int(
            getattr(settings, "archive_extraction_pool_gb", 5),
            ARCHIVE_POOL_MIN_GB,
            ARCHIVE_POOL_MAX_GB,
        )
        self.archive_cache_strategy = normalize_archive_cache_strategy(
            getattr(settings, "archive_cache_strategy", ArchiveCacheStrategy.ZIP_BUNDLE.value)
        )
        self.canonical_import_policy = normalize_canonical_import_policy(
            getattr(
                settings,
                "canonical_import_policy",
                DEFAULT_CANONICAL_IMPORT_POLICY.value,
            )
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
        # The persisted Show Collections preference: whether to ask for the
        # passphrase at launch. Whether Hidden Space is on screen now also
        # needs the vault unlocked; see ``hidden_space_visible``.
        self.show_hidden_collection = bool(settings.show_hidden_collection)
        self.hidden_space_status = HiddenSpaceStatus()
        self.purge_encrypted_cache_on_close = bool(
            getattr(settings, "purge_encrypted_cache_on_close", True)
        )

    @property
    def shows_encrypted_archive_settings(self) -> bool:
        """Whether Privacy lists the encrypted-archive cache switch.

        The switch only governs pages of password-protected archives, which the
        Reader no longer opens while ``ARCHIVE_PASSWORD_ACCESS_ENABLED`` is
        closed. The stored preference is kept so reopening the gate restores it.
        """

        return ARCHIVE_PASSWORD_ACCESS_ENABLED

    @property
    def archive_cache_strategy_label(self) -> str:
        return ARCHIVE_CACHE_STRATEGY_LABELS[self.archive_cache_strategy]

    @property
    def canonical_import_policy_label(self) -> str:
        return canonical_import_policy_label(self.canonical_import_policy)

    @property
    def canonical_import_policy_options(self) -> tuple[str, ...]:
        return canonical_import_policy_options()

    def set_section(self, section: SettingsSectionKey | str) -> None:
        normalized = SettingsSectionKey(section)
        if normalized == self.current_section:
            return
        self.current_section = normalized
        self.state_changed.emit()

    def set_default_reader_preference(self, field: str, value: object) -> None:
        """Change a future reader's starting value, leaving open books untouched."""

        enum_fields = {
            "direction": ReaderDirection,
            "transition_mode": ReaderTransitionMode,
            "fit_mode": ReaderFitMode,
        }
        bool_fields = {
            "custom_enabled", "always_one_page", "vertical_custom_enabled", "vertical_fit_width",
        }
        if field in enum_fields:
            value = enum_fields[field](value)
        elif field in bool_fields:
            value = bool(value)
        elif field == "page_spacing":
            value = _clamp_int(value, 0, 200)
        elif field == "vertical_zoom_percent":
            value = _clamp_int(value, 25, 200)
        else:
            raise ValueError(f"Unsupported default reader preference: {field}")
        updated = replace(self.default_reader_settings, **{field: value})
        if updated == self.default_reader_settings:
            return
        self._persist(default_reader_settings=updated)
        self.default_reader_settings = updated

    def set_language(self, language: str) -> None:
        if language == self.language and language != "System":
            return
        self.language = language
        self._persist(language=language)
        locale_service.load_language(language)
        # Language has its own presentation event: a generic rebuild loses edits.
        self.language_changed.emit()

    def set_import_on_read_drop(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.import_on_read_drop:
            return
        self.import_on_read_drop = enabled
        self._persist(import_on_read_drop=enabled)
        self.state_changed.emit()

    def drop_disposition(self, origin: OpenOrigin, library_state: LibraryState) -> OpenDisposition:
        """Resolve a Library drop using the dedicated preference."""

        if origin not in (OpenOrigin.DROP_READ, OpenOrigin.DROP_IMPORT):
            raise ValueError(f"Not a drop origin: {origin!r}")
        return decide_open(
            origin,
            library_state=library_state,
            import_on_read_drop=self.import_on_read_drop,
        )

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

    def set_windows_background_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.windows_background_enabled:
            return
        updates = {"windows_background_enabled": enabled}
        if enabled:
            # A fresh opt-in is a new notification cycle, even when the user
            # had suppressed the explanation during an earlier enabled period.
            updates["windows_background_notice_suppressed"] = False
        self._persist(**updates)
        self.windows_background_enabled = enabled
        self.windows_background_changed.emit(enabled)
        self.state_changed.emit()

    def set_windows_background_auto_cleanup_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.windows_background_auto_cleanup_enabled:
            return
        self._persist(windows_background_auto_cleanup_enabled=enabled)
        self.windows_background_auto_cleanup_enabled = enabled
        self.windows_background_cleanup_changed.emit(
            (enabled, self.windows_background_auto_cleanup_seconds)
        )
        self.state_changed.emit()

    def set_windows_background_auto_cleanup_seconds(self, seconds: int) -> None:
        clamped = _clamp_int(
            seconds,
            WINDOWS_BACKGROUND_CLEANUP_SECONDS_MIN,
            WINDOWS_BACKGROUND_CLEANUP_SECONDS_MAX,
        )
        if clamped == self.windows_background_auto_cleanup_seconds:
            return
        self._persist(windows_background_auto_cleanup_seconds=clamped)
        self.windows_background_auto_cleanup_seconds = clamped
        self.windows_background_cleanup_changed.emit(
            (self.windows_background_auto_cleanup_enabled, clamped)
        )
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

    def set_archive_extraction_pool_gb(self, value: int) -> None:
        clamped = _clamp_int(value, ARCHIVE_POOL_MIN_GB, ARCHIVE_POOL_MAX_GB)
        if clamped == self.archive_extraction_pool_gb:
            return
        self.archive_extraction_pool_gb = clamped
        self._persist(archive_extraction_pool_gb=clamped)
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

    def set_canonical_import_policy(self, value: str) -> None:
        policy = canonical_import_policy_from_label(value)
        if policy == self.canonical_import_policy:
            return
        self.canonical_import_policy = policy
        self._persist(canonical_import_policy=policy.value)
        self.canonical_import_policy_changed.emit()
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
        # Deliberately not ``state_changed``: this arrives from the caching
        # workers about once a second while a book is being cached, and a
        # section-wide rebuild for one number tears down every control in it,
        # including any popup the user has open on one.
        self.archive_pool_usage_changed.emit()

    def set_hidden_space_service(
        self,
        service: HiddenSpaceService | None,
        *,
        task_service: TaskExecutor | None = None,
    ) -> None:
        # Storage reconfiguration rebuilds the service; the VM holds a
        # reference so the settings page can still call methods after a
        # storage swap without re-binding the widget tree.
        self._hidden_space_service = service
        if task_service is not None:
            self._task_service = task_service
        self.refresh_hidden_space_status()

    # -- Hidden Space: state ------------------------------------------------------

    @property
    def hidden_space_state(self) -> HiddenSpaceState:
        service = self._hidden_space_service
        return service.state if service is not None else HiddenSpaceState.NONE

    @property
    def hidden_space_visible(self) -> bool:
        """Hidden Space is on screen: Show Collections on and the vault unlocked."""

        return self.show_hidden_collection and self.hidden_space_state is HiddenSpaceState.UNLOCKED

    @property
    def hidden_space_has_vault(self) -> bool:
        return self.hidden_space_state in (HiddenSpaceState.LOCKED, HiddenSpaceState.UNLOCKED)

    @staticmethod
    def passphrase_rule_error(passphrase: str, confirm: str) -> str | None:
        """Fast check of a new passphrase pair, for keeping its dialog open."""

        try:
            validate_new_passphrase(passphrase, confirm)
        except VaultError as error:
            return hidden_space_error_text(error)
        return None

    @staticmethod
    def passphrase_is_short(passphrase: str) -> bool:
        return len(normalize_passphrase(passphrase or "")) < PASSPHRASE_RECOMMENDED_LENGTH

    @staticmethod
    def recovery_code_format_error(typed: str) -> str | None:
        """Fast check that *typed* reads as a recovery code at all."""

        try:
            RecoveryCode.parse(typed)
        except VaultError as error:
            return hidden_space_error_text(error)
        return None

    @staticmethod
    def export_recovery_code(code: RecoveryCode, destination: Path) -> str | None:
        """Write the recovery code to a file the user chose. Returns error text.

        Plain text so it opens anywhere and prints; the user picked the place,
        so it is not app data and does not go through PathService.
        """

        text = locale_service.t("hidden_space.code_file_text", code=code.formatted())
        try:
            Path(destination).write_text(text.rstrip("\n") + "\n", encoding="utf-8")
        except OSError as error:
            logger.warning("Recovery code could not be saved: %s", type(error).__name__)
            return locale_service.t("hidden_space_error.code_not_saved")
        return None

    @staticmethod
    def recovery_code_confirmation_error(expected: RecoveryCode, typed: str) -> str | None:
        """Fast check that *typed* is the code just shown. No secret leaves here."""

        try:
            confirm_recovery_code(expected, typed)
        except VaultError as error:
            return hidden_space_error_text(error)
        return None

    @property
    def hidden_space_status_text(self) -> str:
        """One line for Privacy: problems first, then work still running."""

        status = self.hidden_space_status
        t = locale_service.t
        if status.failed:
            return t("settings.hidden_status_failed", count=status.failed)
        if status.encrypting:
            return t("settings.hidden_status_encrypting", count=status.encrypting)
        if status.decrypting:
            if self.hidden_space_state is HiddenSpaceState.LOCKED:
                return t("settings.hidden_status_decrypt_waiting", count=status.decrypting)
            return t("settings.hidden_status_decrypting", count=status.decrypting)
        return t("settings.hidden_status_encrypted")

    def refresh_hidden_space_status(self) -> None:
        """Re-count outstanding encryption work, for the Privacy status line."""

        service = self._hidden_space_service
        sealing = service.sealing if service is not None and service.has_vault else None
        status = HiddenSpaceStatus()
        if sealing is not None:
            try:
                states = book_seal_states(sealing.jobs_by_book())
            except Exception:
                logger.warning("Hidden Space status could not be read", exc_info=True)
            else:
                status = HiddenSpaceStatus.from_states(states.values())
        if status != self.hidden_space_status:
            self.hidden_space_status = status
            self.hidden_space_status_changed.emit()

    def handle_hidden_space_locked(self) -> None:
        """The vault locked, for whatever reason. The preference is unchanged."""

        self._refresh_hidden_space_state()

    # -- Hidden Space: setup, the legacy upgrade, unlocking ---------------------------
    #
    # Each command runs on the task service and calls exactly one of
    # ``on_success`` or ``on_failure`` (with text to show) on the UI thread.

    def prepare_hidden_space_setup(
        self,
        passphrase: str,
        confirm: str,
        *,
        on_success: Callable[[VaultDraft], None],
        on_failure: Callable[[str], None],
    ) -> None:
        """Build a new vault to confirm. Nothing is stored until it is."""

        service = self._require_hidden_space_service()
        if service is None:
            on_failure(locale_service.t("hidden_space_error.unavailable"))
            return
        self._run_hidden_space_task(
            "hidden-space-prepare",
            lambda: service.prepare_vault(passphrase, confirm),
            on_success,
            on_failure,
            changes_state=False,
        )

    def prepare_hidden_space_upgrade(
        self,
        old_password: str,
        *,
        on_success: Callable[[VaultDraft], None],
        on_failure: Callable[[str], None],
    ) -> None:
        """Check the earlier password and build a vault that keeps it."""

        service = self._require_hidden_space_service()
        if service is None:
            on_failure(locale_service.t("hidden_space_error.unavailable"))
            return
        self._run_hidden_space_task(
            "hidden-space-prepare-upgrade",
            lambda: service.prepare_upgrade(old_password),
            on_success,
            on_failure,
            changes_state=False,
        )

    def finish_hidden_space_setup(
        self,
        draft: VaultDraft,
        typed_code: str,
        *,
        on_success: Callable[[int], None],
        on_failure: Callable[[str], None],
    ) -> None:
        """Store the vault once its recovery code is confirmed; it starts unlocked.

        Works for a new setup and for the legacy upgrade alike. Succeeds with
        the number of hidden books queued for encryption.
        """

        service = self._require_hidden_space_service()
        if service is None:
            on_failure(locale_service.t("hidden_space_error.unavailable"))
            return
        upgrading = service.state is HiddenSpaceState.LEGACY

        def finish() -> int:
            if upgrading:
                queued = service.complete_upgrade(draft, typed_code)
            else:
                queued = service.create_vault(draft, typed_code)
            service.set_show_hidden_collection(True)
            return queued

        self._run_hidden_space_task("hidden-space-create", finish, on_success, on_failure)

    def unlock_hidden_space(
        self,
        passphrase: str,
        *,
        on_success: Callable[[None], None],
        on_failure: Callable[[str], None],
    ) -> None:
        """Unlock and show Hidden Space. Waiting decryptions and failed seals resume."""

        service = self._require_hidden_space_service()
        if service is None:
            on_failure(locale_service.t("hidden_space_error.unavailable"))
            return

        def unlock() -> None:
            service.unlock(passphrase)
            service.set_show_hidden_collection(True)

        self._run_hidden_space_task(
            "hidden-space-unlock",
            unlock,
            on_success,
            on_failure,
            unlock_failed_key="hidden_space_error.incorrect_passphrase",
        )

    def recover_hidden_space(
        self,
        typed_code: str,
        new_passphrase: str,
        confirm: str,
        *,
        on_success: Callable[[None], None],
        on_failure: Callable[[str], None],
    ) -> None:
        """Unlock with the recovery code and set a new passphrase."""

        service = self._require_hidden_space_service()
        if service is None:
            on_failure(locale_service.t("hidden_space_error.unavailable"))
            return

        def recover() -> None:
            service.recover(typed_code, new_passphrase, confirm)
            service.set_show_hidden_collection(True)

        self._run_hidden_space_task(
            "hidden-space-recover",
            recover,
            on_success,
            on_failure,
            unlock_failed_key="hidden_space_error.incorrect_recovery_code",
        )

    def change_hidden_space_passphrase(
        self,
        current: str,
        new_passphrase: str,
        confirm: str,
        *,
        on_success: Callable[[None], None],
        on_failure: Callable[[str], None],
    ) -> None:
        service = self._require_hidden_space_service()
        if service is None:
            on_failure(locale_service.t("hidden_space_error.unavailable"))
            return
        self._run_hidden_space_task(
            "hidden-space-change-passphrase",
            lambda: service.change_passphrase(current, new_passphrase, confirm),
            on_success,
            on_failure,
            changes_state=False,
            unlock_failed_key="hidden_space_error.incorrect_current_passphrase",
        )

    def prepare_hidden_space_recovery_code(
        self,
        passphrase: str,
        *,
        on_success: Callable[[RecoveryCodeDraft], None],
        on_failure: Callable[[str], None],
    ) -> None:
        """Make a replacement recovery code to confirm; the old one works until then."""

        service = self._require_hidden_space_service()
        if service is None:
            on_failure(locale_service.t("hidden_space_error.unavailable"))
            return
        self._run_hidden_space_task(
            "hidden-space-prepare-recovery-code",
            lambda: service.prepare_recovery_code(passphrase),
            on_success,
            on_failure,
            changes_state=False,
            unlock_failed_key="hidden_space_error.incorrect_passphrase",
        )

    def replace_hidden_space_recovery_code(
        self,
        draft: RecoveryCodeDraft,
        typed_code: str,
        *,
        on_success: Callable[[None], None],
        on_failure: Callable[[str], None],
    ) -> None:
        service = self._require_hidden_space_service()
        if service is None:
            on_failure(locale_service.t("hidden_space_error.unavailable"))
            return
        self._run_hidden_space_task(
            "hidden-space-replace-recovery-code",
            lambda: service.replace_recovery_code(draft, typed_code),
            on_success,
            on_failure,
            changes_state=False,
        )

    # -- Hidden Space: showing, locking, reverting, erasing ----------------------------

    def set_show_hidden_collection(self, enabled: bool) -> None:
        """Persist Show Collections. Turning it off also locks the vault.

        Turning it on alone never shows anything: the caller unlocks first,
        and the unlock commands persist the preference themselves.
        """

        service = self._require_hidden_space_service()
        if service is None:
            return
        service.set_show_hidden_collection(bool(enabled))
        if not enabled:
            # Locking runs the keyring's listeners on this thread (dropping
            # decrypted covers and pages) and is quick.
            service.lock()
        self._refresh_hidden_space_state()

    def lock_hidden_space(self, *, notify: bool = True) -> None:
        """Lock without touching the preference: the next launch asks again.

        ``notify=False`` for a closing window, which has nothing left to
        refresh; the keyring's own listeners still run either way.
        """

        service = self._hidden_space_service
        if service is not None:
            service.lock()
        if notify:
            self._refresh_hidden_space_state()

    def revert_hidden_space(
        self,
        *,
        on_success: Callable[[int], None],
        on_failure: Callable[[str], None],
        legacy_password: str | None = None,
    ) -> None:
        """Unhide every book and make every hidable collection a normal one.

        With a vault this needs it unlocked: each book is decrypted in the
        background and leaves Hidden when it is. Succeeds with the number of
        books still decrypting. ``legacy_password`` is checked first for a
        Library still on the earlier password.
        """

        service = self._require_hidden_space_service()
        if service is None:
            on_failure(locale_service.t("hidden_space_error.unavailable"))
            return

        def revert() -> int:
            state = service.state
            if state is HiddenSpaceState.LEGACY:
                if legacy_password is None or not service.verify(legacy_password):
                    raise VaultError("Incorrect password.", code="incorrect_current")
            elif state is HiddenSpaceState.LOCKED:
                # Decrypting needs the key; the caller unlocks first.
                raise VaultLockedError("Hidden Space is locked.")
            outcomes = service.revert_all()
            return sum(1 for outcome in outcomes.values() if outcome in _STILL_DECRYPTING)

        self._run_hidden_space_task("hidden-space-revert", revert, on_success, on_failure)

    def reset_hidden_space(
        self,
        *,
        on_success: Callable[[None], None],
        on_failure: Callable[[str], None],
    ) -> None:
        """Delete every hidden book and the vault. Works in every state."""

        service = self._require_hidden_space_service()
        if service is None:
            on_failure(locale_service.t("hidden_space_error.unavailable"))
            return
        self._run_hidden_space_task("hidden-space-reset", service.reset_and_erase, on_success, on_failure)

    def retry_hidden_space_failures(self) -> int:
        """Try every failed encryption or decryption again now."""

        service = self._hidden_space_service
        sealing = service.sealing if service is not None and service.has_vault else None
        if sealing is None:
            return 0
        retried = sealing.retry_failed()
        self.refresh_hidden_space_status()
        return retried

    # -- Hidden Space: plumbing ----------------------------------------------------------

    def _run_hidden_space_task(
        self,
        name: str,
        work: Callable[[], _T],
        on_success: Callable[[_T], None],
        on_failure: Callable[[str], None],
        *,
        changes_state: bool = True,
        unlock_failed_key: str | None = None,
    ) -> None:
        def succeeded(result: _T) -> None:
            if changes_state:
                self._refresh_hidden_space_state()
            on_success(result)

        def failed(error: Exception) -> None:
            if not isinstance(error, VaultError):
                # Expected failures are the user's to fix and say nothing
                # secret; anything else is a defect worth its traceback.
                logger.error(
                    "Hidden Space task failed name=%s", name,
                    exc_info=(type(error), error, error.__traceback__),
                )
            if changes_state:
                self._refresh_hidden_space_state()
            on_failure(hidden_space_error_text(error, unlock_failed_key=unlock_failed_key))

        executor = self._task_service
        if executor is None:
            try:
                result = work()
            except Exception as error:
                failed(error)
                return
            succeeded(result)
            return
        executor.submit(name, work, on_success=succeeded, on_failure=failed, priority=TaskPriority.HIGH)

    def _refresh_hidden_space_state(self) -> None:
        if self._settings_store is not None:
            self.show_hidden_collection = bool(self._settings_store.load().show_hidden_collection)
        self.refresh_hidden_space_status()
        self.state_changed.emit()
        self.hidden_space_changed.emit()

    def _require_hidden_space_service(self) -> HiddenSpaceService | None:
        if self._hidden_space_service is None:
            logger.warning("HiddenSpaceService not wired to SettingsViewModel; ignoring request")
            return None
        return self._hidden_space_service

    def set_purge_encrypted_cache_on_close(self, enabled: bool) -> None:
        """Whether an encrypted archive's extracted pages survive its reader.

        Takes effect on the next open: the pool is told at open time, so a
        document already on screen keeps whatever it was opened under rather
        than changing its mind about its own bytes half way through.
        """

        value = bool(enabled)
        if value == self.purge_encrypted_cache_on_close:
            return
        self.purge_encrypted_cache_on_close = value
        self._persist(purge_encrypted_cache_on_close=value)
        self.state_changed.emit()

    def set_page_prefetch_before(self, value: int) -> None:
        self._set_prefetch_count("page_prefetch_before", value, PREFETCH_BEFORE_DEFAULT, PREFETCH_BEFORE_MAX)

    def set_page_prefetch_after(self, value: int) -> None:
        self._set_prefetch_count("page_prefetch_after", value, PREFETCH_AFTER_DEFAULT, PREFETCH_AFTER_MAX)

    def sync_page_prefetch(self, before: int, after: int) -> None:
        """Reflect a Reader panel edit without persisting the same values twice."""

        before = prefetch_count(before, default=PREFETCH_BEFORE_DEFAULT, maximum=PREFETCH_BEFORE_MAX)
        after = prefetch_count(after, default=PREFETCH_AFTER_DEFAULT, maximum=PREFETCH_AFTER_MAX)
        if (before, after) == (self.page_prefetch_before, self.page_prefetch_after):
            return
        self.page_prefetch_before = before
        self.page_prefetch_after = after
        self.prefetch_window_changed.emit()

    def _set_prefetch_count(self, field: str, value: int, default: int, maximum: int) -> None:
        value = prefetch_count(value, default=default, maximum=maximum)
        if value == getattr(self, field):
            return
        self._persist(**{field: value})
        setattr(self, field, value)
        self.prefetch_window_changed.emit()

    def window_size(self, kind: str) -> tuple[int, int] | None:
        return self._window_sizes[kind]

    def remember_window_size(self, kind: str, size: tuple[int, int]) -> None:
        if self._window_sizes[kind] == size:
            return
        self._persist(**{f"{kind}_window_size": size})
        self._window_sizes[kind] = size

    def reset_window_sizes(self) -> None:
        self._persist(library_window_size=None, reader_window_size=None)
        self._window_sizes = {"library": None, "reader": None}
        self.window_sizes_reset.emit()

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
