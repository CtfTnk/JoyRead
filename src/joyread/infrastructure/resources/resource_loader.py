"""Centralized access to bundled read-only resources."""

from __future__ import annotations

import logging
from pathlib import Path

from joyread.ui.resources.styles.theme import Theme


logger = logging.getLogger(__name__)


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
        return self.icon_path("JoyRead.icns")

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
