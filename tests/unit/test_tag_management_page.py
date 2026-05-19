"""Headless widget tests for the Settings -> Tags page."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication

from joyread.core.models.tag import Tag
from joyread.core.repositories.tag_repository import TagNameConflictError
from joyread.core.services.tag_service import TagService
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.settings_viewmodel import SettingsSectionKey, SettingsViewModel
from joyread.ui.viewmodels.tag_management_viewmodel import (
    TagInputMode,
    TagManagementViewModel,
)
from joyread.ui.widgets.settings_page import SettingsPageWidget
from joyread.ui.widgets.tag_chip import TagChipWidget
from joyread.ui.widgets.tag_management_page import TagManagementPage


def _apply_theme() -> None:
    app = QApplication.instance()
    assert app is not None
    app.setStyleSheet(ResourceLoader().load_stylesheet())


class _FakeRepository:
    def __init__(self) -> None:
        self._tags: dict[str, Tag] = {}
        self._next = 0

    def list_tags(self) -> list[Tag]:
        return sorted(self._tags.values(), key=lambda t: t.name.casefold())

    def get_tag(self, tag_id: str) -> Tag | None:
        return self._tags.get(tag_id)

    def get_tag_by_normalized(self, normalized: str) -> Tag | None:
        for tag in self._tags.values():
            if tag.name_normalized == normalized.casefold():
                return tag
        return None

    def create(self, display_name: str) -> Tag:
        from joyread.core.models.tag import normalize_tag_name
        name = normalize_tag_name(display_name)
        if self.get_tag_by_normalized(name.casefold()):
            raise TagNameConflictError(f"A tag named '{name}' already exists.")
        self._next += 1
        tag = Tag(tag_id=f"tag-{self._next}", name=name)
        self._tags[tag.tag_id] = tag
        return tag

    def find_or_create(self, display_name: str) -> Tag:
        from joyread.core.models.tag import normalize_tag_name
        name = normalize_tag_name(display_name)
        existing = self.get_tag_by_normalized(name.casefold())
        if existing is not None:
            return existing
        return self.create(display_name)

    def rename(self, tag_id: str, new_display_name: str) -> Tag:
        from joyread.core.models.tag import normalize_tag_name
        name = normalize_tag_name(new_display_name)
        current = self._tags.get(tag_id)
        if current is None:
            from joyread.core.repositories.tag_repository import TagNotFoundError
            raise TagNotFoundError("not found")
        if current.name_normalized != name.casefold() and self.get_tag_by_normalized(name.casefold()):
            raise TagNameConflictError("conflict")
        renamed = Tag(tag_id=tag_id, name=name)
        self._tags[tag_id] = renamed
        return renamed

    def delete(self, tag_id: str) -> int:
        return int(self._tags.pop(tag_id, None) is not None)

    def link_book(self, tag_id: str, book_id: str) -> None:
        pass

    def unlink_book(self, tag_id: str, book_id: str) -> None:
        pass

    def list_tag_ids_for_book(self, book_id: str) -> list[str]:
        return []


def _viewmodel_with_tags(*names: str) -> TagManagementViewModel:
    repo = _FakeRepository()
    service = TagService(repo)
    for name in names:
        repo.create(name)
    vm = TagManagementViewModel(service)
    vm.refresh()
    return vm


def test_tag_management_page_renders_chips_plus_add_chip(qtbot) -> None:
    _apply_theme()
    vm = _viewmodel_with_tags("Comedy", "Action")
    page = TagManagementPage(vm)
    qtbot.addWidget(page)
    page.resize(620, 360)
    page.show()
    QApplication.processEvents()

    chips = page.findChildren(TagChipWidget)
    add_chips = [chip for chip in chips if chip.is_add_chip]
    tag_chips = [chip for chip in chips if not chip.is_add_chip]

    assert len(add_chips) == 1
    assert [chip._name for chip in tag_chips] == ["Action", "Comedy"]


def test_tag_chip_geometry_matches_theme(qtbot) -> None:
    _apply_theme()
    vm = _viewmodel_with_tags("Action")
    page = TagManagementPage(vm)
    qtbot.addWidget(page)
    page.show()
    QApplication.processEvents()

    chip = next(chip for chip in page.findChildren(TagChipWidget) if not chip.is_add_chip)
    assert chip.maximumWidth() == Theme.tag_chip_max_width
    assert chip.minimumWidth() == Theme.tag_chip_min_width
    assert chip.height() == Theme.tag_chip_height


def test_buttons_disabled_with_no_selection(qtbot) -> None:
    _apply_theme()
    vm = _viewmodel_with_tags("Action")
    page = TagManagementPage(vm)
    qtbot.addWidget(page)
    page.show()
    QApplication.processEvents()

    assert page.rename_button.isEnabled() is False
    assert page.delete_button.isEnabled() is False


def test_rename_disabled_when_multiple_selected(qtbot) -> None:
    _apply_theme()
    vm = _viewmodel_with_tags("Action", "Comedy")
    page = TagManagementPage(vm)
    qtbot.addWidget(page)
    page.show()
    QApplication.processEvents()

    first, second = vm.tags
    vm.toggle_select(first.tag_id, additive=False)
    vm.toggle_select(second.tag_id, additive=True)
    QApplication.processEvents()

    assert page.rename_button.isEnabled() is False
    assert page.delete_button.isEnabled() is True


def test_clicking_add_chip_swaps_control_to_input(qtbot) -> None:
    _apply_theme()
    vm = _viewmodel_with_tags("Action")
    page = TagManagementPage(vm)
    qtbot.addWidget(page)
    page.show()
    QApplication.processEvents()

    add_chip = next(chip for chip in page.findChildren(TagChipWidget) if chip.is_add_chip)
    qtbot.mouseClick(add_chip, Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    assert vm.input_mode == TagInputMode.CREATE
    assert page.line_edit.isVisible()


def test_submit_create_via_line_edit_return_press(qtbot) -> None:
    _apply_theme()
    vm = _viewmodel_with_tags()
    page = TagManagementPage(vm)
    qtbot.addWidget(page)
    page.show()
    QApplication.processEvents()

    add_chip = next(chip for chip in page.findChildren(TagChipWidget) if chip.is_add_chip)
    qtbot.mouseClick(add_chip, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    page.line_edit.setText("comedy")
    qtbot.keyClick(page.line_edit, Qt.Key.Key_Return)
    QApplication.processEvents()

    tag_chips = [chip for chip in page.findChildren(TagChipWidget) if not chip.is_add_chip]
    assert [chip._name for chip in tag_chips] == ["Comedy"]
    assert vm.input_mode == TagInputMode.BUTTONS


def test_settings_page_routes_tag_section_through_viewmodel(qtbot) -> None:
    _apply_theme()
    settings_vm = SettingsViewModel()
    tag_vm = _viewmodel_with_tags("Comedy")
    page = SettingsPageWidget(
        settings_vm,
        ResourceLoader(),
        tag_viewmodel=tag_vm,
    )
    qtbot.addWidget(page)
    page.resize(Theme.settings_panel_width, Theme.settings_panel_height)
    page.show()
    QApplication.processEvents()

    settings_vm.set_section(SettingsSectionKey.TAGS)
    QApplication.processEvents()

    tag_pages = page.findChildren(TagManagementPage)
    assert len(tag_pages) == 1


def test_tag_chip_sizes_to_content_width_clamped_to_max(qtbot) -> None:
    _apply_theme()
    # Use the max allowed length to push the chip past 200 px.
    vm = _viewmodel_with_tags("Comedy", "M" * 32)
    page = TagManagementPage(vm)
    qtbot.addWidget(page)
    page.show()
    QApplication.processEvents()

    tag_chips = sorted(
        (chip for chip in page.findChildren(TagChipWidget) if not chip.is_add_chip),
        key=lambda chip: chip._name,
    )
    # The 32 'M' chip should pin to the 200 px ceiling.
    long_hint_width = tag_chips[1].sizeHint().width()
    assert long_hint_width == Theme.tag_chip_max_width
    assert all(chip.sizeHint().height() == Theme.tag_chip_height for chip in tag_chips)
    # Short "Comedy" stays at or above the floor.
    short_hint_width = tag_chips[0].sizeHint().width()
    assert short_hint_width >= Theme.tag_chip_min_width
    assert short_hint_width <= Theme.tag_chip_max_width


def test_tag_chip_short_text_does_not_elide_before_max_width(qtbot) -> None:
    _apply_theme()
    vm = _viewmodel_with_tags("Comedy")
    page = TagManagementPage(vm)
    qtbot.addWidget(page)
    page.resize(620, 360)
    page.show()
    QApplication.processEvents()

    chip = next(chip for chip in page.findChildren(TagChipWidget) if not chip.is_add_chip)
    label = chip._label
    metrics = QFontMetrics(label.font())

    assert chip.width() < Theme.tag_chip_max_width
    assert label.width() >= metrics.horizontalAdvance("Comedy")
    assert label.text() == "Comedy"
    assert label.toolTip() == ""


def test_tag_chip_long_text_elides_only_at_max_width(qtbot) -> None:
    _apply_theme()
    long_name = "M" * 32
    vm = _viewmodel_with_tags(long_name)
    page = TagManagementPage(vm)
    qtbot.addWidget(page)
    page.resize(620, 360)
    page.show()
    QApplication.processEvents()

    chip = next(chip for chip in page.findChildren(TagChipWidget) if not chip.is_add_chip)
    label = chip._label

    assert chip.width() == Theme.tag_chip_max_width
    assert label.text() != long_name
    assert label.toolTip() == long_name


def test_revisiting_tags_section_does_not_crash(qtbot) -> None:
    # Regression: navigating away and back used to fire the previous
    # page's _render callback (still subscribed to the VM) against a
    # destroyed FlowLayout, raising
    #   "Internal C++ object (FlowLayout) already deleted."
    # The fix is to cache the TagManagementPage instance and skip its
    # deleteLater on section navigation, so the same page (with a live
    # FlowLayout) is reused.
    _apply_theme()
    settings_vm = SettingsViewModel()
    tag_vm = _viewmodel_with_tags("Comedy")
    page = SettingsPageWidget(
        settings_vm,
        ResourceLoader(),
        tag_viewmodel=tag_vm,
    )
    qtbot.addWidget(page)
    page.resize(Theme.settings_panel_width, Theme.settings_panel_height)
    page.show()
    QApplication.processEvents()

    settings_vm.set_section(SettingsSectionKey.TAGS)
    QApplication.processEvents()
    first_tag_page = page.findChild(TagManagementPage)
    assert first_tag_page is not None

    settings_vm.set_section(SettingsSectionKey.GENERAL)
    QApplication.processEvents()
    settings_vm.set_section(SettingsSectionKey.TAGS)
    QApplication.processEvents()
    # Same instance reused — page must not have been deleteLater'd.
    second_tag_page = page.findChild(TagManagementPage)
    assert second_tag_page is first_tag_page


def test_destroyed_tag_management_page_does_not_receive_vm_refresh(qtbot) -> None:
    _apply_theme()
    vm = _viewmodel_with_tags("Comedy")
    page = TagManagementPage(vm)
    qtbot.addWidget(page)
    page.show()
    QApplication.processEvents()

    page.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()

    # Regression: the VM used to retain page._render after the Qt object
    # was destroyed, so refresh crashed while touching the deleted FlowLayout.
    vm.refresh()


def test_leaving_tags_section_clears_selection(qtbot) -> None:
    _apply_theme()
    settings_vm = SettingsViewModel()
    tag_vm = _viewmodel_with_tags("Comedy", "Action")
    page = SettingsPageWidget(
        settings_vm,
        ResourceLoader(),
        tag_viewmodel=tag_vm,
    )
    qtbot.addWidget(page)
    page.show()
    QApplication.processEvents()

    settings_vm.set_section(SettingsSectionKey.TAGS)
    QApplication.processEvents()
    tag_vm.toggle_select(tag_vm.tags[0].tag_id, additive=False)
    assert tag_vm.selected_tag_ids != set()

    settings_vm.set_section(SettingsSectionKey.GENERAL)
    QApplication.processEvents()
    assert tag_vm.selected_tag_ids == set()


def test_clicking_blank_area_in_tag_manager_clears_selection(qtbot) -> None:
    _apply_theme()
    vm = _viewmodel_with_tags("Comedy")
    page = TagManagementPage(vm)
    qtbot.addWidget(page)
    page.resize(620, 360)
    page.show()
    QApplication.processEvents()

    vm.toggle_select(vm.tags[0].tag_id, additive=False)
    assert vm.selected_tag_ids != set()

    # A click somewhere inside the page that isn't on a chip / button.
    # The bottom-right corner of the manager frame is reliably empty.
    blank = page.rect().bottomRight() - QPoint(20, 60)
    qtbot.mouseClick(page, Qt.MouseButton.LeftButton, pos=blank)
    QApplication.processEvents()

    assert vm.selected_tag_ids == set()


def test_tag_chip_shift_click_toggles_selection(qtbot) -> None:
    _apply_theme()
    vm = _viewmodel_with_tags("Action", "Comedy")
    page = TagManagementPage(vm)
    qtbot.addWidget(page)
    page.show()
    QApplication.processEvents()

    tag_chips = sorted(
        (chip for chip in page.findChildren(TagChipWidget) if not chip.is_add_chip),
        key=lambda chip: chip._name,
    )
    qtbot.mouseClick(tag_chips[0], Qt.MouseButton.LeftButton)
    qtbot.mouseClick(
        tag_chips[1],
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ShiftModifier,
    )
    QApplication.processEvents()

    assert len(vm.selected_tag_ids) == 2
