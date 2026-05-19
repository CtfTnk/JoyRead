"""View-model for the Settings -> Tags management surface."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from joyread.core.models.tag import MAX_TAG_NAME_LENGTH, Tag
from joyread.core.services.tag_service import (
    TagNameConflictError,
    TagNotFoundError,
    TagService,
)
from joyread.ui.viewmodels.signals import Signal


logger = logging.getLogger(__name__)


class TagInputMode(Enum):
    BUTTONS = "buttons"   # Rename + Delete row visible.
    CREATE = "create"     # Input row visible, no source tag.
    RENAME = "rename"     # Input row visible, prefilled with selected tag name.


@dataclass(frozen=True)
class TagOperationResult:
    success: bool
    title: str
    message: str


class TagManagementViewModel:
    def __init__(self, service: TagService) -> None:
        self._service = service
        self.state_changed: Signal[None] = Signal()
        self.operation_result: Signal[TagOperationResult] = Signal()

        self.tags: tuple[Tag, ...] = ()
        self.selected_tag_ids: set[str] = set()
        self.input_mode: TagInputMode = TagInputMode.BUTTONS
        self.input_initial_text: str = ""

    def replace_service(self, service: TagService) -> None:
        """Swap to a fresh TagService after a storage-root rebuild.

        The new service points at the rebuilt database; existing state
        (selection, in-flight input) is dropped because the tag rows
        themselves no longer exist.
        """
        self._service = service
        self.tags = ()
        self.selected_tag_ids = set()
        self.input_mode = TagInputMode.BUTTONS
        self.input_initial_text = ""
        self.state_changed.emit()

    @property
    def can_rename(self) -> bool:
        return len(self.selected_tag_ids) == 1 and self.input_mode == TagInputMode.BUTTONS

    @property
    def can_delete(self) -> bool:
        return len(self.selected_tag_ids) >= 1 and self.input_mode == TagInputMode.BUTTONS

    def refresh(self) -> None:
        try:
            self.tags = tuple(self._service.list_tags())
        except Exception:
            logger.exception("TagManagementViewModel.refresh failed")
            self.tags = ()
        # Drop selections that point at tags no longer present.
        valid_ids = {tag.tag_id for tag in self.tags}
        self.selected_tag_ids = {tag_id for tag_id in self.selected_tag_ids if tag_id in valid_ids}
        self.state_changed.emit()

    def clear_selection(self) -> None:
        """Deselect every chip and exit input mode.

        Wired to (a) blank-area clicks inside the tag manager and
        (b) leaving the Tags settings section. Emits ``state_changed``
        only when state actually changes so we don't churn the UI.
        """
        if not self.selected_tag_ids and self.input_mode == TagInputMode.BUTTONS:
            return
        self.selected_tag_ids = set()
        self.input_mode = TagInputMode.BUTTONS
        self.input_initial_text = ""
        self.state_changed.emit()

    def toggle_select(self, tag_id: str, *, additive: bool) -> None:
        if self.input_mode != TagInputMode.BUTTONS:
            # Selecting a chip while editing exits input mode and resets
            # selection to the clicked tag. Matches the user's spec: the
            # input is in flight, so committing to a different tag should
            # cancel the in-flight edit.
            self.input_mode = TagInputMode.BUTTONS
            self.input_initial_text = ""
            self.selected_tag_ids = {tag_id}
            self.state_changed.emit()
            return
        if additive:
            if tag_id in self.selected_tag_ids:
                self.selected_tag_ids.discard(tag_id)
            else:
                self.selected_tag_ids.add(tag_id)
        else:
            self.selected_tag_ids = {tag_id}
        self.state_changed.emit()

    def begin_create(self) -> None:
        self.selected_tag_ids = set()
        self.input_mode = TagInputMode.CREATE
        self.input_initial_text = ""
        self.state_changed.emit()

    def begin_rename(self) -> None:
        if len(self.selected_tag_ids) != 1:
            return
        tag_id = next(iter(self.selected_tag_ids))
        tag = self._tag_by_id(tag_id)
        if tag is None:
            return
        self.input_mode = TagInputMode.RENAME
        self.input_initial_text = tag.name
        self.state_changed.emit()

    def cancel_input(self) -> None:
        if self.input_mode == TagInputMode.BUTTONS:
            return
        self.input_mode = TagInputMode.BUTTONS
        self.input_initial_text = ""
        self.state_changed.emit()

    def submit_input(self, text: str) -> None:
        if self.input_mode == TagInputMode.CREATE:
            self._submit_create(text)
            return
        if self.input_mode == TagInputMode.RENAME:
            self._submit_rename(text)
            return
        # No active input — defensive no-op.
        logger.debug("submit_input called while input_mode=%s; ignoring", self.input_mode)

    def delete_selected(self) -> None:
        if not self.selected_tag_ids:
            return
        targets = [self._tag_by_id(tag_id) for tag_id in self.selected_tag_ids]
        targets = [tag for tag in targets if tag is not None]
        if not targets:
            return
        total_unlinked = 0
        failed_names: list[str] = []
        for tag in targets:
            try:
                total_unlinked += self._service.delete(tag.tag_id)
            except Exception as exc:
                logger.exception("delete_selected failed for tag=%s", tag.tag_id)
                failed_names.append(f"'{tag.name}': {exc}")
        self.selected_tag_ids = set()
        self.refresh()
        if failed_names:
            self.operation_result.emit(
                TagOperationResult(
                    success=False,
                    title="Delete Tag",
                    message="Some tags could not be deleted:\n" + "\n".join(failed_names),
                )
            )
            return
        if len(targets) == 1:
            message = f"Tag '{targets[0].name}' deleted ({total_unlinked} books unlinked)."
        else:
            message = (
                f"Deleted {len(targets)} tags "
                f"({total_unlinked} books unlinked)."
            )
        self.operation_result.emit(
            TagOperationResult(success=True, title="Delete Tag", message=message)
        )

    def _submit_create(self, text: str) -> None:
        try:
            tag = self._service.create(text)
        except ValueError as exc:
            self.operation_result.emit(
                TagOperationResult(success=False, title="Create Tag", message=str(exc))
            )
            return
        except TagNameConflictError as exc:
            self.operation_result.emit(
                TagOperationResult(success=False, title="Create Tag", message=str(exc))
            )
            return
        except Exception as exc:
            logger.exception("Tag create failed")
            self.operation_result.emit(
                TagOperationResult(success=False, title="Create Tag", message=str(exc))
            )
            return
        self.input_mode = TagInputMode.BUTTONS
        self.input_initial_text = ""
        self.refresh()
        self.operation_result.emit(
            TagOperationResult(
                success=True,
                title="Create Tag",
                message=f"Tag '{tag.name}' created.",
            )
        )

    def _submit_rename(self, text: str) -> None:
        if len(self.selected_tag_ids) != 1:
            return
        tag_id = next(iter(self.selected_tag_ids))
        try:
            tag = self._service.rename(tag_id, text)
        except ValueError as exc:
            self.operation_result.emit(
                TagOperationResult(success=False, title="Rename Tag", message=str(exc))
            )
            return
        except TagNameConflictError as exc:
            self.operation_result.emit(
                TagOperationResult(success=False, title="Rename Tag", message=str(exc))
            )
            return
        except TagNotFoundError as exc:
            self.operation_result.emit(
                TagOperationResult(success=False, title="Rename Tag", message=str(exc))
            )
            return
        except Exception as exc:
            logger.exception("Tag rename failed")
            self.operation_result.emit(
                TagOperationResult(success=False, title="Rename Tag", message=str(exc))
            )
            return
        self.input_mode = TagInputMode.BUTTONS
        self.input_initial_text = ""
        self.refresh()
        self.operation_result.emit(
            TagOperationResult(
                success=True,
                title="Rename Tag",
                message=f"Tag renamed to '{tag.name}'.",
            )
        )

    def _tag_by_id(self, tag_id: str) -> Tag | None:
        for tag in self.tags:
            if tag.tag_id == tag_id:
                return tag
        return None


__all__ = [
    "MAX_TAG_NAME_LENGTH",
    "TagInputMode",
    "TagOperationResult",
    "TagManagementViewModel",
]
