"""Centralized access to bundled read-only resources."""

from __future__ import annotations

from pathlib import Path

from joyread.ui.resources.styles.theme import Theme


class ResourceLoader:
    def __init__(self, package_root: Path | None = None) -> None:
        self._package_root = package_root or Path(__file__).resolve().parents[2]

    @property
    def package_root(self) -> Path:
        return self._package_root

    def style_path(self, name: str = "main.qss") -> Path:
        return self._package_root / "ui" / "resources" / "styles" / name

    def icon_path(self, name: str) -> Path:
        return self._package_root / "ui" / "resources" / "icons" / name

    def app_icon_path(self) -> Path:
        return self.icon_path("JoyRead.icns")

    def load_stylesheet(self, name: str = "main.qss") -> str:
        path = self.style_path(name)
        if not path.exists():
            return ""
        stylesheet = path.read_text(encoding="utf-8")
        stylesheet = stylesheet.replace("__ICON_DIR__", self.icon_path("").as_posix())
        for token, value in Theme.qss_tokens().items():
            stylesheet = stylesheet.replace(token, value)
        return stylesheet
