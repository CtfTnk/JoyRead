"""Settings -> Tags management page (Figma node 688:3630)."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.tag_management_viewmodel import (
    MAX_TAG_NAME_LENGTH,
    TagInputMode,
    TagManagementViewModel,
    TagOperationResult,
)
from joyread.ui.widgets.auto_hide_scrollbar import AutoHideScrollHandle
from joyread.ui.widgets.flow_layout import FlowLayout
from joyread.ui.widgets.settings_page import SettingsPushButton
from joyread.ui.widgets.tag_chip import TagChipWidget


class _TagInputLineEdit(QLineEdit):
    """QLineEdit subclass that emits ``escape_pressed`` when Esc is hit."""

    escape_pressed = QtSignal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key.Key_Escape:
            self.escape_pressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class TagManagementPage(QWidget):
    """The container body inside the Settings page for the Tags surface."""

    tag_operation_completed = QtSignal(bool, str, str)
    tag_delete_requested = QtSignal(str, str)

    def __init__(self, viewmodel: TagManagementViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._viewmodel = viewmodel
        self._disposed = False
        self.setFixedHeight(Theme.tag_manager_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        self._manager_frame = QFrame(self)
        self._manager_frame.setObjectName("TagManager")
        self._manager_frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._manager_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._manager_frame.setFixedHeight(Theme.tag_manager_height)
        outer_layout.addWidget(self._manager_frame, stretch=1)

        manager_layout = QVBoxLayout(self._manager_frame)
        manager_layout.setContentsMargins(
            Theme.tag_manager_padding,
            Theme.tag_manager_padding,
            Theme.tag_manager_padding,
            Theme.tag_manager_padding,
        )
        manager_layout.setSpacing(Theme.tag_manager_gap)

        self._scroll_area = QScrollArea(self._manager_frame)
        self._scroll_area.setObjectName("TagListScrollArea")
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.viewport().setObjectName("TagListViewport")
        self._scroll_area.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        manager_layout.addWidget(self._scroll_area, stretch=1)

        self._chip_host = QWidget()
        self._chip_host.setObjectName("TagListHost")
        self._chip_host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._flow_layout = FlowLayout(
            self._chip_host,
            margin=0,
            horizontal_spacing=Theme.tag_chip_gap,
            vertical_spacing=Theme.tag_chip_gap,
        )
        self._scroll_area.setWidget(self._chip_host)
        self._scroll_handle = AutoHideScrollHandle(self._scroll_area, parent=self)

        # Control bar — QStackedLayout with [0] button row, [1] input row.
        self._control_stack_host = QWidget()
        self._control_stack_host.setFixedHeight(Theme.tag_control_bar_height)
        self._control_stack_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._control_stack = QStackedLayout(self._control_stack_host)
        self._control_stack.setContentsMargins(0, 0, 0, 0)

        self._button_bar = QWidget()
        button_layout = QHBoxLayout(self._button_bar)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(Theme.tag_manager_gap)
        button_layout.addStretch(1)
        self._rename_button = SettingsPushButton("Rename")
        self._rename_button.setProperty("variant", "tag")
        self._rename_button.setFixedSize(Theme.tag_control_button_width, Theme.tag_control_button_height)
        self._rename_button.clicked.connect(self._handle_rename_clicked)
        button_layout.addWidget(self._rename_button)
        self._delete_button = SettingsPushButton("Delete")
        self._delete_button.setProperty("variant", "tag")
        self._delete_button.setFixedSize(Theme.tag_control_button_width, Theme.tag_control_button_height)
        self._delete_button.setProperty("destructive", "true")
        self._delete_button.clicked.connect(self._handle_delete_clicked)
        button_layout.addWidget(self._delete_button)
        button_layout.addStretch(1)
        self._control_stack.addWidget(self._button_bar)

        self._input_bar = QWidget()
        input_layout = QHBoxLayout(self._input_bar)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(Theme.tag_manager_gap)
        input_layout.addStretch(1)
        self._line_edit = _TagInputLineEdit()
        self._line_edit.setProperty("class", "TagInputField")
        self._line_edit.setMaxLength(MAX_TAG_NAME_LENGTH)
        self._line_edit.setPlaceholderText("Tag name")
        self._line_edit.setFixedSize(Theme.tag_input_field_width, Theme.tag_control_button_height)
        self._line_edit.returnPressed.connect(self._handle_submit_clicked)
        self._line_edit.escape_pressed.connect(self._viewmodel.cancel_input)
        input_layout.addWidget(self._line_edit)
        self._confirm_button = SettingsPushButton("Confirm")
        self._confirm_button.setProperty("variant", "tag")
        self._confirm_button.setFixedSize(Theme.tag_control_button_width, Theme.tag_control_button_height)
        self._confirm_button.clicked.connect(self._handle_submit_clicked)
        input_layout.addWidget(self._confirm_button)
        input_layout.addStretch(1)
        self._control_stack.addWidget(self._input_bar)

        manager_layout.addWidget(self._control_stack_host)

        self._viewmodel.state_changed.connect(self._render)
        self._viewmodel.operation_result.connect(self._handle_operation_result)
        self.destroyed.connect(self._handle_destroyed)
        self._render()

    @property
    def chip_widgets(self) -> tuple[TagChipWidget, ...]:
        return tuple(
            self._flow_layout.itemAt(index).widget()  # type: ignore[union-attr]
            for index in range(self._flow_layout.count())
            if self._flow_layout.itemAt(index) is not None
            and isinstance(self._flow_layout.itemAt(index).widget(), TagChipWidget)
        )

    @property
    def rename_button(self) -> SettingsPushButton:
        return self._rename_button

    @property
    def delete_button(self) -> SettingsPushButton:
        return self._delete_button

    @property
    def confirm_button(self) -> SettingsPushButton:
        return self._confirm_button

    @property
    def line_edit(self) -> QLineEdit:
        return self._line_edit

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._viewmodel.state_changed.disconnect(self._render)
        self._viewmodel.operation_result.disconnect(self._handle_operation_result)

    def _render(self) -> None:
        if self._disposed:
            return
        # Rebuild the chip flow from the current VM state. The chip count
        # is small (tens at most) so a full rebuild is cheap and avoids
        # complicating the layout's identity tracking.
        while self._flow_layout.count():
            item = self._flow_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        for tag in self._viewmodel.tags:
            chip = TagChipWidget(tag.tag_id, tag.name)
            chip.set_selected(tag.tag_id in self._viewmodel.selected_tag_ids)
            chip.chip_clicked.connect(self._handle_chip_clicked)
            self._flow_layout.addWidget(chip)
        add_chip = TagChipWidget.as_add_chip()
        add_chip.add_clicked.connect(self._viewmodel.begin_create)
        self._flow_layout.addWidget(add_chip)
        # FlowLayout's height depends on width — kick a re-layout so the
        # scroll area sees the latest measurement.
        self._chip_host.adjustSize()

        in_input_mode = self._viewmodel.input_mode != TagInputMode.BUTTONS
        if in_input_mode:
            self._control_stack.setCurrentWidget(self._input_bar)
            self._line_edit.setText(self._viewmodel.input_initial_text)
            self._line_edit.setFocus(Qt.FocusReason.OtherFocusReason)
            self._line_edit.selectAll()
        else:
            self._control_stack.setCurrentWidget(self._button_bar)
            self._line_edit.clear()
        self._rename_button.setEnabled(self._viewmodel.can_rename)
        self._delete_button.setEnabled(self._viewmodel.can_delete)

    def _handle_chip_clicked(self, tag_id: str, additive: bool) -> None:
        self._viewmodel.toggle_select(tag_id, additive=additive)

    def _handle_rename_clicked(self) -> None:
        self._viewmodel.begin_rename()

    def _handle_delete_clicked(self) -> None:
        if not self._viewmodel.can_delete:
            return
        title, message = self._delete_confirmation_text()
        self.tag_delete_requested.emit(title, message)

    def _handle_submit_clicked(self) -> None:
        self._viewmodel.submit_input(self._line_edit.text())

    def _handle_operation_result(self, result: TagOperationResult) -> None:
        self.tag_operation_completed.emit(result.success, result.title, result.message)

    def _delete_confirmation_text(self) -> tuple[str, str]:
        selected_names = [
            tag.name for tag in self._viewmodel.tags if tag.tag_id in self._viewmodel.selected_tag_ids
        ]
        if len(selected_names) == 1:
            return (
                "Delete Tag",
                (
                    f"Delete tag '{selected_names[0]}'?\n\n"
                    "This removes the tag from every linked book. Books are not deleted. "
                    "This cannot be undone."
                ),
            )
        return (
            "Delete Tags",
            (
                f"Delete {len(selected_names)} tags?\n\n"
                "This removes these tags from every linked book. Books are not deleted. "
                "This cannot be undone."
            ),
        )

    def _handle_destroyed(self, _obj: object | None = None) -> None:
        self.dispose()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        # Chips and the control bar accept their own clicks (event.accept),
        # so anything reaching this handler is a click in the empty area
        # of the tag manager (or on the manager's own background). Treat
        # that as "deselect all", matching the user expectation that
        # clicking blank space clears the selection.
        if event.button() == Qt.MouseButton.LeftButton:
            self._viewmodel.clear_selection()
            event.accept()
            return
        super().mousePressEvent(event)


__all__ = ["TagManagementPage"]
