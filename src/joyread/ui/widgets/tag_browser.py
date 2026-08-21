"""Searchable, A-Z grouped tag browser (design "Tag Window", screen 1a).

One widget for all three tag surfaces -- the tag filter dialog, the assign
dialog, and the Settings tag manager -- because they are the same problem:
a flat chip cloud stops being usable somewhere past fifty tags.

Layout, top to bottom: a search row, then a fixed-height pool holding an
optional "on this book" tray above the grouped chip list, with a letter rail
down the right edge that jumps to a bucket.
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QIcon, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from joyread.core.models.tag import Tag
from joyread.core.tag_indexing import group_tags
from joyread.infrastructure.i18n.locale_service import active_language_code, t
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.auto_hide_scrollbar import AutoHideScrollHandle
from joyread.ui.widgets.flow_layout import FlowLayout
from joyread.ui.widgets.tag_chip import TagChipWidget


def _faded_pixmap(path: str, size: int, opacity: float) -> QPixmap:
    """Render an SVG once at *size* with *opacity* baked in.

    Baking the alpha into a shared pixmap keeps the per-chip cost to a label
    paint -- a QGraphicsOpacityEffect per tray chip would not.
    """

    source = QIcon(path).pixmap(QSize(size, size))
    faded = QPixmap(source.size())
    faded.setDevicePixelRatio(source.devicePixelRatio())
    faded.fill(Qt.GlobalColor.transparent)
    painter = QPainter(faded)
    painter.setOpacity(opacity)
    painter.drawPixmap(0, 0, source)
    painter.end()
    return faded


class _SectionHeader(QWidget):
    """A caption followed by a hairline that eats the remaining width."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Theme.tag_browser_section_gap)
        self._label = QLabel(text)
        self._label.setProperty("class", "TagBrowserSectionLabel")
        layout.addWidget(self._label)
        rule = QFrame()
        rule.setObjectName("TagBrowserSectionRule")
        rule.setFixedHeight(1)
        rule.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(rule, stretch=1)
        self._trailing = QLabel("")
        self._trailing.setProperty("class", "TagBrowserSectionLabel")
        layout.addWidget(self._trailing)

    def set_text(self, text: str) -> None:
        self._label.setText(text)

    def set_trailing(self, text: str) -> None:
        self._trailing.setText(text)


class _LetterRail(QLabel):
    """One clickable letter in the jump rail."""

    clicked = QtSignal(str)

    def __init__(self, letter: str, parent: QWidget | None = None) -> None:
        super().__init__(letter, parent)
        self._letter = letter
        self.setObjectName("TagBrowserRailLetter")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(Theme.tag_browser_rail_row_height)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            event.accept()
            self.clicked.emit(self._letter)
            return
        super().mouseReleaseEvent(event)


class TagBrowserWidget(QWidget):
    """Search + tray + A-Z grouped chips + jump rail.

    Selection is owned by the caller: this widget reports clicks and repaints
    whatever ``set_selected_tag_ids`` is handed back, so the filter dialog's
    multi-select and the assign dialog's shift-to-replace rules stay where
    they already live instead of being duplicated here.
    """

    tag_clicked = QtSignal(str, bool)
    tag_remove_clicked = QtSignal(str)
    add_clicked = QtSignal()
    blank_clicked = QtSignal(bool)

    def __init__(
        self,
        resources: ResourceLoader | None = None,
        parent: QWidget | None = None,
        *,
        show_tray: bool = False,
        include_add_chip: bool = False,
        han_language: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("TagBrowser")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._resources = resources or ResourceLoader()
        self._show_tray = show_tray
        self._include_add_chip = include_add_chip
        self._follows_active_locale = han_language is None
        self._han_language = han_language or self._active_han_language()
        self._tags: tuple[Tag, ...] = ()
        self._selected_tag_ids: set[str] = set()
        self._query = ""
        self._group_anchors: dict[str, QWidget] = {}
        self._chips: list[TagChipWidget] = []
        self._pressed_inside = False

        self._remove_pixmap = _faded_pixmap(
            str(self._resources.icon_path("icon_close.svg")),
            Theme.tag_browser_remove_icon_size,
            Theme.tag_browser_remove_icon_opacity,
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(Theme.tag_browser_gap)

        root.addWidget(self._build_search_row())
        root.addWidget(self._build_pool(), stretch=1)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_search_row(self) -> QWidget:
        row = QFrame()
        row.setObjectName("TagBrowserSearchRow")
        row.setFixedHeight(Theme.tag_browser_search_height)
        row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(
            Theme.tag_browser_search_padding_horizontal,
            0,
            Theme.tag_browser_search_padding_horizontal,
            0,
        )
        layout.setSpacing(Theme.tag_browser_search_gap)

        icon = QLabel()
        icon.setPixmap(
            _faded_pixmap(
                str(self._resources.icon_path("icon_search.svg")),
                Theme.tag_browser_search_icon_size,
                Theme.tag_browser_search_icon_opacity,
            )
        )
        icon.setFixedSize(Theme.tag_browser_search_icon_size, Theme.tag_browser_search_icon_size)
        layout.addWidget(icon)

        self._search_field = QLineEdit()
        self._search_field.setObjectName("TagBrowserSearchField")
        self._search_field.setPlaceholderText(t("tags.search_placeholder"))
        self._search_field.setFrame(False)
        # Typing only updates the clear affordance; the pool rebuilds when the
        # query is committed. Rebuilding per keystroke costs the whole library
        # each time -- 54ms at 1,000 tags, 282ms at 5,000 -- for a result the
        # user is still in the middle of describing.
        self._search_field.textChanged.connect(self._handle_text_edited)
        self._search_field.returnPressed.connect(self._apply_query)
        layout.addWidget(self._search_field, stretch=1)

        self._clear_button = QLabel()
        self._clear_button.setObjectName("TagBrowserSearchClear")
        self._clear_button.setPixmap(
            _faded_pixmap(
                str(self._resources.icon_path("icon_close.svg")),
                Theme.tag_browser_clear_icon_size,
                Theme.tag_browser_clear_icon_opacity,
            )
        )
        self._clear_button.setFixedSize(
            Theme.tag_browser_clear_icon_size, Theme.tag_browser_clear_icon_size
        )
        self._clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_button.mouseReleaseEvent = self._handle_clear_clicked  # type: ignore[method-assign]
        self._clear_button.hide()
        layout.addWidget(self._clear_button)
        return row

    def _build_pool(self) -> QWidget:
        pool = QFrame()
        pool.setObjectName("TagBrowserPool")
        pool.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        pool.setFixedHeight(Theme.tag_browser_pool_height)
        layout = QVBoxLayout(pool)
        margin = Theme.tag_browser_pool_padding - Theme.tag_browser_pool_border_width
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(Theme.tag_browser_pool_gap)

        self._tray = self._build_tray()
        layout.addWidget(self._tray)
        self._tray.setVisible(self._show_tray)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(Theme.tag_browser_body_gap)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("TagBrowserScrollArea")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.viewport().setObjectName("TagBrowserScrollViewport")
        self._scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._groups_host = QWidget()
        self._groups_host.setObjectName("TagBrowserGroupsHost")
        self._groups_host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._groups_layout = QVBoxLayout(self._groups_host)
        self._groups_layout.setContentsMargins(0, 0, Theme.tag_browser_scroll_padding, 0)
        self._groups_layout.setSpacing(0)
        self._groups_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(self._groups_host)
        self._scroll_handle = AutoHideScrollHandle(self._scroll, parent=self)
        body_layout.addWidget(self._scroll, stretch=1)

        self._rail = QWidget()
        self._rail.setObjectName("TagBrowserRail")
        self._rail.setFixedWidth(Theme.tag_browser_rail_width)
        self._rail_layout = QVBoxLayout(self._rail)
        self._rail_layout.setContentsMargins(0, 0, 0, 0)
        self._rail_layout.setSpacing(0)
        body_layout.addWidget(self._rail)

        layout.addWidget(body, stretch=1)
        return pool

    def _build_tray(self) -> QWidget:
        tray = QWidget()
        tray.setObjectName("TagBrowserTray")
        layout = QVBoxLayout(tray)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Theme.tag_browser_tray_gap)
        self._tray_header = _SectionHeader(t("tags.tray_header"))
        layout.addWidget(self._tray_header)

        self._tray_scroll = QScrollArea()
        self._tray_scroll.setObjectName("TagBrowserTrayScrollArea")
        self._tray_scroll.setWidgetResizable(True)
        self._tray_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._tray_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._tray_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._tray_scroll.setMaximumHeight(Theme.tag_browser_tray_max_height)
        self._tray_scroll.viewport().setObjectName("TagBrowserTrayViewport")
        self._tray_scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._tray_host = QWidget()
        self._tray_host.setObjectName("TagBrowserTrayHost")
        self._tray_host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._tray_flow = FlowLayout(
            self._tray_host,
            margin=0,
            horizontal_spacing=Theme.tag_chip_gap,
            vertical_spacing=Theme.tag_chip_gap,
        )
        self._tray_scroll.setWidget(self._tray_host)
        layout.addWidget(self._tray_scroll)
        return tray

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def selected_tag_ids(self) -> tuple[str, ...]:
        return tuple(tag.tag_id for tag in self._tags if tag.tag_id in self._selected_tag_ids)

    @property
    def chip_widgets(self) -> tuple[TagChipWidget, ...]:
        return tuple(self._chips)

    @property
    def search_field(self) -> QLineEdit:
        return self._search_field

    @property
    def rail_letters(self) -> tuple[str, ...]:
        return tuple(
            child.text()
            for child in self._rail.findChildren(_LetterRail)
        )

    def set_han_language(self, han_language: str) -> None:
        self._follows_active_locale = False
        if han_language == self._han_language:
            return
        self._han_language = han_language
        self._render_groups()
        self._render_rail()

    def set_tags(
        self,
        tags: Iterable[Tag],
        selected_tag_ids: Iterable[str] = (),
        *,
        include_add_chip: bool | None = None,
    ) -> None:
        self._tags = tuple(tags)
        valid = {tag.tag_id for tag in self._tags}
        self._selected_tag_ids = {tag_id for tag_id in selected_tag_ids if tag_id in valid}
        if include_add_chip is not None:
            self._include_add_chip = include_add_chip
        self._render_groups()
        self._render_rail()
        self._render_tray()

    def set_selected_tag_ids(self, selected_tag_ids: Iterable[str]) -> None:
        valid = {tag.tag_id for tag in self._tags}
        self._selected_tag_ids = {tag_id for tag_id in selected_tag_ids if tag_id in valid}
        for chip in self._chips:
            if not chip.is_add_chip:
                chip.set_selected(chip.tag_id in self._selected_tag_ids)
        self._render_tray()

    def clear_selection(self) -> None:
        self.set_selected_tag_ids(())

    def refresh_labels(self) -> None:
        self._search_field.setPlaceholderText(t("tags.search_placeholder"))
        self._tray_header.set_text(t("tags.tray_header"))
        language_changed = False
        if self._follows_active_locale:
            han_language = self._active_han_language()
            language_changed = han_language != self._han_language
            self._han_language = han_language
        self._render_tray()
        self._render_groups()
        if language_changed:
            self._render_rail()

    @staticmethod
    def _active_han_language() -> str:
        """Use Chinese readings only for the Chinese app locale.

        English and Japanese retain the established Japanese reading for
        shared Han text; an explicit constructor/setter override opts out of
        following subsequent locale changes.
        """

        return "zh" if active_language_code() == "zh" else "ja"

    def jump_to_letter(self, letter: str) -> None:
        """Scroll the *letter* group to the top of the pool.

        Reads the group's laid-out position, so it needs the pool to have
        been through a layout pass. That always holds for a rail click --
        the user has to see the rail to click it -- but a caller driving
        this straight after ``set_tags`` must let the event loop run first,
        or every anchor still reports ``y() == 0``.
        """

        anchor = self._group_anchors.get(letter)
        if anchor is None:
            return
        self._groups_layout.activate()
        self._scroll.verticalScrollBar().setValue(max(0, anchor.y()))

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _clear_layout(self, layout) -> None:  # noqa: ANN001 - any QLayout.
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _render_groups(self) -> None:
        self._clear_layout(self._groups_layout)
        self._group_anchors = {}
        self._chips = []

        groups = group_tags(self._tags, han_language=self._han_language, query=self._query)
        if not groups:
            empty = QLabel(
                t("tags.no_search_match") if self._query else t("dialog.no_tags_hint")
            )
            empty.setObjectName("TagBrowserEmptyHint")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setWordWrap(True)
            empty.setMinimumHeight(Theme.tag_browser_empty_height)
            self._groups_layout.addWidget(empty)
            self._append_add_chip_group()
            self._scroll.verticalScrollBar().setValue(0)
            return

        for letter, tags in groups:
            group = QWidget()
            group.setObjectName("TagBrowserGroup")
            group_layout = QVBoxLayout(group)
            group_layout.setContentsMargins(0, 0, 0, Theme.tag_browser_group_bottom_padding)
            group_layout.setSpacing(Theme.tag_browser_group_gap)
            group_layout.addWidget(_SectionHeader(letter))

            chips_host = QWidget()
            flow = FlowLayout(
                chips_host,
                margin=0,
                horizontal_spacing=Theme.tag_chip_gap,
                vertical_spacing=Theme.tag_chip_gap,
            )
            for tag in tags:
                chip = TagChipWidget(tag.tag_id, tag.name)
                chip.set_selected(tag.tag_id in self._selected_tag_ids)
                chip.chip_clicked.connect(self.tag_clicked.emit)
                flow.addWidget(chip)
                self._chips.append(chip)
            group_layout.addWidget(chips_host)

            self._groups_layout.addWidget(group)
            self._group_anchors[letter] = group

        self._append_add_chip_group()
        self._scroll.verticalScrollBar().setValue(0)

    def _append_add_chip_group(self) -> None:
        """The "+" entry lives after every bucket, not inside one -- it is a
        command, and sorting it under "+" would put it on the rail."""

        if not self._include_add_chip or self._query:
            return
        host = QWidget()
        flow = FlowLayout(
            host,
            margin=0,
            horizontal_spacing=Theme.tag_chip_gap,
            vertical_spacing=Theme.tag_chip_gap,
        )
        add_chip = TagChipWidget.as_add_chip()
        add_chip.add_clicked.connect(self.add_clicked.emit)
        flow.addWidget(add_chip)
        self._chips.append(add_chip)
        self._groups_layout.addWidget(host)

    def _render_rail(self) -> None:
        self._clear_layout(self._rail_layout)
        # Built from every tag, not the filtered set, so the rail does not
        # reshuffle under the cursor while the user is typing.
        present: list[str] = []
        seen: set[str] = set()
        for _letter, _tags in group_tags(self._tags, han_language=self._han_language):
            if _letter not in seen:
                seen.add(_letter)
                present.append(_letter)
        for letter in present:
            row = _LetterRail(letter)
            row.clicked.connect(self.jump_to_letter)
            self._rail_layout.addWidget(row)
        self._rail_layout.addStretch(1)
        self._rail.setVisible(bool(present))

    def _render_tray(self) -> None:
        if not self._show_tray:
            return
        self._clear_layout(self._tray_flow)
        selected = [tag for tag in self._tags if tag.tag_id in self._selected_tag_ids]
        self._tray_header.set_trailing(
            t("tags.tray_count", count=str(len(selected)), total=str(len(self._tags)))
        )
        if not selected:
            hint = QLabel(t("tags.tray_empty_hint"))
            hint.setObjectName("TagBrowserTrayEmptyHint")
            hint.setFixedHeight(Theme.tag_chip_height)
            self._tray_flow.addWidget(hint)
            self._sync_tray_height()
            return
        for tag in selected:
            chip = TagChipWidget(tag.tag_id, tag.name, remove_pixmap=self._remove_pixmap)
            chip.set_selected(True)
            chip.chip_clicked.connect(
                lambda tag_id, _additive: self.tag_remove_clicked.emit(tag_id)
            )
            self._tray_flow.addWidget(chip)
        self._sync_tray_height()

    def _sync_tray_height(self) -> None:
        """Give the tray only the rows it needs, up to its cap.

        Without this the tray always claims its maximum and leaves dead
        space above the first group. It grows to roughly three rows and then
        scrolls, so the pool below keeps every pixel the tray is not using.
        """

        if not self._show_tray:
            return
        width = self._tray_host.width() or self._tray_scroll.viewport().width()
        needed = (
            self._tray_flow.heightForWidth(width) if width > 0 else Theme.tag_chip_height
        )
        capped = min(max(Theme.tag_chip_height, needed), Theme.tag_browser_tray_max_height)
        self._tray_scroll.setFixedHeight(capped)

    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt API override.
        super().resizeEvent(event)
        # Chips reflow at the new width, so the row count -- and with it the
        # tray's height -- can change without the selection changing.
        self._sync_tray_height()

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def _handle_text_edited(self, text: str) -> None:
        self._clear_button.setVisible(bool(text))
        # Emptying the field is not a search, it is a cancel, so restore the
        # full list straight away rather than leaving stale results behind a
        # blank box.
        if not text and self._query:
            self._apply_query()

    def _apply_query(self) -> None:
        text = self._search_field.text()
        if text == self._query:
            return
        self._query = text
        self._render_groups()

    def _handle_clear_clicked(self, event: QMouseEvent) -> None:
        event.accept()
        self._search_field.clear()
        self._apply_query()
        self._search_field.setFocus(Qt.FocusReason.OtherFocusReason)

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


__all__ = ["TagBrowserWidget"]
