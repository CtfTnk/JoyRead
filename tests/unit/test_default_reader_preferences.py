"""Global reader defaults seed only books without their own preferences."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from joyread.core.reader.models import (
    ReaderDirection, ReaderFitMode, ReaderSettings, ReaderTransitionMode,
)
from joyread.infrastructure.config.settings_store import SettingsStore
from joyread.infrastructure.i18n import locale_service
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.viewmodels.settings_viewmodel import SettingsSectionKey, SettingsViewModel
from joyread.ui.views.reader_shell import _reader_settings_for_book
from joyread.ui.widgets.settings_page import SettingsNumericItem, SettingsPageWidget, SettingsSwitchItem


def _store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(support_root=tmp_path / "support", default_storage_root=tmp_path / "library")


def test_default_reader_preferences_persist_and_validate_old_or_damaged_json(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.load().default_reader_settings == ReaderSettings()

    viewmodel = SettingsViewModel(store.load(), store)
    viewmodel.set_default_reader_preference("direction", ReaderDirection.LEFT_TO_RIGHT)
    viewmodel.set_default_reader_preference("transition_mode", ReaderTransitionMode.SLIDE)
    viewmodel.set_default_reader_preference("fit_mode", ReaderFitMode.FIT_WIDTH)
    viewmodel.set_default_reader_preference("custom_enabled", True)
    viewmodel.set_default_reader_preference("vertical_custom_enabled", True)
    viewmodel.set_default_reader_preference("page_spacing", 35)
    viewmodel.set_default_reader_preference("vertical_zoom_percent", 140)

    expected = viewmodel.default_reader_settings
    assert store.load().default_reader_settings == expected
    assert SettingsViewModel(store.load(), store).default_reader_settings == expected

    # Regular app-setting edits preserve the nested reader model in memory.
    store.update(language="English")
    assert store.load().default_reader_settings == expected

    raw = json.loads(store.settings_path.read_text(encoding="utf-8"))
    raw["default_reader_settings"] = {
        "direction": "invalid", "transition_mode": "slide", "page_spacing": 999,
        "vertical_zoom_percent": 0,
    }
    store.settings_path.write_text(json.dumps(raw), encoding="utf-8")
    recovered = store.load().default_reader_settings
    assert recovered.direction == ReaderDirection.RIGHT_TO_LEFT
    assert recovered.transition_mode == ReaderTransitionMode.SLIDE
    assert recovered.page_spacing == 200
    assert recovered.vertical_zoom_percent == 25


def test_reader_defaults_apply_to_temporary_and_new_books_but_not_saved_books() -> None:
    defaults = ReaderSettings(direction=ReaderDirection.TOP_TO_BOTTOM, transition_mode=ReaderTransitionMode.SLIDE)
    saved = ReaderSettings(direction=ReaderDirection.LEFT_TO_RIGHT)

    class Library:
        def __init__(self) -> None:
            self.settings = None

        def get_reader_settings(self, _uuid: str) -> ReaderSettings | None:
            return self.settings

    library = Library()
    book = SimpleNamespace(uuid="book-1")
    assert _reader_settings_for_book(library, None, defaults) == defaults
    assert _reader_settings_for_book(library, book, defaults) == defaults
    library.settings = saved
    assert _reader_settings_for_book(library, book, defaults) == saved


def test_reading_section_controls_persist_and_retranslate(qtbot, tmp_path: Path) -> None:
    locale_service.load_language("English")
    store = _store(tmp_path)
    viewmodel = SettingsViewModel(store.load(), store)
    viewmodel.set_section(SettingsSectionKey.READING)
    page = SettingsPageWidget(viewmodel, ResourceLoader())
    qtbot.addWidget(page)

    direction = page._reading_dropdowns["direction"]
    assert direction.value == "Right-to-left"
    direction.set_value("Top-to-down")
    assert store.load().default_reader_settings.direction == ReaderDirection.TOP_TO_BOTTOM

    horizontal_custom, one_page, vertical_custom, fit_width = page.findChildren(SettingsSwitchItem)
    spacing, zoom = page.findChildren(SettingsNumericItem)
    assert not one_page.isEnabled()
    assert not spacing.isEnabled()
    horizontal_custom.switch.set_checked(True)
    assert one_page.isEnabled()
    assert viewmodel.default_reader_settings.custom_enabled
    vertical_custom.switch.set_checked(True)
    assert fit_width.isEnabled() and spacing.isEnabled() and zoom.isEnabled()
    spacing.spin_button.set_value(18)
    assert store.load().default_reader_settings.page_spacing == 18
    fit_width.switch.set_checked(True)
    assert not zoom.isEnabled()

    locale_service.load_language("Chinese")
    page.refresh_labels()
    assert direction.value == "从上到下"
    locale_service.load_language("English")
