"""Reusable tag chip flow used by Settings and filter dialogs."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QWidget

from joyread.core.models.tag import Tag
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.flow_layout import FlowLayout
from joyread.ui.widgets.tag_chip import TagChipWidget


class TagChipFlowWidget(QWidget):
    """Small reusable owner for the tag chip FlowLayout."""

    tag_clicked = QtSignal(str, bool)
    add_clicked = QtSignal()
    blank_clicked = QtSignal(bool)

    def __init__(self, parent: QWidget | None = None, *, object_name: str = "TagListHost") -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._tags: tuple[Tag, ...] = ()
        self._selected_tag_ids: set[str] = set()
        self._pressed_inside = False
        self._flow_layout = FlowLayout(
            self,
            margin=0,
            horizontal_spacing=Theme.tag_chip_gap,
            vertical_spacing=Theme.tag_chip_gap,
        )

    @property
    def selected_tag_ids(self) -> tuple[str, ...]:
        return tuple(tag.tag_id for tag in self._tags if tag.tag_id in self._selected_tag_ids)

    @property
    def chip_widgets(self) -> tuple[TagChipWidget, ...]:
        return tuple(
            item.widget()  # type: ignore[union-attr]
            for index in range(self._flow_layout.count())
            if (item := self._flow_layout.itemAt(index)) is not None
            and isinstance(item.widget(), TagChipWidget)
        )

    def set_tags(
        self,
        tags: Iterable[Tag],
        selected_tag_ids: Iterable[str] = (),
        *,
        include_add_chip: bool = False,
    ) -> None:
        self._tags = tuple(tags)
        valid_ids = {tag.tag_id for tag in self._tags}
        self._selected_tag_ids = {tag_id for tag_id in selected_tag_ids if tag_id in valid_ids}

        while self._flow_layout.count():
            item = self._flow_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        for tag in self._tags:
            chip = TagChipWidget(tag.tag_id, tag.name)
            chip.set_selected(tag.tag_id in self._selected_tag_ids)
            chip.chip_clicked.connect(self.tag_clicked.emit)
            self._flow_layout.addWidget(chip)
        if include_add_chip:
            add_chip = TagChipWidget.as_add_chip()
            add_chip.add_clicked.connect(self.add_clicked.emit)
            self._flow_layout.addWidget(add_chip)
        self.adjustSize()

    def set_selected_tag_ids(self, selected_tag_ids: Iterable[str]) -> None:
        valid_ids = {tag.tag_id for tag in self._tags}
        self._selected_tag_ids = {tag_id for tag_id in selected_tag_ids if tag_id in valid_ids}
        for chip in self.chip_widgets:
            if chip.is_add_chip:
                continue
            chip.set_selected(chip.tag_id in self._selected_tag_ids)

    def clear_selection(self) -> None:
        self.set_selected_tag_ids(())

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed_inside = True
            event.accept()
            return
        self._pressed_inside = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed_inside:
            self._pressed_inside = False
            event.accept()
            if self.rect().contains(event.position().toPoint()):
                additive = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                self.blank_clicked.emit(additive)
            return
        self._pressed_inside = False
        super().mouseReleaseEvent(event)
