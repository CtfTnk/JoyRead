"""Windows MAX_PATH capability detection and error classification."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import errno
import os
from pathlib import Path
from threading import RLock

from joyread.core.models.path_issue import PathIssue, PathIssueKind


WINDOWS_MAX_PATH = 260
WINDOWS_CREATE_DIRECTORY_LIMIT = WINDOWS_MAX_PATH - 12
ERROR_FILENAME_EXCED_RANGE = 206


RegistryReader = Callable[[], bool | None]


class WindowsLongPathCapability:
    """Describe whether an absolute path needs unavailable Windows support.

    PyInstaller's Windows executable manifest is long-path aware. Windows still
    requires the machine policy to be enabled, so this adapter reads that one
    policy value and caches it for the process lifetime, matching Windows' own
    per-process caching behavior.
    """

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        registry_reader: RegistryReader | None = None,
    ) -> None:
        self._platform_name = platform_name or os.name
        self._registry_reader = registry_reader or _read_windows_long_paths_enabled
        self._enabled: bool | None = None
        self._status_loaded = False
        self._lock = RLock()

    @property
    def enabled(self) -> bool | None:
        if self._platform_name != "nt":
            return None
        with self._lock:
            if not self._status_loaded:
                self._enabled = self._registry_reader()
                self._status_loaded = True
            return self._enabled

    def inspect_path(self, path: Path, *, operation: str) -> PathIssue | None:
        if self._platform_name != "nt":
            return None
        length = _absolute_path_length(path)
        if length < WINDOWS_MAX_PATH:
            return None
        if self.enabled is False:
            return PathIssue(PathIssueKind.WINDOWS_LONG_PATHS_DISABLED, operation, length)
        return None

    def inspect_directory(self, path: Path, *, operation: str) -> PathIssue | None:
        """Preflight a directory that the caller is about to create.

        Classic Win32 directory creation reserves twelve characters for an 8.3
        child name. That makes its no-opt-in ceiling stricter than file access.
        Existing directories continue through the ordinary 260-character path
        check; this method is only for ``mkdir`` boundaries.
        """

        if self._platform_name != "nt":
            return None
        length = _absolute_path_length(path)
        if length < WINDOWS_CREATE_DIRECTORY_LIMIT:
            return None
        if self.enabled is False:
            return PathIssue(PathIssueKind.WINDOWS_LONG_PATHS_DISABLED, operation, length)
        return None

    def inspect_error(
        self,
        error: BaseException,
        paths: Iterable[Path],
        *,
        operation: str,
    ) -> PathIssue | None:
        if self._platform_name != "nt" or not _is_path_too_long_error(error):
            return None
        length = max((_absolute_path_length(path) for path in paths), default=0)
        kind = (
            PathIssueKind.WINDOWS_LONG_PATHS_DISABLED
            if self.enabled is False
            else PathIssueKind.PATH_TOO_LONG_UNSUPPORTED
        )
        return PathIssue(kind, operation, length)


def _absolute_path_length(path: Path) -> int:
    """Measure the absolute spelling passed to Win32 without touching disk."""

    return len(os.path.abspath(os.fspath(path)))


def _is_path_too_long_error(error: BaseException) -> bool:
    current: BaseException | None = error
    visited: set[int] = set()
    while isinstance(current, BaseException) and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, OSError):
            if getattr(current, "winerror", None) == ERROR_FILENAME_EXCED_RANGE:
                return True
            if current.errno == errno.ENAMETOOLONG:
                return True
        current = current.__cause__ or current.__context__
    return False


def _read_windows_long_paths_enabled() -> bool | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem",
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "LongPathsEnabled")
        return int(value) == 1
    except (OSError, TypeError, ValueError):
        # An unreadable policy is not evidence that the user disabled it. We
        # still recognize WinError 206, but present the non-prescriptive form.
        return None
