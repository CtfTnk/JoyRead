"""Runtime path management for packaged and development builds."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

try:
    from platformdirs import user_cache_path, user_config_path, user_data_path, user_log_path
except ImportError:  # pragma: no cover - exercised implicitly when dependency is absent.
    user_cache_path = user_config_path = user_data_path = user_log_path = None


class WritableLocation(StrEnum):
    BOOKS = "Books"
    DATABASE = "Database"
    THUMBNAILS = "Thumbnails"
    CACHE = "Cache"
    LOGS = "Logs"
    PLUGINS = "Plugins"
    CONFIG = "Config"
    BACKUPS = "Backups"


@dataclass(frozen=True)
class AppPaths:
    books: Path
    database: Path
    thumbnails: Path
    cache: Path
    logs: Path
    plugins: Path
    config: Path
    backups: Path

    def as_dict(self) -> dict[WritableLocation, Path]:
        return {
            WritableLocation.BOOKS: self.books,
            WritableLocation.DATABASE: self.database,
            WritableLocation.THUMBNAILS: self.thumbnails,
            WritableLocation.CACHE: self.cache,
            WritableLocation.LOGS: self.logs,
            WritableLocation.PLUGINS: self.plugins,
            WritableLocation.CONFIG: self.config,
            WritableLocation.BACKUPS: self.backups,
        }


class PathService:
    """Owns all user-writable locations.

    Passing ``base_dir`` is intended for tests and portable/development profiles.
    Production defaults use platform-specific user directories and never resolve
    under the application source tree.
    """

    def __init__(
        self,
        app_name: str = "JoyRead",
        app_author: str = "JoyRead",
        base_dir: Path | None = None,
        storage_root: Path | None = None,
        support_root: Path | None = None,
    ) -> None:
        self._app_name = app_name
        self._app_author = app_author
        self._paths = self._build_paths(base_dir, storage_root, support_root)

    @property
    def paths(self) -> AppPaths:
        return self._paths

    def get_path(self, location: WritableLocation) -> Path:
        return self._paths.as_dict()[location]

    def required_directories(self) -> tuple[Path, ...]:
        return tuple(self._paths.as_dict().values())

    def ensure_directories(self) -> None:
        for directory in self.required_directories():
            directory.mkdir(parents=True, exist_ok=True)

    def _build_paths(
        self,
        base_dir: Path | None,
        storage_root: Path | None,
        support_root: Path | None,
    ) -> AppPaths:
        if storage_root is not None:
            data_root = storage_root.expanduser().resolve()
            support = support_root.expanduser().resolve() if support_root is not None else data_root
            cache_root = data_root / "Cache"
            thumbnails_root = data_root / "Thumbnails"
            config_root = support / "Config"
            logs_root = support / "Logs"
        elif base_dir is not None:
            root = base_dir.expanduser().resolve()
            data_root = root / "Data"
            cache_root = root / "Cache"
            thumbnails_root = cache_root / "Thumbnails"
            config_root = root / "Config"
            logs_root = root / "Logs"
        else:
            data_root = self._platform_path("data")
            cache_root = self._platform_path("cache")
            thumbnails_root = cache_root / "Thumbnails"
            config_root = self._platform_path("config")
            logs_root = self._platform_path("logs")

        return AppPaths(
            books=data_root / "Books",
            database=data_root / "Database",
            thumbnails=thumbnails_root,
            cache=cache_root,
            logs=logs_root,
            plugins=data_root / "Plugins",
            config=config_root,
            backups=data_root / "Backups",
        )

    def _platform_path(self, kind: str) -> Path:
        fallback_root = Path.home() / ".local" / "share" / self._app_name
        if kind == "data":
            if user_data_path is not None:
                return Path(user_data_path(self._app_name, self._app_author, roaming=True))
            return fallback_root
        if kind == "cache":
            if user_cache_path is not None:
                return Path(user_cache_path(self._app_name, self._app_author))
            return Path.home() / ".cache" / self._app_name
        if kind == "config":
            if user_config_path is not None:
                return Path(user_config_path(self._app_name, self._app_author, roaming=True))
            return Path.home() / ".config" / self._app_name
        if user_log_path is not None:
            return Path(user_log_path(self._app_name, self._app_author))
        return fallback_root / "Logs"
