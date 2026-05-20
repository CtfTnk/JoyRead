"""Reusable app-centered popup dialogs adapted from JoyRead Figma nodes."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSize, Qt, QTimer, Signal as QtSignal
from PySide6.QtGui import QColor, QIcon, QKeyEvent, QMouseEvent, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from joyread.core.models.collection import Collection
from joyread.core.models.tag import Tag
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.selection import toggle_selection
from joyread.ui.widgets.elided_label import ElidedLabel
from joyread.ui.widgets.tag_selection_panel import TagChipFlowWidget


class DialogTextButton(QFrame):
    """Figma ButtonLongText: fixed 100x28 text button with centered label."""

    clicked = QtSignal()

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pressed_inside = False
        self.setProperty("class", "DialogTextButton")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(Theme.dialog_button_width, Theme.dialog_button_height)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(Theme.dialog_button_shadow_blur)
        shadow.setOffset(0, Theme.dialog_button_shadow_offset)
        shadow.setColor(QColor(*Theme.color_shadow_rgba))
        self.setGraphicsEffect(shadow)

        layout = QHBoxLayout(self)
        # Figma uses 4px visual padding and a 1px stroke. Qt borders consume
        # layout space, so 3px margins keep the text inset visually at 4px.
        layout.setContentsMargins(
            Theme.dialog_button_layout_margin,
            Theme.dialog_button_layout_margin,
            Theme.dialog_button_layout_margin,
            Theme.dialog_button_layout_margin,
        )
        layout.setSpacing(0)

        self._label = QLabel(text)
        self._label.setProperty("class", "DialogTextButtonText")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self._label)

    @property
    def text(self) -> str:
        return self._label.text()

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
                self.clicked.emit()
            return
        self._pressed_inside = False
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class DialogMessageContent(QWidget):
    """Centered text content used by info, confirm, and delete prompts."""

    def __init__(self, message: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DialogMessageContent")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Theme.dialog_content_padding,
            Theme.dialog_content_padding,
            Theme.dialog_content_padding,
            Theme.dialog_content_padding,
        )
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._label = QLabel(message)
        self._label.setProperty("class", "JoyReadDialogContent")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setWordWrap(True)
        self._label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._label)

    def set_available_width(self, width: int) -> None:
        self.setFixedWidth(width)
        inner_width = max(0, width - (Theme.dialog_content_padding * 2))
        self._label.setFixedWidth(inner_width)
        wrapped_height = max(self._label.sizeHint().height(), self._label.heightForWidth(inner_width))
        # QLabel word-wrap can be one or two pixels short with platform font
        # fallback. Keep the measured content explicit so dialog panels do not
        # clip tall glyphs or the last wrapped line.
        self._label.setFixedHeight(wrapped_height + Theme.dialog_message_clip_guard)
        self.setFixedHeight(self._label.height() + (Theme.dialog_content_padding * 2))
        self.updateGeometry()


class DialogInputFieldWithHeader(QWidget):
    """Figma `input_field-with-header`: 200px group with a 192px input field."""

    def __init__(
        self,
        header: str,
        *,
        initial_text: str = "",
        echo_mode: QLineEdit.EchoMode = QLineEdit.EchoMode.Normal,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("DialogInputFieldWithHeader")
        self.setFixedWidth(Theme.dialog_input_group_width)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Theme.dialog_input_group_padding,
            Theme.dialog_input_group_padding,
            Theme.dialog_input_group_padding,
            Theme.dialog_input_group_padding,
        )
        layout.setSpacing(Theme.dialog_input_area_gap)

        self._header = ElidedLabel(header)
        self._header.setProperty("class", "DialogInputHeader")
        layout.addWidget(self._header)

        self.line_edit = QLineEdit(initial_text)
        self.line_edit.setProperty("class", "DialogInputField")
        self.line_edit.setEchoMode(echo_mode)
        if echo_mode != QLineEdit.EchoMode.Normal:
            self.line_edit.setInputMethodHints(Qt.InputMethodHint.ImhNone)
        self.line_edit.setFixedHeight(Theme.dialog_input_field_height)
        layout.addWidget(self.line_edit)

    @property
    def value(self) -> str:
        return self.line_edit.text()


class DialogInputContent(QWidget):
    """Figma one-field input content used for general JoyRead text prompts."""

    submitted = QtSignal()

    def __init__(
        self,
        header: str,
        initial_text: str = "",
        *,
        echo_mode: QLineEdit.EchoMode = QLineEdit.EchoMode.Normal,
        detail_text: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("DialogInputContent")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Theme.dialog_content_outer_padding,
            Theme.dialog_content_outer_padding,
            Theme.dialog_content_outer_padding,
            Theme.dialog_content_outer_padding,
        )
        layout.setSpacing(Theme.dialog_input_area_gap - 2)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        input_area = QWidget()
        input_area.setObjectName("DialogInputArea")
        input_layout = QVBoxLayout(input_area)
        input_layout.setContentsMargins(
            Theme.dialog_input_area_padding,
            Theme.dialog_input_area_padding,
            Theme.dialog_input_area_padding,
            Theme.dialog_input_area_padding,
        )
        input_layout.setSpacing(0)
        input_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.field = DialogInputFieldWithHeader(header, initial_text=initial_text, echo_mode=echo_mode)
        self.field.line_edit.returnPressed.connect(self.submitted.emit)
        input_layout.addWidget(self.field, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(input_area)

        self._detail_label = QLabel(detail_text or "")
        self._detail_label.setObjectName("DialogDetailPrompt")
        self._detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._detail_label.setWordWrap(True)
        self._detail_label.setVisible(bool(detail_text))
        layout.addWidget(self._detail_label)

        self._state_label = QLabel("")
        self._state_label.setObjectName("DialogStatePrompt")
        self._state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._state_label.setWordWrap(True)
        self._state_label.hide()
        layout.addWidget(self._state_label)

    @property
    def value(self) -> str:
        return self.field.value

    def set_state_prompt(self, message: str) -> None:
        self._state_label.setText(message)
        self._state_label.setVisible(bool(message))
        self.updateGeometry()

    def set_available_width(self, width: int) -> None:
        self.setFixedWidth(width)
        label_width = max(1, width - (Theme.dialog_content_outer_padding * 2))
        for label in (self._detail_label, self._state_label):
            label.setFixedWidth(label_width)
            if label.isVisible():
                label.setFixedHeight(_wrapped_label_height(label, label_width))
        self.updateGeometry()


class DialogPasswordContent(QWidget):
    """Future-ready password input content following the Figma password frame."""

    def __init__(
        self,
        headers: tuple[str, ...] = ("Old Password", "New Password", "Confirm New Password"),
        echo_modes: tuple[QLineEdit.EchoMode, ...] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("DialogPasswordContent")
        if echo_modes is None:
            echo_modes = tuple(QLineEdit.EchoMode.Password for _ in headers)
        if len(echo_modes) != len(headers):
            raise ValueError("echo_modes length must match headers length")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Theme.dialog_content_outer_padding,
            Theme.dialog_content_outer_padding,
            Theme.dialog_content_outer_padding,
            Theme.dialog_content_outer_padding,
        )
        layout.setSpacing(Theme.dialog_input_area_gap - 2)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        input_area = QWidget()
        input_area.setObjectName("DialogInputArea")
        input_layout = QVBoxLayout(input_area)
        input_layout.setContentsMargins(
            Theme.dialog_input_area_padding,
            Theme.dialog_input_area_padding,
            Theme.dialog_input_area_padding,
            Theme.dialog_input_area_padding,
        )
        input_layout.setSpacing(Theme.dialog_input_area_gap)
        input_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.fields: list[DialogInputFieldWithHeader] = []
        for header, echo_mode in zip(headers, echo_modes):
            field = DialogInputFieldWithHeader(header, echo_mode=echo_mode)
            self.fields.append(field)
            input_layout.addWidget(field, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(input_area)

        self._state_label = QLabel("")
        self._state_label.setObjectName("DialogStatePrompt")
        self._state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._state_label)

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(field.value for field in self.fields)

    def set_state_prompt(self, message: str) -> None:
        self._state_label.setText(message)
        self.updateGeometry()

    def set_available_width(self, width: int) -> None:
        self.setFixedWidth(width)


def _wrapped_label_height(label: QLabel, width: int) -> int:
    return max(label.sizeHint().height(), label.heightForWidth(width)) + Theme.dialog_message_clip_guard


class DialogCollectionChoiceRow(QFrame):
    """Selectable collection row reused inside the Add-to dialog list."""

    clicked = QtSignal(str)

    def __init__(
        self,
        collection: Collection,
        resources: ResourceLoader | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.collection_uuid = collection.uuid
        self._pressed_inside = False
        self.setProperty("class", "DialogCollectionChoice")
        self.setProperty("selected", "false")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(Theme.sidebar_item_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.sidebar_item_padding_left,
            Theme.sidebar_item_padding_vertical,
            Theme.sidebar_item_padding_right,
            Theme.sidebar_item_padding_vertical,
        )
        layout.setSpacing(Theme.sidebar_item_icon_text_gap)

        if resources is not None:
            icon = QLabel()
            icon.setObjectName("DialogCollectionChoiceIcon")
            icon.setFixedSize(Theme.icon_size, Theme.icon_size)
            # Match the sidebar treatment: hidable collections show the
            # strike-eye variant so the Add-to dialog can't visually
            # confuse a hidable target with a normal one.
            icon_name = "icon_collection_hiden.svg" if collection.is_hidable else "icon_collection.svg"
            icon.setPixmap(
                QIcon(str(resources.icon_path(icon_name))).pixmap(QSize(Theme.icon_size, Theme.icon_size))
            )
            layout.addWidget(icon)

        label = QLabel(collection.name)
        label.setObjectName("DialogCollectionChoiceLabel")
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(label)
        layout.addStretch(1)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

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
                self.clicked.emit(self.collection_uuid)
            return
        self._pressed_inside = False
        super().mouseReleaseEvent(event)


class DialogCollectionSelectContent(QWidget):
    """Figma Add-to collection content with a 260px bordered scroll panel."""

    def __init__(
        self,
        collections: list[Collection],
        resources: ResourceLoader | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("DialogCollectionSelectContent")
        self._selected_collection_uuid: str | None = collections[0].uuid if collections else None
        self._rows: dict[str, DialogCollectionChoiceRow] = {}
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Theme.dialog_content_outer_padding,
            Theme.dialog_content_outer_padding,
            Theme.dialog_content_outer_padding,
            Theme.dialog_content_outer_padding,
        )
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        input_area = QWidget()
        input_area.setObjectName("DialogInputArea")
        input_layout = QVBoxLayout(input_area)
        input_layout.setContentsMargins(
            Theme.dialog_input_area_padding,
            Theme.dialog_input_area_padding,
            Theme.dialog_input_area_padding,
            Theme.dialog_input_area_padding,
        )
        input_layout.setSpacing(0)
        input_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        panel = QFrame()
        panel.setObjectName("DialogCollectionScrollPanel")
        panel.setFixedWidth(Theme.dialog_collection_scroll_width)
        panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        panel_layout = QVBoxLayout(panel)
        # Figma gives this panel 10px visual padding with a 1px stroke. The Qt
        # border consumes one pixel, so 9px margins preserve the 10px inset.
        panel_layout.setContentsMargins(
            Theme.dialog_collection_scroll_layout_margin,
            Theme.dialog_collection_scroll_layout_margin,
            Theme.dialog_collection_scroll_layout_margin,
            Theme.dialog_collection_scroll_layout_margin,
        )
        panel_layout.setSpacing(0)

        self._row_scroll = QScrollArea()
        self._row_scroll.setObjectName("DialogCollectionInnerScrollArea")
        self._row_scroll.setWidgetResizable(True)
        self._row_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._row_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._row_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._row_scroll.viewport().setObjectName("DialogCollectionInnerViewport")
        self._row_scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        row_content = QWidget()
        row_content.setObjectName("DialogCollectionRowContent")
        row_layout = QVBoxLayout(row_content)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(Theme.dialog_collection_item_gap)
        row_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        for collection in collections:
            row = DialogCollectionChoiceRow(collection, resources)
            row.clicked.connect(self._select_collection)
            row.set_selected(collection.uuid == self._selected_collection_uuid)
            self._rows[collection.uuid] = row
            row_layout.addWidget(row)

        row_gaps = max(0, len(collections) - 1) * Theme.dialog_collection_item_gap
        row_content.setFixedHeight((len(collections) * Theme.sidebar_item_height) + row_gaps)
        self._row_scroll.setWidget(row_content)

        max_panel_height = (
            Theme.dialog_content_max_height
            - (Theme.dialog_content_outer_padding * 2)
            - (Theme.dialog_input_area_padding * 2)
        )
        max_scroll_height = max(0, max_panel_height - (Theme.dialog_collection_scroll_layout_margin * 2))
        scroll_height = min(row_content.height(), max_scroll_height)
        self._row_scroll.setFixedHeight(scroll_height)
        panel_layout.addWidget(self._row_scroll)
        panel.setFixedHeight((Theme.dialog_collection_scroll_layout_margin * 2) + scroll_height)
        input_layout.addWidget(panel, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(input_area)
        self.setFixedHeight(
            (Theme.dialog_content_outer_padding * 2)
            + (Theme.dialog_input_area_padding * 2)
            + panel.height()
        )

    @property
    def selected_collection_uuid(self) -> str | None:
        return self._selected_collection_uuid

    def set_available_width(self, width: int) -> None:
        self.setFixedWidth(width)
        self._row_scroll.verticalScrollBar().setValue(0)

    def _select_collection(self, collection_uuid: str) -> None:
        self._selected_collection_uuid = collection_uuid
        for row_uuid, row in self._rows.items():
            row.set_selected(row_uuid == collection_uuid)


class DialogTagFilterContent(QWidget):
    """Figma tag-filter content with a fixed 260px tag list panel."""

    def __init__(
        self,
        tags: list[Tag],
        selected_tag_ids: tuple[str, ...] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("DialogTagFilterContent")
        self._tags = tuple(tags)
        self._selected_tag_ids = set(selected_tag_ids)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Theme.dialog_content_outer_padding,
            Theme.dialog_content_outer_padding,
            Theme.dialog_content_outer_padding,
            Theme.dialog_content_outer_padding,
        )
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        input_area = QWidget()
        input_area.setObjectName("DialogInputArea")
        input_layout = QVBoxLayout(input_area)
        input_layout.setContentsMargins(
            Theme.dialog_input_area_padding,
            Theme.dialog_input_area_padding,
            Theme.dialog_input_area_padding,
            Theme.dialog_input_area_padding,
        )
        input_layout.setSpacing(0)
        input_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        panel = QFrame()
        panel.setObjectName("DialogTagFilterScrollPanel")
        panel.setFixedSize(Theme.dialog_collection_scroll_width, Theme.dialog_tag_filter_panel_height)
        panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(
            Theme.dialog_collection_scroll_layout_margin,
            Theme.dialog_collection_scroll_layout_margin,
            Theme.dialog_collection_scroll_layout_margin,
            Theme.dialog_collection_scroll_layout_margin,
        )
        panel_layout.setSpacing(0)

        self._row_scroll = QScrollArea()
        self._row_scroll.setObjectName("DialogTagFilterInnerScrollArea")
        self._row_scroll.setWidgetResizable(True)
        self._row_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._row_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._row_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._row_scroll.viewport().setObjectName("DialogTagFilterInnerViewport")
        self._row_scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        scroll_height = max(0, Theme.dialog_tag_filter_panel_height - (Theme.dialog_collection_scroll_layout_margin * 2))
        self._chip_flow = TagChipFlowWidget(object_name="DialogTagFilterListHost")
        self._chip_flow.setMinimumHeight(scroll_height)
        self._chip_flow.tag_clicked.connect(self._handle_tag_clicked)
        self._chip_flow.set_tags(self._tags, self._selected_tag_ids, include_add_chip=False)
        self._row_scroll.setWidget(self._chip_flow)
        self._row_scroll.setFixedHeight(scroll_height)
        panel_layout.addWidget(self._row_scroll)
        input_layout.addWidget(panel, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(input_area)
        self.setFixedHeight(Theme.dialog_content_max_height)

    @property
    def selected_tag_ids(self) -> tuple[str, ...]:
        return tuple(tag.tag_id for tag in self._tags if tag.tag_id in self._selected_tag_ids)

    def clear_selection(self) -> None:
        self._selected_tag_ids = set()
        self._chip_flow.clear_selection()

    def set_available_width(self, width: int) -> None:
        self.setFixedWidth(width)
        self._row_scroll.verticalScrollBar().setValue(0)

    def _handle_tag_clicked(self, tag_id: str, additive: bool) -> None:
        self._selected_tag_ids = toggle_selection(self._selected_tag_ids, tag_id, additive=additive)
        self._chip_flow.set_selected_tag_ids(self._selected_tag_ids)


class JoyReadDialogPanel(QFrame):
    """Figma's 400px popup panel with dynamic content up to 215px."""

    accepted = QtSignal()
    rejected = QtSignal()
    skipped = QtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "JoyReadDialogPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(Theme.dialog_width)
        self.setMinimumHeight(0)
        self._preferred_height = 0

        root_layout = QVBoxLayout(self)
        # Figma panel has 10px visual padding and a 2px stroke. Subtract the
        # QSS border so the child frames still begin 10px from the outer edge.
        root_layout.setContentsMargins(
            Theme.dialog_layout_margin,
            Theme.dialog_layout_margin,
            Theme.dialog_layout_margin,
            Theme.dialog_layout_margin,
        )
        root_layout.setSpacing(Theme.dialog_gap)

        self._title_area = QWidget()
        self._title_area.setObjectName("JoyReadDialogTitleArea")
        title_layout = QHBoxLayout(self._title_area)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(0)
        title_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._title_label = QLabel("Title")
        self._title_label.setProperty("class", "JoyReadDialogTitle")
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_layout.addWidget(self._title_label)
        root_layout.addWidget(self._title_area)

        self._content_area = QWidget()
        self._content_area.setObjectName("JoyReadDialogContentArea")
        self._content_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        content_layout = QHBoxLayout(self._content_area)
        content_layout.setContentsMargins(
            Theme.dialog_content_outer_padding,
            Theme.dialog_content_outer_padding,
            Theme.dialog_content_outer_padding,
            Theme.dialog_content_outer_padding,
        )
        content_layout.setSpacing(0)
        content_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._content_scroll = QScrollArea()
        self._content_scroll.setObjectName("JoyReadDialogContentScrollArea")
        self._content_scroll.viewport().setObjectName("JoyReadDialogContentViewport")
        self._content_scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._content_scroll.setWidgetResizable(False)
        self._content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._content_scroll.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        content_layout.addWidget(self._content_scroll, alignment=Qt.AlignmentFlag.AlignCenter)
        self._content_widget: QWidget | None = None
        root_layout.addWidget(self._content_area)

        self._option_area = QWidget()
        self._option_area.setObjectName("JoyReadDialogOptionArea")
        self._option_layout = QHBoxLayout(self._option_area)
        self._option_layout.setContentsMargins(0, 0, 0, 0)
        self._option_layout.setSpacing(Theme.dialog_option_gap)
        self._option_layout.setAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignBottom)
        root_layout.addWidget(self._option_area)

    def sizeHint(self) -> QSize:
        return QSize(Theme.dialog_width, self._preferred_height or self.layout().sizeHint().height())

    def set_info(self, title: str, message: str, button_text: str) -> None:
        self._set_title(title)
        self._set_content_widget(DialogMessageContent(message))
        self._set_buttons(((button_text, self.accepted.emit),))

    def set_confirm(
        self,
        title: str,
        message: str,
        cancel_text: str,
        confirm_text: str,
        *,
        destructive: bool = False,
    ) -> None:
        self._set_title(title, destructive=destructive)
        self._set_content_widget(DialogMessageContent(message))
        self._set_buttons(
            (
                (cancel_text, self.rejected.emit),
                (confirm_text, self.accepted.emit),
            )
        )

    def set_input_content(
        self,
        title: str,
        content: QWidget,
        cancel_text: str,
        confirm_text: str,
        skip_text: str | None = None,
    ) -> None:
        self._set_title(title)
        self._set_content_widget(content)
        buttons: list[tuple[str, Callable[[], None]]] = [(cancel_text, self.rejected.emit)]
        if skip_text is not None:
            buttons.append((skip_text, self.skipped.emit))
        buttons.append((confirm_text, self.accepted.emit))
        self._set_buttons(tuple(buttons))

    def set_tag_filter_content(self, title: str, content: DialogTagFilterContent) -> None:
        self._set_title(title)
        self._set_content_widget(content)
        self._set_buttons(
            (
                ("Reset", content.clear_selection),
                ("Confirm", self.accepted.emit),
            )
        )

    def refresh_size(self) -> None:
        self._refresh_size()

    def _set_title(self, title: str, *, destructive: bool = False) -> None:
        self._title_label.setText(title)
        # ``destructive`` property pairs with the QSS rule
        # ``QLabel[class="JoyReadDialogTitle"][destructive="true"]`` so the
        # title goes red whenever a Reset/Erase-style dialog reuses this
        # panel; resetting the property keeps subsequent dialogs neutral.
        self._title_label.setProperty("destructive", "true" if destructive else "false")
        self._title_label.style().unpolish(self._title_label)
        self._title_label.style().polish(self._title_label)

    def _set_buttons(self, buttons: tuple[tuple[str, Callable[[], None]], ...]) -> None:
        while self._option_layout.count():
            item = self._option_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        self._option_layout.addStretch(1)
        for label, callback in buttons:
            button = DialogTextButton(label, self)
            button.clicked.connect(callback)
            self._option_layout.addWidget(button)
        self._option_layout.addStretch(1)
        self._refresh_size()

    def _set_content_widget(self, widget: QWidget) -> None:
        if self._content_widget is not None:
            old_widget = self._content_scroll.takeWidget()
            if old_widget is not None:
                old_widget.setParent(None)
                old_widget.deleteLater()
        self._content_widget = widget
        self._content_scroll.setWidget(widget)
        self._refresh_size()

    def _refresh_size(self) -> None:
        if self._content_widget is None:
            return
        self._content_scroll.verticalScrollBar().setValue(0)
        self._content_scroll.horizontalScrollBar().setValue(0)
        available_width = (
            Theme.dialog_width
            - (Theme.dialog_layout_margin * 2)
            - (Theme.dialog_content_outer_padding * 2)
        )
        self._content_scroll.setFixedWidth(available_width)
        if hasattr(self._content_widget, "set_available_width"):
            self._content_widget.set_available_width(available_width)  # type: ignore[attr-defined]
        else:
            self._content_widget.setFixedWidth(available_width)

        if self._content_widget.layout() is not None:
            self._content_widget.layout().activate()
        self._content_widget.adjustSize()
        content_height = self._content_widget.sizeHint().height()
        viewport_height = min(max(0, content_height), Theme.dialog_content_max_height)
        self._content_scroll.setFixedHeight(viewport_height)
        self.layout().activate()
        self._preferred_height = self._calculate_panel_height()
        self.setFixedHeight(self._preferred_height)
        self.updateGeometry()

    def _calculate_panel_height(self) -> int:
        """Measure the fixed-height Figma frames explicitly before show/layout."""

        root_layout = self.layout()
        margins = root_layout.contentsMargins()
        content_margins = self._content_area.layout().contentsMargins()
        title_height = max(self._title_area.sizeHint().height(), self._title_label.sizeHint().height())
        content_height = self._content_scroll.height() + content_margins.top() + content_margins.bottom()
        option_height = max(self._option_area.sizeHint().height(), Theme.dialog_button_height)
        return (
            margins.top()
            + title_height
            + root_layout.spacing()
            + content_height
            + root_layout.spacing()
            + option_height
            + margins.bottom()
        )


class JoyReadDialogOverlay(QWidget):
    """Window-local modal layer that closes only through dialog buttons."""

    def __init__(
        self,
        parent: QWidget | None = None,
        resources: ResourceLoader | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("JoyReadDialogOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._resources = resources
        self._on_accept: Callable[[], None] | None = None
        self._on_reject: Callable[[], None] | None = None
        self._on_skip: Callable[[], None] | None = None
        self._before_accept: Callable[[], bool] | None = None
        self._key_forward_target: QLineEdit | None = None

        self._panel = JoyReadDialogPanel(self)
        self._panel.accepted.connect(self._accept)
        self._panel.rejected.connect(self._reject)
        self._panel.skipped.connect(self._skip)
        self.hide()

    @property
    def panel(self) -> JoyReadDialogPanel:
        return self._panel

    def show_info(self, title: str, message: str, button_text: str = "Confirm") -> None:
        self._on_accept = None
        self._on_reject = None
        self._on_skip = None
        self._before_accept = None
        self._panel.set_info(title, message, button_text)
        self._show_centered()

    def show_confirm(
        self,
        title: str,
        message: str,
        on_confirm: Callable[[], None],
        on_cancel: Callable[[], None] | None = None,
        confirm_text: str = "Confirm",
        cancel_text: str = "Cancel",
        *,
        destructive: bool = False,
    ) -> None:
        self._on_accept = on_confirm
        self._on_reject = on_cancel
        self._on_skip = None
        self._before_accept = None
        self._panel.set_confirm(title, message, cancel_text, confirm_text, destructive=destructive)
        self._show_centered()

    def show_input(
        self,
        title: str,
        header: str,
        on_confirm: Callable[[str], None],
        *,
        initial_text: str = "",
        confirm_text: str = "Confirm",
        cancel_text: str = "Cancel",
        validator: Callable[[str], str | None] | None = None,
    ) -> None:
        content = DialogInputContent(header, initial_text)

        def before_accept() -> bool:
            if validator is None:
                return True
            error = validator(content.value)
            if error is None:
                return True
            content.set_state_prompt(error)
            self._panel.refresh_size()
            self._position_panel()
            return False

        self._before_accept = before_accept
        self._on_accept = lambda: on_confirm(content.value)
        self._on_reject = None
        self._on_skip = None
        self._panel.set_input_content(title, content, cancel_text, confirm_text)
        content.submitted.connect(self._panel.accepted.emit)
        self._show_centered()
        self._focus_line_edit_deferred(content.field.line_edit, select_all_text=initial_text)

    def show_password_input(
        self,
        title: str,
        header: str,
        on_confirm: Callable[[str], None],
        *,
        confirm_text: str = "Confirm",
        cancel_text: str = "Cancel",
        validator: Callable[[str], str | None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        on_skip: Callable[[], None] | None = None,
        skip_text: str | None = None,
        detail_text: str | None = None,
        state_prompt: str | None = None,
    ) -> None:
        content = DialogInputContent(
            header,
            echo_mode=QLineEdit.EchoMode.PasswordEchoOnEdit,
            detail_text=detail_text,
        )
        if state_prompt:
            content.set_state_prompt(state_prompt)

        def before_accept() -> bool:
            if validator is None:
                return True
            error = validator(content.value)
            if error is None:
                return True
            content.set_state_prompt(error)
            self._panel.refresh_size()
            self._position_panel()
            return False

        self._before_accept = before_accept
        self._on_accept = lambda: on_confirm(content.value)
        self._on_reject = on_cancel
        self._on_skip = on_skip
        self._panel.set_input_content(title, content, cancel_text, confirm_text, skip_text)
        content.submitted.connect(self._panel.accepted.emit)
        self._show_centered()
        self._focus_line_edit_deferred(content.field.line_edit, select_all_text="")

    def show_multi_password_input(
        self,
        title: str,
        headers: tuple[str, ...],
        on_confirm: Callable[[tuple[str, ...]], None],
        *,
        echo_modes: tuple[QLineEdit.EchoMode, ...] | None = None,
        confirm_text: str = "Confirm",
        cancel_text: str = "Cancel",
        validator: Callable[[tuple[str, ...]], str | None] | None = None,
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        # Multi-field password panel used by Hidden Space setup (Password /
        # Confirm / Hint) and Change Password (Old / New / Confirm). The
        # validator returns ``None`` on success or a user-visible error
        # string that gets piped into ``set_state_prompt`` so the panel
        # stays open on validation failure.
        content = DialogPasswordContent(headers=headers, echo_modes=echo_modes)

        def before_accept() -> bool:
            if validator is None:
                return True
            error = validator(content.values)
            if error is None:
                return True
            content.set_state_prompt(error)
            self._panel.refresh_size()
            self._position_panel()
            return False

        self._before_accept = before_accept
        self._on_accept = lambda: on_confirm(content.values)
        self._on_reject = on_cancel
        self._on_skip = None
        self._panel.set_input_content(title, content, cancel_text, confirm_text)
        self._show_centered()
        if content.fields:
            self._focus_line_edit_deferred(content.fields[0].line_edit, select_all_text="")

    def show_collection_select(
        self,
        title: str,
        collections: list[Collection],
        on_confirm: Callable[[str], None],
        *,
        confirm_text: str = "Confirm",
        cancel_text: str = "Cancel",
    ) -> None:
        content = DialogCollectionSelectContent(collections, self._resources)

        def before_accept() -> bool:
            return content.selected_collection_uuid is not None

        self._before_accept = before_accept
        self._on_accept = lambda: on_confirm(content.selected_collection_uuid or "")
        self._on_reject = None
        self._on_skip = None
        self._panel.set_input_content(title, content, cancel_text, confirm_text)
        self._show_centered()

    def show_tag_filter(
        self,
        title: str,
        tags: list[Tag],
        selected_tag_ids: tuple[str, ...],
        on_confirm: Callable[[tuple[str, ...]], None],
    ) -> None:
        content = DialogTagFilterContent(tags, selected_tag_ids)
        self._before_accept = None
        self._on_accept = lambda: on_confirm(content.selected_tag_ids)
        self._on_reject = None
        self._on_skip = None
        self._panel.set_tag_filter_content(title, content)
        self._show_centered()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # The popup is intentionally modal inside the app window: blank clicks
        # are swallowed but do not dismiss it.
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            return
        if self._forward_key_to_active_input(event):
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_panel()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._position_panel()

    def _show_centered(self) -> None:
        self._panel.refresh_size()
        self._position_panel()
        self.show()
        self.raise_()
        self._panel.raise_()
        self.setFocus(Qt.FocusReason.PopupFocusReason)

    def _focus_line_edit_deferred(self, line_edit: QLineEdit, *, select_all_text: str) -> None:
        self._key_forward_target = line_edit

        def focus_line_edit() -> None:
            if not self.isVisible() or not line_edit.isVisible():
                return
            line_edit.setFocus(Qt.FocusReason.PopupFocusReason)
            # If a fast first key arrived while the overlay still had focus,
            # do not select that inserted text and make the next key replace it.
            if line_edit.text() == select_all_text:
                line_edit.selectAll()

        QTimer.singleShot(0, focus_line_edit)

    def _forward_key_to_active_input(self, event: QKeyEvent) -> bool:
        target = self._key_forward_target
        if target is None or not target.isVisible():
            return False
        if event.key() in {
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Escape,
            Qt.Key.Key_Tab,
            Qt.Key.Key_Backtab,
        }:
            return False
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            return False
        text = event.text()
        if not text:
            return False
        target.setFocus(Qt.FocusReason.PopupFocusReason)
        target.insert(text)
        event.accept()
        return True

    def _position_panel(self) -> None:
        x = (self.width() - self._panel.width()) // 2
        y = (self.height() - self._panel.height()) // 2
        self._panel.move(max(0, x), max(0, y))

    def _accept(self) -> None:
        if self._before_accept is not None and not self._before_accept():
            return
        callback = self._on_accept
        self._clear_and_hide()
        if callback is not None:
            callback()

    def _reject(self) -> None:
        callback = self._on_reject
        self._clear_and_hide()
        if callback is not None:
            callback()

    def _skip(self) -> None:
        callback = self._on_skip
        self._clear_and_hide()
        if callback is not None:
            callback()

    def _clear_and_hide(self) -> None:
        self._on_accept = None
        self._on_reject = None
        self._on_skip = None
        self._before_accept = None
        self._key_forward_target = None
        self.hide()
