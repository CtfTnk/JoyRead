"""Tests for the TagManagementViewModel."""

from __future__ import annotations

from joyread.core.models.tag import Tag, normalize_tag_name
from joyread.core.repositories.tag_repository import (
    TagNameConflictError,
    TagNotFoundError,
)
from joyread.core.services.tag_service import TagService
from joyread.ui.viewmodels.tag_management_viewmodel import (
    TagInputMode,
    TagManagementViewModel,
    TagOperationResult,
)


class _FakeRepository:
    def __init__(self) -> None:
        self._tags: dict[str, Tag] = {}
        self._next_id = 0
        self.linked: dict[str, int] = {}

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
        name = normalize_tag_name(display_name)
        if self.get_tag_by_normalized(name.casefold()):
            raise TagNameConflictError(f"A tag named '{name}' already exists.")
        self._next_id += 1
        tag = Tag(tag_id=f"tag-{self._next_id}", name=name)
        self._tags[tag.tag_id] = tag
        return tag

    def find_or_create(self, display_name: str) -> Tag:
        name = normalize_tag_name(display_name)
        existing = self.get_tag_by_normalized(name.casefold())
        if existing is not None:
            return existing
        return self.create(display_name)

    def rename(self, tag_id: str, new_display_name: str) -> Tag:
        name = normalize_tag_name(new_display_name)
        current = self._tags.get(tag_id)
        if current is None:
            raise TagNotFoundError("Not found.")
        if current.name_normalized != name.casefold() and self.get_tag_by_normalized(name.casefold()):
            raise TagNameConflictError("Conflict.")
        renamed = Tag(tag_id=tag_id, name=name)
        self._tags[tag_id] = renamed
        return renamed

    def delete(self, tag_id: str) -> int:
        if tag_id not in self._tags:
            return 0
        linked = self.linked.pop(tag_id, 0)
        del self._tags[tag_id]
        return linked

    def link_book(self, tag_id: str, book_id: str) -> None:
        self.linked[tag_id] = self.linked.get(tag_id, 0) + 1

    def unlink_book(self, tag_id: str, book_id: str) -> None:
        if self.linked.get(tag_id):
            self.linked[tag_id] -= 1

    def list_tag_ids_for_book(self, book_id: str) -> list[str]:
        return []


def _viewmodel_with_tags(*names: str) -> tuple[TagManagementViewModel, _FakeRepository, list[TagOperationResult]]:
    repo = _FakeRepository()
    service = TagService(repo)
    for name in names:
        repo.create(name)
    vm = TagManagementViewModel(service)
    results: list[TagOperationResult] = []
    vm.operation_result.connect(results.append)
    vm.refresh()
    return vm, repo, results


def test_refresh_loads_tags_sorted() -> None:
    vm, _, _ = _viewmodel_with_tags("comedy", "Action", "drama")
    assert [tag.name for tag in vm.tags] == ["Action", "Comedy", "Drama"]


def test_toggle_select_replaces_selection_without_shift() -> None:
    vm, _, _ = _viewmodel_with_tags("Action", "Comedy")
    first, second = vm.tags
    vm.toggle_select(first.tag_id, additive=False)
    vm.toggle_select(second.tag_id, additive=False)
    assert vm.selected_tag_ids == {second.tag_id}


def test_toggle_select_clears_single_selection_without_shift() -> None:
    vm, _, _ = _viewmodel_with_tags("Action")
    tag = vm.tags[0]

    vm.toggle_select(tag.tag_id, additive=False)
    vm.toggle_select(tag.tag_id, additive=False)

    assert vm.selected_tag_ids == set()


def test_toggle_select_keeps_clicked_member_from_multi_selection_without_shift() -> None:
    vm, _, _ = _viewmodel_with_tags("Action", "Comedy", "Drama")
    first, second, third = vm.tags

    vm.toggle_select(first.tag_id, additive=False)
    vm.toggle_select(second.tag_id, additive=True)
    vm.toggle_select(third.tag_id, additive=True)
    vm.toggle_select(second.tag_id, additive=False)

    assert vm.selected_tag_ids == {second.tag_id}


def test_toggle_select_replaces_multi_selection_with_unselected_without_shift() -> None:
    vm, _, _ = _viewmodel_with_tags("Action", "Comedy", "Drama")
    first, second, third = vm.tags

    vm.toggle_select(first.tag_id, additive=False)
    vm.toggle_select(second.tag_id, additive=True)
    vm.toggle_select(third.tag_id, additive=False)

    assert vm.selected_tag_ids == {third.tag_id}


def test_toggle_select_additive_toggles_membership() -> None:
    vm, _, _ = _viewmodel_with_tags("Action", "Comedy", "Drama")
    first, second, third = vm.tags
    vm.toggle_select(first.tag_id, additive=False)
    vm.toggle_select(second.tag_id, additive=True)
    vm.toggle_select(third.tag_id, additive=True)
    vm.toggle_select(second.tag_id, additive=True)  # toggles off
    assert vm.selected_tag_ids == {first.tag_id, third.tag_id}


def test_button_enablement_matrix() -> None:
    vm, _, _ = _viewmodel_with_tags("Action", "Comedy")
    assert not vm.can_rename
    assert not vm.can_delete

    first, second = vm.tags
    vm.toggle_select(first.tag_id, additive=False)
    assert vm.can_rename
    assert vm.can_delete

    vm.toggle_select(second.tag_id, additive=True)
    assert not vm.can_rename
    assert vm.can_delete


def test_begin_create_clears_selection_and_enters_create_mode() -> None:
    vm, _, _ = _viewmodel_with_tags("Action")
    vm.toggle_select(vm.tags[0].tag_id, additive=False)
    vm.begin_create()
    assert vm.input_mode == TagInputMode.CREATE
    assert vm.selected_tag_ids == set()
    assert vm.input_initial_text == ""


def test_begin_rename_requires_single_selection() -> None:
    vm, _, _ = _viewmodel_with_tags("Action", "Comedy")
    vm.begin_rename()  # no selection — no-op
    assert vm.input_mode == TagInputMode.BUTTONS

    first, second = vm.tags
    vm.toggle_select(first.tag_id, additive=False)
    vm.toggle_select(second.tag_id, additive=True)
    vm.begin_rename()  # two selections — no-op
    assert vm.input_mode == TagInputMode.BUTTONS

    vm.toggle_select(second.tag_id, additive=True)  # back to single selection
    vm.begin_rename()
    assert vm.input_mode == TagInputMode.RENAME
    assert vm.input_initial_text == "Action"


def test_submit_create_success_emits_result_and_refreshes() -> None:
    vm, repo, results = _viewmodel_with_tags()
    vm.begin_create()
    vm.submit_input("comedy")
    assert vm.input_mode == TagInputMode.BUTTONS
    assert [tag.name for tag in vm.tags] == ["Comedy"]
    assert results[-1].success is True
    assert "Comedy" in results[-1].message


def test_submit_create_conflict_keeps_input_open() -> None:
    vm, _, results = _viewmodel_with_tags("Comedy")
    vm.begin_create()
    vm.submit_input("COMEDY")
    assert vm.input_mode == TagInputMode.CREATE  # still open
    assert results[-1].success is False
    assert "already exists" in results[-1].message


def test_submit_create_empty_keeps_input_open() -> None:
    vm, _, results = _viewmodel_with_tags()
    vm.begin_create()
    vm.submit_input("   ")
    assert vm.input_mode == TagInputMode.CREATE
    assert results[-1].success is False


def test_submit_rename_success() -> None:
    vm, _, results = _viewmodel_with_tags("Action")
    tag = vm.tags[0]
    vm.toggle_select(tag.tag_id, additive=False)
    vm.begin_rename()
    vm.submit_input("Drama")
    assert vm.input_mode == TagInputMode.BUTTONS
    assert [tag.name for tag in vm.tags] == ["Drama"]
    assert results[-1].success is True


def test_submit_rename_conflict_keeps_input_open() -> None:
    vm, _, results = _viewmodel_with_tags("Action", "Comedy")
    action, _ = vm.tags
    vm.toggle_select(action.tag_id, additive=False)
    vm.begin_rename()
    vm.submit_input("Comedy")
    assert vm.input_mode == TagInputMode.RENAME
    assert results[-1].success is False


def test_delete_selected_emits_singular_message() -> None:
    vm, repo, results = _viewmodel_with_tags("Comedy")
    tag = vm.tags[0]
    repo.link_book(tag.tag_id, "book-1")
    repo.link_book(tag.tag_id, "book-2")
    vm.toggle_select(tag.tag_id, additive=False)
    vm.delete_selected()
    assert vm.tags == ()
    assert results[-1].success is True
    assert "Comedy" in results[-1].message
    assert "2" in results[-1].message


def test_delete_selected_multiple_emits_combined_message() -> None:
    vm, repo, results = _viewmodel_with_tags("Action", "Comedy")
    first, second = vm.tags
    repo.link_book(first.tag_id, "b1")
    repo.link_book(second.tag_id, "b2")
    vm.toggle_select(first.tag_id, additive=False)
    vm.toggle_select(second.tag_id, additive=True)
    vm.delete_selected()
    assert vm.tags == ()
    assert results[-1].success is True
    assert "Deleted 2 tags" in results[-1].message


def test_cancel_input_returns_to_buttons() -> None:
    vm, _, _ = _viewmodel_with_tags("Action")
    vm.begin_create()
    vm.cancel_input()
    assert vm.input_mode == TagInputMode.BUTTONS
    assert vm.input_initial_text == ""


def test_clicking_chip_while_in_input_mode_cancels_input() -> None:
    vm, _, _ = _viewmodel_with_tags("Action", "Comedy")
    first, _ = vm.tags
    vm.begin_create()
    vm.toggle_select(first.tag_id, additive=False)
    assert vm.input_mode == TagInputMode.BUTTONS
    assert vm.selected_tag_ids == {first.tag_id}
