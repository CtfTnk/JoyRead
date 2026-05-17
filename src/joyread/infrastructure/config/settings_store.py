"""Persistent app settings stored outside the movable library data root."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

from joyread.core.models.cache import ArchiveCacheStrategy, normalize_archive_cache_strategy

try:
    from platformdirs import user_config_path, user_data_path
except ImportError:  # pragma: no cover - platformdirs is a project dependency.
    user_config_path = user_data_path = None


@dataclass(frozen=True)
class AppSettings:
    storage_location: str
    hash_algorithm: str = "sha256"
    language: str = "English"
    import_book_when_opening: bool = False
    individual_read_window: bool = False
    shelf_sort_field: str = "Add Time"
    shelf_sort_ascending: bool = False
    shelf_file_filter: str = "ALL"
    shelf_view_mode: str = "grid"
    # Cache budgets (MB). Defaults intentionally match AppConfig so a missing
    # settings.json or an upgrade from an older build behaves identically to a
    # fresh install. Reader cache is the *total* shared budget across every
    # open reader window — see CacheService.reader_page_cache.
    reader_page_cache_mb: int = 512
    detail_thumbnail_cache_mb: int = 64
    archive_extraction_pool_mb: int = 1024
    archive_cache_strategy: str = ArchiveCacheStrategy.ZIP_BUNDLE.value
    import_folder_max_depth: int = 1
    archive_internal_max_depth: int = 2
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
    def config_dir(self) -> Path:
        return self._support_root / "Config"

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
            self.save(settings)
            return settings

        raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
        return AppSettings(
            storage_location=str(raw.get("storage_location") or self._default_storage_root),
            hash_algorithm=str(raw.get("hash_algorithm") or "sha256"),
            language=str(raw.get("language") or "English"),
            import_book_when_opening=bool(raw.get("import_book_when_opening", False)),
            individual_read_window=bool(raw.get("individual_read_window", False)),
            shelf_sort_field=str(raw.get("shelf_sort_field") or "Add Time"),
            shelf_sort_ascending=bool(raw.get("shelf_sort_ascending", False)),
            shelf_file_filter=str(raw.get("shelf_file_filter") or "ALL"),
            shelf_view_mode=str(raw.get("shelf_view_mode") or "grid"),
            reader_page_cache_mb=_coerce_positive_int(raw.get("reader_page_cache_mb"), default=512),
            detail_thumbnail_cache_mb=_coerce_positive_int(raw.get("detail_thumbnail_cache_mb"), default=64),
            archive_extraction_pool_mb=_coerce_positive_int(raw.get("archive_extraction_pool_mb"), default=1024),
            archive_cache_strategy=normalize_archive_cache_strategy(raw.get("archive_cache_strategy")).value,
            import_folder_max_depth=_coerce_int_in_range(
                raw.get("import_folder_max_depth"),
                default=1,
                minimum=1,
                maximum=5,
            ),
            archive_internal_max_depth=_coerce_int_in_range(
                raw.get("archive_internal_max_depth"),
                default=2,
                minimum=1,
                maximum=5,
            ),
            hidden_space_password_hash=_coerce_optional_str(raw.get("hidden_space_password_hash")),
            hidden_space_password_salt=_coerce_optional_str(raw.get("hidden_space_password_salt")),
            hidden_space_password_hint=_coerce_optional_str(raw.get("hidden_space_password_hint")),
            show_hidden_collection=bool(raw.get("show_hidden_collection", False)),
        )

    def save(self, settings: AppSettings) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(
            json.dumps(asdict(settings), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def update(self, **changes: Any) -> AppSettings:
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
            return Path.cwd() / ".joyread_storage"
        if user_data_path is not None:
            return Path(user_data_path(self._app_name, self._app_author, roaming=True))
        return Path.home() / ".local" / "share" / self._app_name


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
