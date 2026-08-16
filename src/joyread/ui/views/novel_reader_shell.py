"""Novel reader shell wiring an EPUB viewmodel to the JoyRead chrome.

Chapter content comes from ``NovelReaderViewModel`` (via
``NovelChapterPayload``) and renders in a ``QTextBrowser``-backed
``NovelContentArea``. The chrome (auto-hide, panels, ESC ladder,
slider↔scroll, edge-reveal) is unchanged from the skeleton; the only
addition is consuming the viewmodel's TOC + bookmarks + progress
signals.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, Signal as QtSignal
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
from joyread.core.reader import ReaderDirection, ReaderProgress
from joyread.infrastructure.i18n.locale_service import t
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.novel_reader_viewmodel import NovelChapterPayload, NovelReaderViewModel
from joyread.ui.viewmodels.reader_viewmodel import ReaderBookmarkItem, ReaderContentsItem
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
        # ``page_count`` here = spine length once the viewmodel loads.
        # Footer slider still tracks scroll-within-chapter; the page
        # indicator shows ``(current_chapter + 1) / chapter_count``.
        self._page_count = 1
        self._current_index = 0
        self._writing_mode_hint_shown = False

        # Resume from the last-saved chapter when the shelf knows about
        # this book. ``page_index`` in the schema stores the spine index
        # for EPUB readers — see plan §7.
        start_index = max(0, int(start_page_index)) if start_page_index is not None else 0
        if start_index == 0 and book is not None and context.library_service is not None:
            try:
                progress: ReaderProgress | None = context.library_service.get_progress(book.uuid)
            except Exception:
                logger.exception("Loading novel progress failed for %s", book.uuid)
                progress = None
            if progress is not None:
                start_index = max(0, int(progress.page_index))

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
        self._sync_title_control_mode()
        context.settings_viewmodel.state_changed.connect(self._sync_title_control_mode)
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
        # Thumbnails mode is novel-irrelevant (no per-chapter previews
        # yet); CONTENTS/BOOKMARKS flip on once the viewmodel populates.
        self.header.set_contents_enabled(False)
        self.header.set_bookmarks_enabled(False)
        self.topic_panel.reset_thumbnails(0)
        # set_page_state would otherwise reset the slider's reading
        # direction to whatever the (hidden) direction switch defaults
        # to; pass TOP_TO_BOTTOM so the filled track stays left-anchored.
        self.footer.set_page_state(self._current_index, self._page_count, ReaderDirection.TOP_TO_BOTTOM)
        self._update_page_indicator()

        logger.info(
            "NovelReaderShellWidget init: path=%s book=%s embedded=%s start_index=%s",
            self._source_path,
            book.uuid if book is not None else None,
            show_back_button,
            start_index,
        )

        self.viewmodel = NovelReaderViewModel(
            context.task_service,
            context.library_service if book is not None else None,
            book_uuid=book.uuid if book is not None else None,
            title=resolved_title,
            initial_spine_index=start_index,
        )

        self._connect_signals()
        self._install_auto_hide()

        # Defer the open by one event-loop tick so the first layout
        # pass sees a real viewport size before the chapter HTML lands.
        from PySide6.QtCore import QTimer

        self._open_timer = QTimer(self)
        self._open_timer.setSingleShot(True)
        self._open_timer.timeout.connect(lambda: self.viewmodel.open_path(self._source_path))
        self._open_timer.start(0)

    def cancel(self) -> None:
        """Tear down before close; symmetric with the manga shell."""
        if hasattr(self, "_open_timer"):
            self._open_timer.stop()
        if hasattr(self, "viewmodel"):
            self.viewmodel.cancel()

    def _connect_signals(self) -> None:
        self.header.back_requested.connect(self.back_requested.emit)
        self.header.mouse_activity.connect(lambda: self._show_controls((self.header,), reset_timer=True))
        self.header.custom_requested.connect(self._toggle_custom_panel)
        self.header.topic_mode_requested.connect(self._show_topic_panel)
        self.footer.mouse_activity.connect(lambda: self._show_controls((self.footer,), reset_timer=True))
        # Footer paddles navigate by chapter (not by viewport scroll —
        # within-chapter scroll is handled by the slider + wheel +
        # keyboard).
        self.footer.start_requested.connect(self._handle_jump_to_start)
        self.footer.previous_requested.connect(self._handle_previous_chapter)
        self.footer.next_requested.connect(self._handle_next_chapter)
        self.footer.end_requested.connect(self._handle_jump_to_end)
        self.footer.seek_requested.connect(self._handle_seek)
        # Side paddles advance / rewind a chapter as well, matching the
        # Figma double-arrow affordance for novel readers.
        self.left_arrow.clicked.connect(self._handle_previous_chapter)
        self.right_arrow.clicked.connect(self._handle_next_chapter)
        # Edge-of-content mouse + right-click wake the chrome — mirrors
        # the manga shell's wiring against ``ReaderCanvas``.
        self.content_area.mouse_moved.connect(self._handle_content_mouse_move)
        self.content_area.left_clicked.connect(self._hide_floating_panels_if_visible)
        self.content_area.right_clicked.connect(lambda: self._show_controls(reset_timer=True))
        # Slider doubles as a scrollbar for the current chapter — both
        # directions feed each other; the re-entrancy guards inside
        # ``NovelContentArea`` and the slider's blockSignals prevent
        # the round-trip from looping.
        self.footer.slider.valueChanged.connect(self._sync_content_from_slider)
        self.content_area.scroll_changed.connect(self._sync_slider_from_content)
        self.custom_panel.enable_custom_changed.connect(self._handle_enable_custom_changed)
        self.custom_panel.font_size_changed.connect(self._handle_font_size_changed)
        self.custom_panel.disable_css_changed.connect(self._handle_disable_css_changed)
        # Topic panel selections all funnel through the viewmodel's
        # spine-indexed seek.
        self.topic_panel.contents_selected.connect(self.viewmodel.seek)
        self.topic_panel.bookmark_selected.connect(self.viewmodel.seek)
        self.topic_panel.new_bookmark_requested.connect(self.viewmodel.add_bookmark)
        self.topic_panel.bookmark_rename_requested.connect(self._show_rename_bookmark_dialog)
        self.topic_panel.bookmark_delete_requested.connect(self.viewmodel.delete_bookmark)
        # ViewModel → shell.
        self.viewmodel.state_changed.connect(self._sync_state_from_viewmodel)
        self.viewmodel.chapter_rendered.connect(self._handle_chapter_rendered)
        self.viewmodel.toc_changed.connect(self._handle_toc_changed)
        self.viewmodel.bookmarks_changed.connect(self._handle_bookmarks_changed)
        self.viewmodel.bookmark_error_changed.connect(
            lambda message: self.dialog_overlay.show_info(t("reader.bookmarks"), message)
        )
        self.viewmodel.progress_changed.connect(self._handle_progress_changed)
        self.viewmodel.error_changed.connect(self._handle_error_changed)
        self.viewmodel.writing_mode_warning.connect(self._handle_writing_mode_warning)

    def _sync_title_control_mode(self) -> None:
        settings_vm = self._context.settings_viewmodel
        self.header.set_title_control_mode(
            force_non_macos_title_controls=bool(settings_vm.inspect_non_native_title_control),
        )

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
        # The slider domain is 0..100 = scroll percentage within the
        # current chapter. The slider value already drove the content
        # area via ``_sync_content_from_slider``; this handler is a
        # no-op kept for future engine wiring (e.g. snap to a TOC
        # anchor on slider release).
        del index

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

    def _handle_previous_chapter(self) -> None:
        if hasattr(self, "viewmodel"):
            self.viewmodel.go_previous()

    def _handle_next_chapter(self) -> None:
        if hasattr(self, "viewmodel"):
            self.viewmodel.go_next()

    def _handle_jump_to_start(self) -> None:
        if hasattr(self, "viewmodel"):
            self.viewmodel.jump_to_start()

    def _handle_jump_to_end(self) -> None:
        if hasattr(self, "viewmodel"):
            self.viewmodel.jump_to_end()

    def _update_page_indicator(self) -> None:
        # Slider runs from 0..100 = scroll-within-current-chapter. The
        # page indicator labels show ``(spine_index + 1) / chapter_count``.
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
        # Thumbnails mode is novel-irrelevant (no per-chapter previews
        # yet); the header keeps it disabled, but defend if reached.
        if mode == ReaderTopicMode.THUMBNAILS:
            self.header.clear_topic_active_mode()
            return
        if mode == ReaderTopicMode.CONTENTS and not self.viewmodel.can_use_contents:
            self.header.clear_topic_active_mode()
            return
        if mode == ReaderTopicMode.BOOKMARKS and not self.viewmodel.can_use_bookmarks:
            self.header.clear_topic_active_mode()
            return
        self._hide_custom_panel()
        self.header.set_topic_active_mode(mode)
        self.topic_panel.set_mode(mode)
        if mode == ReaderTopicMode.BOOKMARKS:
            self.viewmodel.refresh_bookmarks()
        self._position_topic_panel()
        self.topic_panel.show()
        self.topic_panel.raise_()
        self.panel_filter.activate(self.topic_panel)
        self._start_hide_timer_if_allowed()

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

    def _is_custom_safe_click(self, widget: QWidget | None, global_position: QPoint) -> bool:
        # ``windowType()`` returns the WindowType after masking; bitwise
        # AND against the Popup flag is buggy because every Window has
        # overlapping bits, so the AND read True for every click.
        #
        # Geometric guard: see ReaderShellWidget._is_settings_safe_click.
        if QRect(self.custom_panel.mapToGlobal(QPoint(0, 0)), self.custom_panel.size()).contains(global_position):
            return True
        targets = {self.custom_panel, self.header.custom_button}
        while widget is not None:
            if widget in targets:
                return True
            if widget.window().windowType() == Qt.WindowType.Popup:
                return True
            widget = widget.parentWidget()
        return False

    def _is_topic_safe_click(self, widget: QWidget | None, global_position: QPoint) -> bool:
        if QRect(self.topic_panel.mapToGlobal(QPoint(0, 0)), self.topic_panel.size()).contains(global_position):
            return True
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

    # --- Custom panel sink ----------------------------------------------
    def _handle_enable_custom_changed(self, enabled: bool) -> None:
        logger.debug("novel custom: enable_custom=%s", enabled)
        self.custom_panel.set_enable_custom(enabled, emit=False)
        self.content_area.apply_enable_custom(enabled)

    def _handle_font_size_changed(self, size: int) -> None:
        logger.debug("novel custom: font_size=%s", size)
        self.content_area.apply_font_size(size)

    def _handle_disable_css_changed(self, disabled: bool) -> None:
        logger.debug("novel custom: disable_css=%s", disabled)
        self.content_area.apply_disable_css(disabled)

    # --- Viewmodel signal handlers --------------------------------------
    def _sync_state_from_viewmodel(self) -> None:
        self.header.set_title(self.viewmodel.title)
        self._page_count = max(1, self.viewmodel.chapter_count)
        self._current_index = max(0, min(self.viewmodel.current_index, self._page_count - 1))
        self.header.set_bookmarks_enabled(self.viewmodel.can_use_bookmarks)
        self.header.set_contents_enabled(self.viewmodel.can_use_contents)
        self._update_page_indicator()
        if self.viewmodel.is_loading:
            self.content_area.show_error("Opening EPUB…")
        elif self.viewmodel.error_message:
            self.content_area.show_error(self.viewmodel.error_message)

    def _handle_chapter_rendered(self, payload: NovelChapterPayload) -> None:
        self._current_index = payload.spine_index
        self._page_count = max(1, payload.chapter_count)
        self.content_area.set_chapter(payload)
        self._update_page_indicator()
        # Reset slider to top after a chapter swap (scroll-changed will
        # follow once the content settles).
        self.footer.slider.blockSignals(True)
        try:
            self.footer.slider.setValue(0)
        finally:
            self.footer.slider.blockSignals(False)

    def _handle_toc_changed(self, items: tuple[ReaderContentsItem, ...]) -> None:
        self.topic_panel.set_contents(items)
        self.header.set_contents_enabled(bool(items))

    def _handle_bookmarks_changed(self, items: tuple[ReaderBookmarkItem, ...]) -> None:
        self.topic_panel.set_bookmarks(items)
        self.header.set_bookmarks_enabled(self.viewmodel.can_use_bookmarks)

    def _handle_progress_changed(self, book_uuid: str, page_index: int, percent: float) -> None:
        self.progress_changed.emit(book_uuid, page_index, percent)

    def _handle_error_changed(self, message: str | None) -> None:
        if message:
            self.content_area.show_error(message)

    def _handle_writing_mode_warning(self, message: str) -> None:
        if self._writing_mode_hint_shown:
            return
        self._writing_mode_hint_shown = True
        self.dialog_overlay.show_info(t("reader.heads_up"), message)

    def _show_rename_bookmark_dialog(self, bookmark_uuid: str, current_name: str) -> None:
        self.dialog_overlay.show_input(
            t("reader.bookmark_rename_title"),
            t("reader.bookmark_name_header"),
            on_confirm=lambda name, bookmark_uuid=bookmark_uuid: self.viewmodel.rename_bookmark(
                bookmark_uuid, name
            ),
            initial_text=current_name,
            confirm_text=t("reader.bookmark_rename"),
            cancel_text=t("dialog.btn_cancel"),
            validator=lambda value: None if value.strip() else t("reader.bookmark_name_required"),
        )

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
        # Inset the content area by the shell's corner radius on left
        # and right so text glyphs never enter the rounded-corner zone.
        # Top/bottom are covered by the header/footer chrome bands.
        inset = Theme.reader_radius
        self.content_area.setGeometry(
            inset,
            0,
            max(0, self.width() - 2 * inset),
            self.height(),
        )
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
        logger.info(
            "NovelReaderShellWidget close: book=%s",
            getattr(self.viewmodel, "book_uuid", None),
        )
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
