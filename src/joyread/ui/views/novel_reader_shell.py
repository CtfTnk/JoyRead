"""Skeleton novel reader shell mirroring the manga reader's chrome.

Engine work is deferred: this widget renders a placeholder
``NovelContentArea`` and pipes the footer slider to its scrollbar so the
chrome behaviour (auto-hide, ESC ladder, panel outside-click close,
slider↔scroll) can be exercised end-to-end before any EPUB parsing
exists.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, Qt, Signal as QtSignal
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QCursor,
    QIcon,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
)
from PySide6.QtWidgets import QApplication, QToolButton, QWidget

from joyread.app.app_context import AppContext
from joyread.core.models.book import Book
from joyread.core.reader import ReaderDirection
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.views.reader_chrome import AutoHideController, PanelOutsideClickFilter
from joyread.ui.widgets.dialogs import JoyReadDialogOverlay
from joyread.ui.widgets.novel_content_area import NovelContentArea
from joyread.ui.widgets.novel_custom_panel import NovelCustomPanel
from joyread.ui.widgets.reader_controls import ReaderFooter, ReaderHeader
from joyread.ui.widgets.reader_topic_panel import ReaderTopicMode, ReaderTopicPanel


logger = logging.getLogger(__name__)


class NovelReaderShellWidget(QWidget):
    """Reader shell for novel/EPUB content; skeleton until engine lands."""

    back_requested = QtSignal()
    progress_changed = QtSignal(str, int, float)

    def __init__(
        self,
        context: AppContext,
        source_path: str | Path,
        *,
        book: Book | None = None,
        title: str | None = None,
        show_back_button: bool = False,
        start_page_index: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._context = context
        self._source_path = Path(source_path)
        self._drag_position: QPoint | None = None
        self._show_back_button = show_back_button
        self._book = book
        # Skeleton placeholders. Engine work replaces these with the real
        # chapter/page indices coming from a NovelReaderViewModel.
        self._page_count = 1
        self._current_index = 0
        del start_page_index  # accepted for signature parity with manga shell

        self.setObjectName("NovelReaderRootPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        self.content_area = NovelContentArea(self)
        # Aliased so chrome code that talks to ``canvas`` in the manga
        # shell can be ported with minimal renaming.
        self.canvas = self.content_area
        self.header = ReaderHeader(context.resources, self, show_custom_button=True)
        self.header.set_back_visible(show_back_button)
        self.footer = ReaderFooter(context.resources, self)
        # Direction/transition switches and spread shift are manga-only;
        # hide them so the novel footer matches Figma's stripped layout.
        self.footer.direction_switch.hide()
        self.footer.effect_switch.hide()
        self.footer.shift_button.hide()
        # The header's Custom gear is the single trigger for the right-
        # side panel; a second gear in the footer would be redundant.
        self.footer.settings_button.hide()
        # Novels read top-to-bottom: anchor the slider's filled track to
        # the left so progress visually grows in the same direction the
        # next-page button advances.
        self.footer.slider.set_reading_direction(ReaderDirection.TOP_TO_BOTTOM)
        self.left_arrow = _side_button(context.resources, "icon_left.svg", self)
        self.right_arrow = _side_button(context.resources, "icon_right.svg", self)
        self.custom_panel = NovelCustomPanel(context.resources, self)
        self.custom_panel.hide()
        self.topic_panel = ReaderTopicPanel(context.resources, self)
        self.topic_panel.hide()
        self.dialog_overlay = JoyReadDialogOverlay(self, context.resources)
        self.dialog_overlay.hide()

        resolved_title = title or (book.title if book is not None else self._source_path.stem)
        self.header.set_title(resolved_title)
        # Topic group: contents/bookmarks unavailable in the skeleton.
        self.header.set_contents_enabled(False)
        self.header.set_bookmarks_enabled(False)
        self.topic_panel.reset_thumbnails(0)
        # set_page_state would otherwise reset the slider's reading
        # direction to whatever the (hidden) direction switch defaults
        # to; pass TOP_TO_BOTTOM so the filled track stays left-anchored.
        self.footer.set_page_state(self._current_index, self._page_count, ReaderDirection.TOP_TO_BOTTOM)
        self._update_page_indicator()

        logger.info(
            "NovelReaderShellWidget init: path=%s book=%s embedded=%s",
            self._source_path,
            book.uuid if book is not None else None,
            show_back_button,
        )

        self._connect_signals()
        self._install_auto_hide()

    def cancel(self) -> None:
        """Tear down before close; symmetric with the manga shell."""

    def _connect_signals(self) -> None:
        self.header.back_requested.connect(self.back_requested.emit)
        self.header.mouse_activity.connect(lambda: self._show_controls((self.header,), reset_timer=True))
        self.header.custom_requested.connect(self._toggle_custom_panel)
        self.header.topic_mode_requested.connect(self._show_topic_panel)
        self.footer.mouse_activity.connect(lambda: self._show_controls((self.footer,), reset_timer=True))
        self.footer.start_requested.connect(self._scroll_to_start)
        self.footer.previous_requested.connect(lambda: self.content_area.scroll_by_viewport(-1))
        self.footer.next_requested.connect(lambda: self.content_area.scroll_by_viewport(1))
        self.footer.end_requested.connect(self._scroll_to_end)
        self.footer.seek_requested.connect(self._handle_seek)
        self.left_arrow.clicked.connect(lambda: self.content_area.scroll_by_viewport(-1))
        self.right_arrow.clicked.connect(lambda: self.content_area.scroll_by_viewport(1))
        # Edge-of-content mouse + right-click wake the chrome — mirrors
        # the manga shell's wiring against ``ReaderCanvas``.
        self.content_area.mouse_moved.connect(self._handle_content_mouse_move)
        self.content_area.left_clicked.connect(self._hide_floating_panels_if_visible)
        self.content_area.right_clicked.connect(lambda: self._show_controls(reset_timer=True))
        # Slider doubles as a scrollbar — both directions feed each other,
        # but ``NovelContentArea.set_scroll_percentage`` and the slider
        # blockSignals guard prevent the round-trip from looping.
        self.footer.slider.valueChanged.connect(self._sync_content_from_slider)
        self.content_area.scroll_changed.connect(self._sync_slider_from_content)
        self.custom_panel.enable_custom_changed.connect(self._handle_enable_custom_changed)
        self.custom_panel.font_size_changed.connect(self._handle_font_size_changed)

    def _install_auto_hide(self) -> None:
        control_widgets = (self.header, self.footer, self.left_arrow, self.right_arrow)
        self.auto_hide = AutoHideController(
            self,
            control_widgets,
            delay_ms=Theme.reader_auto_hide_delay_ms,
            interaction_predicate=lambda: self._control_interaction_active(),
            on_after_show=lambda: self._raise_panels_if_visible(),
        )
        self.header.installEventFilter(self)
        self.panel_filter = PanelOutsideClickFilter(self)
        self.panel_filter.register(
            self.custom_panel,
            safe_click_predicate=self._is_custom_safe_click,
            on_outside_click=self._hide_custom_panel,
        )
        self.panel_filter.register(
            self.topic_panel,
            safe_click_predicate=self._is_topic_safe_click,
            on_outside_click=self._hide_topic_panel,
        )
        self.auto_hide.start()

    # --- Edge-reveal mouse handling ------------------------------------
    def _handle_content_mouse_move(self, position: QPoint) -> None:
        edge = Theme.reader_edge_reveal_distance
        if position.y() <= edge:
            self._show_controls((self.header,), reset_timer=True)
            return
        if position.y() >= self.height() - edge:
            self._show_controls((self.footer,), reset_timer=True)
            return
        if position.x() <= edge:
            self._show_controls((self.left_arrow,), reset_timer=True)
            return
        if position.x() >= self.width() - edge:
            self._show_controls((self.right_arrow,), reset_timer=True)

    # --- Slider <-> scroll wiring --------------------------------------
    def _handle_seek(self, index: int) -> None:
        if self._page_count <= 1:
            # Slider currently mirrors scroll percentage rather than page
            # indices; the seek_requested signal fires on slider release
            # and we already track it via valueChanged. Nothing to do.
            return
        self._current_index = max(0, min(index, self._page_count - 1))
        self._update_page_indicator()

    def _sync_content_from_slider(self, value: int) -> None:
        maximum = max(1, self.footer.slider.maximum())
        fraction = value / maximum if maximum > 0 else 0.0
        self.content_area.set_scroll_percentage(fraction)

    def _sync_slider_from_content(self, fraction: float) -> None:
        maximum = max(1, self.footer.slider.maximum())
        new_value = int(round(fraction * maximum))
        if self.footer.slider.value() == new_value:
            return
        # blockSignals here breaks the slider→content→slider loop without
        # losing the painted-track redraw triggered by setValue().
        self.footer.slider.blockSignals(True)
        try:
            self.footer.slider.setValue(new_value)
        finally:
            self.footer.slider.blockSignals(False)

    def _scroll_to_start(self) -> None:
        self.content_area.set_scroll_percentage(0.0)

    def _scroll_to_end(self) -> None:
        self.content_area.set_scroll_percentage(1.0)

    def _update_page_indicator(self) -> None:
        # For the skeleton the slider runs from 0..100 to give the seek
        # bar continuous resolution against the placeholder body. When a
        # real engine ships, ``page_count`` drives the slider domain.
        self.footer.slider.blockSignals(True)
        try:
            self.footer.slider.setMinimum(0)
            self.footer.slider.setMaximum(100)
        finally:
            self.footer.slider.blockSignals(False)
        self.footer.page_indicator.setText(f"{self._current_index + 1}/{max(self._page_count, 1)}")

    # --- Panels ---------------------------------------------------------
    def _toggle_custom_panel(self) -> None:
        if self.custom_panel.isVisible():
            self._hide_custom_panel()
            return
        self._hide_topic_panel()
        self._position_custom_panel()
        self.custom_panel.show()
        self.custom_panel.raise_()
        self.panel_filter.activate(self.custom_panel)
        self._start_hide_timer_if_allowed()

    def _show_topic_panel(self, mode: ReaderTopicMode) -> None:
        # Topic features (TOC/bookmarks/thumbnails) need the engine; the
        # skeleton acknowledges the click but renders no panel content.
        self.header.clear_topic_active_mode()
        del mode

    def _hide_custom_panel(self) -> None:
        if self.custom_panel.isHidden():
            return
        self.custom_panel.hide()
        self.panel_filter.deactivate(self.custom_panel)
        self._start_hide_timer_if_allowed()

    def _hide_topic_panel(self) -> None:
        if self.topic_panel.isHidden():
            return
        self.topic_panel.hide()
        self.header.clear_topic_active_mode()
        self.panel_filter.deactivate(self.topic_panel)
        self._start_hide_timer_if_allowed()

    def _hide_floating_panels_if_visible(self) -> None:
        if self.custom_panel.isVisible():
            self._hide_custom_panel()
        if self.topic_panel.isVisible():
            self._hide_topic_panel()

    def _raise_panels_if_visible(self) -> None:
        if self.custom_panel.isVisible():
            self.custom_panel.raise_()
        if self.topic_panel.isVisible():
            self.topic_panel.raise_()
        if self.dialog_overlay.isVisible():
            self.dialog_overlay.raise_()

    def _position_custom_panel(self) -> None:
        self.custom_panel.setFixedHeight(self.height())
        x = max(0, self.width() - self.custom_panel.width())
        self.custom_panel.move(x, 0)

    def _position_topic_panel(self) -> None:
        width = min(Theme.reader_topic_panel_width, max(Theme.reader_topic_panel_min_width, self.width() - 32))
        height = min(Theme.reader_topic_panel_height, max(Theme.reader_topic_panel_min_height, self.height() - 32))
        self.topic_panel.setFixedSize(width, height)
        self.topic_panel.move((self.width() - width) // 2, (self.height() - height) // 2)

    def _is_custom_safe_click(self, widget: QWidget | None) -> bool:
        # ``windowType()`` returns the WindowType after masking; bitwise
        # AND against the Popup flag is buggy because every Window has
        # overlapping bits, so the AND read True for every click.
        targets = {self.custom_panel, self.header.custom_button}
        while widget is not None:
            if widget in targets:
                return True
            if widget.window().windowType() == Qt.WindowType.Popup:
                return True
            widget = widget.parentWidget()
        return False

    def _is_topic_safe_click(self, widget: QWidget | None) -> bool:
        targets = {
            self.topic_panel,
            self.header.topic_button_group,
            self.header.detail_button,
            self.header.bookmark_button,
            self.header.thumbnail_button,
        }
        while widget is not None:
            if widget in targets:
                return True
            if widget.window().windowType() == Qt.WindowType.Popup:
                return True
            widget = widget.parentWidget()
        return False

    # --- Custom panel sink (skeleton-only) ------------------------------
    def _handle_enable_custom_changed(self, enabled: bool) -> None:
        logger.debug("novel custom: enable_custom=%s", enabled)
        self.custom_panel.set_enable_custom(enabled, emit=False)
        self.content_area.apply_enable_custom(enabled)

    def _handle_font_size_changed(self, size: int) -> None:
        logger.debug("novel custom: font_size=%s", size)
        self.content_area.apply_font_size(size)

    # --- Auto-hide helpers ----------------------------------------------
    def _show_controls(self, widgets: tuple[QWidget, ...] | None = None, *, reset_timer: bool) -> None:
        self.auto_hide.show(widgets, reset_timer=False)
        if reset_timer:
            self._start_hide_timer_if_allowed()

    def _hide_inactive_controls(self) -> None:
        self.auto_hide.hide_inactive()

    def _control_interaction_active(self) -> bool:
        if self.dialog_overlay.isVisible() or self.footer.is_slider_active():
            return True
        widget = QApplication.widgetAt(QCursor.pos())
        while widget is not None:
            if widget in {
                self.header,
                self.footer,
                self.left_arrow,
                self.right_arrow,
                self.custom_panel,
                self.topic_panel,
                self.dialog_overlay,
            }:
                return True
            widget = widget.parentWidget()
        return False

    def _start_hide_timer_if_allowed(self) -> None:
        if self.dialog_overlay.isVisible():
            return
        self.auto_hide.restart()

    # --- Qt event hooks -------------------------------------------------
    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        rect = self.rect()
        self.content_area.setGeometry(rect)
        self.header.setGeometry(0, 0, self.width(), Theme.reader_banner_height)
        self.footer.setGeometry(
            0,
            self.height() - Theme.reader_footer_height,
            self.width(),
            Theme.reader_footer_height,
        )
        self.left_arrow.setGeometry(
            Theme.reader_side_button_margin,
            (self.height() - Theme.reader_side_button_height) // 2,
            Theme.reader_side_button_width,
            Theme.reader_side_button_height,
        )
        self.right_arrow.setGeometry(
            self.width() - Theme.reader_side_button_margin - Theme.reader_side_button_width,
            (self.height() - Theme.reader_side_button_height) // 2,
            Theme.reader_side_button_width,
            Theme.reader_side_button_height,
        )
        self._position_topic_panel()
        self.dialog_overlay.setGeometry(rect)
        self._position_custom_panel()
        self._raise_panels_if_visible()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), Theme.reader_radius, Theme.reader_radius)
        painter.fillPath(path, QColor(Theme.color_reader_background))
        painter.end()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self.handle_key_press(event):
            return
        super().keyPressEvent(event)

    def handle_key_press(self, event: QKeyEvent) -> bool:
        if event.key() == Qt.Key.Key_Down or event.key() == Qt.Key.Key_PageDown:
            self.content_area.scroll_by_viewport(1)
            event.accept()
            return True
        if event.key() == Qt.Key.Key_Up or event.key() == Qt.Key.Key_PageUp:
            self.content_area.scroll_by_viewport(-1)
            event.accept()
            return True
        if event.key() == Qt.Key.Key_Escape:
            if self.custom_panel.isVisible():
                self._hide_custom_panel()
            elif self.topic_panel.isVisible():
                self._hide_topic_panel()
            elif self._show_back_button:
                self.back_requested.emit()
            else:
                self.window().close()
            event.accept()
            return True
        return False

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched is self.header:
            if event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
                if event.button() == Qt.MouseButton.LeftButton:
                    window = self.window()
                    self._drag_position = event.globalPosition().toPoint() - window.frameGeometry().topLeft()
            if event.type() == QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
                if self._drag_position is not None and event.buttons() & Qt.MouseButton.LeftButton:
                    window = self.window()
                    if not window.isMaximized():
                        window.move(event.globalPosition().toPoint() - self._drag_position)
                    return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self._drag_position = None
        return super().eventFilter(watched, event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.cancel()
        self.panel_filter.deactivate_all()
        super().closeEvent(event)


def _side_button(resources, icon_name: str, parent: QWidget) -> QToolButton:  # noqa: ANN001
    button = QToolButton(parent)
    button.setProperty("class", "ReaderSideButton")
    button.setIcon(QIcon(str(resources.icon_path(icon_name))))
    button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setFixedSize(Theme.reader_side_button_width, Theme.reader_side_button_height)
    return button
