"""Centralized access to bundled read-only resources."""

from __future__ import annotations

import logging
from pathlib import Path
import sys

from joyread.ui.resources.styles.theme import Theme


logger = logging.getLogger(__name__)

# One artwork, three containers, because each desktop reads a different one.
# Windows wants a multi-size .ico for the taskbar and window chrome; macOS
# wants .icns for the bundle; Linux has no native container, so a large PNG is
# what both Qt and the freedesktop icon spec consume.
#
# This is not cosmetic. `QIcon` does not cache across construction, and the
# 3.76 MB .icns costs 53-69 ms to decode against 2-3 ms for the 68 KB .ico --
# paid on every launch, on a platform that cannot display the result.
_APP_ICON_NAMES = {"win32": "JoyRead.ico", "darwin": "JoyRead.icns"}
_APP_ICON_FALLBACK = "JoyRead.png"


class ResourceLoader:
    """Centralizes read-only package resources for packaged app builds.

    Views ask this object for icons, fonts, and QSS instead of constructing
    relative paths. That keeps PyInstaller/app-bundle changes local to one
    infrastructure class.
    """

    def __init__(self, package_root: Path | None = None) -> None:
        self._package_root = package_root or Path(__file__).resolve().parents[2]
        logger.debug("ResourceLoader package_root=%s", self._package_root)

    @property
    def package_root(self) -> Path:
        return self._package_root

    def style_path(self, name: str = "main.qss") -> Path:
        return self._package_root / "ui" / "resources" / "styles" / name

    def icon_path(self, name: str) -> Path:
        return self._package_root / "ui" / "resources" / "icons" / name

    def font_path(self, name: str) -> Path:
        return self._package_root / "ui" / "resources" / "fonts" / name

    def font_paths(self) -> tuple[Path, ...]:
        return tuple(self.font_path(name) for name in Theme.font_files)

    def app_icon_path(self) -> Path:
        """Return this platform's application icon.

        Falls back to another bundled format rather than returning a path that
        does not exist: a missing file yields a silently empty ``QIcon``, which
        looks like a design choice instead of a packaging bug. The warning is
        what makes it a bug report.
        """

        preferred = _APP_ICON_NAMES.get(sys.platform, _APP_ICON_FALLBACK)
        path = self.icon_path(preferred)
        if path.is_file():
            return path
        for name in (_APP_ICON_FALLBACK, *_APP_ICON_NAMES.values()):
            candidate = self.icon_path(name)
            if candidate.is_file():
                logger.warning(
                    "Application icon %s is missing for platform %s; using %s",
                    preferred,
                    sys.platform,
                    name,
                )
                return candidate
        logger.warning("No application icon resource is bundled: %s", path)
        return path

    def locale_dir(self) -> Path:
        """Return the directory containing bundled locale JSON files."""
        return self._package_root / "resources" / "locales"

    def load_stylesheet(self, name: str = "main.qss") -> str:
        path = self.style_path(name)
        if not path.exists():
            logger.warning("Stylesheet resource missing: %s", path)
            return ""
        stylesheet = path.read_text(encoding="utf-8")
        stylesheet = stylesheet.replace("__ICON_DIR__", self.icon_path("").as_posix())
        for token, value in Theme.qss_tokens().items():
            stylesheet = stylesheet.replace(token, value)
        logger.debug("Stylesheet loaded: %s bytes=%d", path, len(stylesheet))
        return stylesheet
