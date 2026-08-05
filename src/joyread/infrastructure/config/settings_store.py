"""Persistent app settings stored outside the movable library data root."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
from math import ceil
from os import environ
from pathlib import Path
from typing import Any

from joyread.core.models.cache import ArchiveCacheStrategy, normalize_archive_cache_strategy
from joyread.infrastructure.config.storage_names import LIBRARY_DIRECTORY_NAME

try:
    from platformdirs import user_config_path, user_data_path
except ImportError:  # pragma: no cover - platformdirs is a project dependency.
    user_config_path = user_data_path = None


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppSettings:
    storage_location: str
    # Last storage root that passed a startup health check. Recovery falls back
    # to this when ``storage_location`` becomes unavailable. ``None`` until the
    # first successful startup writes it.
    last_good_storage_location: str | None = None
    hash_algorithm: str = "sha256"
    language: str = "English"
    import_book_when_opening: bool = False
    # When enabled, the source is hashed before copy and the staging copy must
    # produce the same digest. Disabling it avoids a separate source pass while
    # retaining a content hash calculated during the required copy pass.
    verify_imported_file_integrity: bool = True
    individual_read_window: bool = False
    shelf_sort_field: str = "Add Time"
    shelf_sort_ascending: bool = False
    shelf_file_filter: str = "ALL"
    shelf_view_mode: str = "grid"
    # In-memory cache budgets use MB; the archive disk pool uses GB. Defaults
    # intentionally match AppConfig so missing settings remain upgrade-safe.
    # Reader cache is the total shared budget across all open reader windows.
    reader_page_cache_mb: int = 512
    thumbnail_cache_mb: int = 64
    archive_extraction_pool_gb: int = 5
    archive_cache_strategy: str = ArchiveCacheStrategy.ZIP_BUNDLE.value
    import_folder_max_depth: int = 1
    nested_archive_max_depth: int = 2
    archive_global_file_max_depth: int = 100
    # Top-level archive size is deliberately independent from resource
    # guardrails: users can accept large source files while still keeping
    # extraction, image, and subprocess budgets enabled.
    archive_max_source_size_enabled: bool = True
    archive_max_source_size_gb: int = 5
    archive_resource_guardrails_enabled: bool = True
    archive_max_extracted_item_gb: int = 1
    archive_max_operation_data_gb: int = 4
    archive_max_image_megapixels: int = 400
    archive_external_command_timeout_seconds: int = 300
    # Hidden Space (soft visibility layer for books + user collections).
    # ``hidden_space_password_hash is None`` is the sentinel for the
    # uninitiated state — the feature is only "armed" once the user
    # completes the password-setup dialog.
    hidden_space_password_hash: str | None = None
    hidden_space_password_salt: str | None = None
    hidden_space_password_hint: str | None = None
    show_hidden_collection: bool = False

class SettingsStore:
    """Loads the bootstrap settings needed before PathService exists.

    The storage location cannot live in SQLite because SQLite itself is inside
    the movable storage root. Keep this config in a stable support directory.
    """

    _FILENAME = "settings.json"

    def __init__(
        self,
        app_name: str = "JoyRead",
        app_author: str = "JoyRead",
        support_root: Path | None = None,
        default_storage_root: Path | None = None,
    ) -> None:
        self._app_name = app_name
        self._app_author = app_author
        self._support_root = (support_root or self._default_support_root()).expanduser().resolve()
        self._default_storage_root = (
            default_storage_root or self._default_storage_root_for_environment()
        ).expanduser().resolve()

    @property
    def support_root(self) -> Path:
        return self._support_root

    @property
    def default_storage_root(self) -> Path:
        """The app's own default JoyRead library root (recovery's last resort)."""

        return self._default_storage_root

    @property
    def config_dir(self) -> Path:
        return self._support_root / "Config"

    @property
    def locales_dir(self) -> Path:
        """User-supplied locale override directory (Config/locales)."""
        return self.config_dir / "locales"

    @property
    def settings_path(self) -> Path:
        return self.config_dir / self._FILENAME

    def load(self) -> AppSettings:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if not self.settings_path.exists():
            # Fresh install: write the defaults to disk so the file is present
            # for out-of-app edits and so later versions can ALTER the
            # on-disk shape against a stable JSON document rather than a
            # virtual one. The next `load` short-circuits past this branch.
            settings = self.default_settings()
            logger.info("Creating default settings file at %s", self.settings_path)
            self.save(settings)
            return settings

        logger.debug("Loading settings from %s", self.settings_path)
        raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
        settings = AppSettings(
            storage_location=str(raw.get("storage_location") or self._default_storage_root),
            last_good_storage_location=_coerce_optional_str(raw.get("last_good_storage_location")),
            hash_algorithm=str(raw.get("hash_algorithm") or "sha256"),
            language=str(raw.get("language") or "English"),
            import_book_when_opening=bool(raw.get("import_book_when_opening", False)),
            verify_imported_file_integrity=bool(raw.get("verify_imported_file_integrity", True)),
            individual_read_window=bool(raw.get("individual_read_window", False)),
            shelf_sort_field=str(raw.get("shelf_sort_field") or "Add Time"),
            shelf_sort_ascending=bool(raw.get("shelf_sort_ascending", False)),
            shelf_file_filter=str(raw.get("shelf_file_filter") or "ALL"),
            shelf_view_mode=str(raw.get("shelf_view_mode") or "grid"),
            reader_page_cache_mb=_coerce_positive_int(raw.get("reader_page_cache_mb"), default=512),
            thumbnail_cache_mb=_coerce_positive_int(
                raw.get("thumbnail_cache_mb", raw.get("detail_thumbnail_cache_mb")),
                default=64,
            ),
            archive_extraction_pool_gb=_coerce_archive_pool_gb(raw),
            archive_cache_strategy=normalize_archive_cache_strategy(raw.get("archive_cache_strategy")).value,
            import_folder_max_depth=_coerce_int_in_range(
                raw.get("import_folder_max_depth"),
                default=1,
                minimum=1,
                maximum=5,
            ),
            nested_archive_max_depth=_coerce_depth_limit(
                raw.get("nested_archive_max_depth", raw.get("archive_internal_max_depth")),
                default=2,
                maximum=5,
            ),
            archive_global_file_max_depth=_coerce_depth_limit(
                raw.get("archive_global_file_max_depth"),
                default=100,
                maximum=1000,
            ),
            archive_max_source_size_enabled=bool(raw.get("archive_max_source_size_enabled", True)),
            archive_max_source_size_gb=_coerce_int_in_range(
                raw.get("archive_max_source_size_gb"),
                default=5,
                minimum=1,
                maximum=15,
            ),
            archive_resource_guardrails_enabled=bool(
                raw.get("archive_resource_guardrails_enabled", True)
            ),
            archive_max_extracted_item_gb=_coerce_limit_or_unlimited(
                raw.get("archive_max_extracted_item_gb"),
                default=1,
                maximum=16,
            ),
            archive_max_operation_data_gb=_coerce_limit_or_unlimited(
                raw.get("archive_max_operation_data_gb"),
                default=4,
                maximum=64,
            ),
            archive_max_image_megapixels=_coerce_limit_or_unlimited(
                raw.get("archive_max_image_megapixels"),
                default=400,
                maximum=1000,
            ),
            archive_external_command_timeout_seconds=_coerce_limit_or_unlimited(
                raw.get("archive_external_command_timeout_seconds"),
                default=300,
                maximum=3600,
            ),
            hidden_space_password_hash=_coerce_optional_str(raw.get("hidden_space_password_hash")),
            hidden_space_password_salt=_coerce_optional_str(raw.get("hidden_space_password_salt")),
            hidden_space_password_hint=_coerce_optional_str(raw.get("hidden_space_password_hint")),
            show_hidden_collection=bool(raw.get("show_hidden_collection", False)),
        )
        logger.debug(
            "Settings loaded storage=%s reader_cache_mb=%d archive_pool_gb=%d",
            settings.storage_location,
            settings.reader_page_cache_mb,
            settings.archive_extraction_pool_gb,
        )
        return settings

    def save(self, settings: AppSettings) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        logger.debug("Saving settings to %s", self.settings_path)
        payload = asdict(settings)
        self.settings_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def update(self, **changes: Any) -> AppSettings:
        logger.debug("Updating settings keys=%s", sorted(changes))
        current = self.load()
        next_settings = AppSettings(**{**asdict(current), **changes})
        self.save(next_settings)
        return next_settings

    def default_settings(self) -> AppSettings:
        return AppSettings(storage_location=str(self._default_storage_root))

    def _default_support_root(self) -> Path:
        if _looks_like_source_checkout(Path.cwd()):
            return Path.cwd() / ".joyread_support"
        if user_config_path is not None:
            return Path(user_config_path(self._app_name, self._app_author, roaming=True))
        return Path.home() / ".config" / self._app_name

    def _default_storage_root_for_environment(self) -> Path:
        if _looks_like_source_checkout(Path.cwd()):
            return Path.cwd() / LIBRARY_DIRECTORY_NAME
        if user_data_path is not None:
            app_data_root = Path(user_data_path(self._app_name, self._app_author, roaming=True))
            return app_data_root.parent / LIBRARY_DIRECTORY_NAME
        return Path.home() / ".local" / "share" / LIBRARY_DIRECTORY_NAME


def create_environment_settings_store(
    app_name: str = "JoyRead",
    app_author: str = "JoyRead",
    *,
    runtime_dir: str | Path | None = None,
) -> SettingsStore:
    """Build the stable pre-database settings store for this runtime profile.

    Startup instance arbitration and ``AppContext`` must resolve the same
    support directory. Keeping that decision here prevents a secondary process
    from touching a different lock before both processes reach settings load.
    """

    runtime_override = runtime_dir if runtime_dir is not None else environ.get("JOYREAD_RUNTIME_DIR")
    if runtime_override is None:
        return SettingsStore(app_name, app_author)
    root = Path(runtime_override).expanduser().resolve()
    return SettingsStore(
        app_name,
        app_author,
        support_root=root / ".joyread_support",
        default_storage_root=root / LIBRARY_DIRECTORY_NAME,
    )


def _looks_like_source_checkout(path: Path) -> bool:
    return (path / "pyproject.toml").exists() and (path / "src" / "joyread").exists()


def _coerce_positive_int(value: object, *, default: int) -> int:
    """Parse a cache-budget integer with a graceful fallback.

    Settings files written by older builds will not carry the cache fields;
    we want missing/invalid entries to silently fall back to the documented
    defaults so the app keeps booting after an upgrade.
    """

    if value is None:
        return default
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    return coerced if coerced > 0 else default


def _coerce_archive_pool_gb(raw: dict[str, Any]) -> int:
    """Load the GB budget, migrating the legacy MB value when necessary."""

    if "archive_extraction_pool_gb" in raw:
        return _coerce_int_in_range(
            raw.get("archive_extraction_pool_gb"),
            default=5,
            minimum=1,
            maximum=50,
        )
    legacy_mb = _coerce_positive_int(
        raw.get("archive_extraction_pool_mb"),
        default=5 * 1024,
    )
    return max(1, min(50, ceil(legacy_mb / 1024)))


def _coerce_optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int_in_range(value: object, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, coerced))


def _coerce_depth_limit(value: object, *, default: int, maximum: int) -> int:
    """Read a depth limit whose only valid negative value is ``-1``."""

    if value is None:
        return default
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    if coerced == -1:
        return -1
    if coerced < 1:
        return default
    return min(maximum, coerced)


def _coerce_limit_or_unlimited(value: object, *, default: int, maximum: int) -> int:
    """Parse a positive resource setting whose UI sentinel is ``-1``."""

    return _coerce_depth_limit(value, default=default, maximum=maximum)
