"""Settings page state for the first Figma-backed settings panel."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from joyread.ui.viewmodels.signals import Signal


class SettingsSectionKey(StrEnum):
    GENERAL = "general"
    TAGS = "tags"
    PRIVATE_SPACE = "private_space"
    ABOUT = "about"


@dataclass(frozen=True)
class SettingsSection:
    key: SettingsSectionKey
    label: str
    lower_group: bool = False


class SettingsViewModel:
    def __init__(self) -> None:
        self.state_changed: Signal[None] = Signal()
        self.sections = (
            SettingsSection(SettingsSectionKey.GENERAL, "General"),
            SettingsSection(SettingsSectionKey.TAGS, "Tags"),
            SettingsSection(SettingsSectionKey.PRIVATE_SPACE, "Private Space"),
            SettingsSection(SettingsSectionKey.ABOUT, "About", lower_group=True),
        )
        self.current_section = SettingsSectionKey.GENERAL
        self.language = "English"
        self.import_book_when_opening = False
        self.individual_read_window = False
        self.storage_location = "~/Documents/JoyRead"

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
        self.state_changed.emit()

    def set_import_book_when_opening(self, enabled: bool) -> None:
        if enabled == self.import_book_when_opening:
            return
        self.import_book_when_opening = enabled
        self.state_changed.emit()

    def set_individual_read_window(self, enabled: bool) -> None:
        if enabled == self.individual_read_window:
            return
        self.individual_read_window = enabled
        self.state_changed.emit()

    def set_storage_location(self, path: str) -> None:
        if path == self.storage_location:
            return
        self.storage_location = path
        self.state_changed.emit()
