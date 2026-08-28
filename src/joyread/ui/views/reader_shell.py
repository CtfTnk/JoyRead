"""Reusable reader shell used by embedded and independent reader hosts."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, QTimer, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QCloseEvent, QCursor, QIcon, QKeyEvent, QMouseEvent, QPainter, QPainterPath, QPaintEvent
from PySide6.QtWidgets import QApplication, QWidget

from joyread.app.reader_page_pipeline import PreparedReaderPage
from joyread.app.reader_document_runtime import ReaderDocumentRuntime
from joyread.app.app_context import AppContext
from joyread.core.models.book import Book
from joyread.core.reader import (
    ReaderDirection,
    ReaderDisplayMode,
    ReaderProgress,
    ReaderSettings,
    ReaderTransitionMode,
)
from joyread.infrastructure.i18n.locale_service import t
from joyread.infrastructure.reader_image_decoder import QtPageFrameDecoder
from joyread.infrastructure.thumbnail_renderer import QtThumbnailRenderer
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.reader_viewmodel import ReaderPasswordPrompt, ReaderViewModel
from joyread.ui.views.floating_panel_scrim import FloatingPanelScrim
from joyread.ui.views.reader_chrome import AutoHideController
from joyread.ui.views.reader_shell_base import ReaderShellBase
from joyread.ui.widgets.dialogs import JoyReadDialogOverlay
from joyread.ui.widgets.path_issue_prompt import PathIssuePromptController
from joyread.ui.widgets.reader_canvas import ReaderCanvas
from joyread.ui.widgets.reader_controls import ReaderFooter, ReaderHeader, ReaderStepButton
from joyread.ui.widgets.reader_settings_panel import ReaderSettingsPanel
from joyread.ui.widgets.reader_topic_panel import ReaderTopicMode, ReaderTopicPanel
from joyread.ui.widgets.window_gestures import SystemMoveGesture


logger = logging.getLogger(__name__)


class ReaderShellWidget(ReaderShellBase):
    """Figma reader surface that can live inside either MainWindow or ReaderWindow."""

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
        self._drag = SystemMoveGesture()
        self._show_back_button = show_back_button
        self._topic_page_count = -1

        self.setObjectName("ReaderRootPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        self.canvas = ReaderCanvas(self)
        self._rapid_navigation_active = False
        self._rapid_navigation_timer = QTimer(self)
        self._rapid_navigation_timer.setSingleShot(True)
        self._rapid_navigation_timer.timeout.connect(self._end_rapid_navigation)
        # Sits above canvas/header/footer/arrows and below whichever floating
        # panel is currently open, so Qt's own hit-testing routes any click
        # that isn't on the panel here instead of through to the content or
        # controls behind it.
        self.panel_scrim = FloatingPanelScrim(self)
        self.panel_scrim.hide()
        self.header = ReaderHeader(context.resources, self)
        self.header.set_back_visible(show_back_button)
        self.panel_scrim.set_drag_handle(self.header)
        self._sync_title_control_mode()
        context.settings_viewmodel.state_changed.connect(self._sync_title_control_mode)
        self.footer = ReaderFooter(context.resources, self)
        self.left_arrow = _side_button(context.resources, "icon_left.svg", self)
        self.right_arrow = _side_button(context.resources, "icon_right.svg", self)
        self.settings_panel = ReaderSettingsPanel(context.resources, self)
        self.settings_panel.hide()
        self.topic_panel = ReaderTopicPanel(context.resources, self)
        self.topic_panel.hide()
        self.dialog_overlay = JoyReadDialogOverlay(self, context.resources, drag_handle=self.header)
        self.dialog_overlay.hide()
        self._path_issue_prompt = PathIssuePromptController(
            self,
            self.dialog_overlay,
            context.path_issue_viewmodel,
        )

        app_settings = context.reload_settings()
        logger.info(
            "ReaderShellWidget init: path=%s book=%s embedded=%s",
            self._source_path,
            book.uuid if book is not None else None,
            show_back_button,
        )
        self.viewmodel = ReaderViewModel(
            ReaderDocumentRuntime(
                context.reader_session_service,
                document_cache_key=(
                    f"file:{book.file_id}" if book is not None and book.file_id else None
                ),
                archive_extraction_cache=context.archive_extraction_pool,
                hash_service=context.hash_service,
                purge_encrypted_cache_on_close=bool(
                    getattr(app_settings, "purge_encrypted_cache_on_close", True)
                ),
            ),
            context.task_service,
            context.cache_service.issue_reader_namespace(),
            context.library_service if book is not None else None,
            book_uuid=book.uuid if book is not None else None,
            title=title or (book.title if book is not None else self._source_path.stem),
            settings=_reader_settings_for_book(context, book),
            progress=_reader_progress_for_book(context, book, start_page_index),
            prefetch_before=context.config.page_prefetch_before,
            prefetch_after=context.config.page_prefetch_after,
            nested_archive_max_depth=app_settings.nested_archive_max_depth,
            archive_global_file_max_depth=app_settings.archive_global_file_max_depth,
            archive_limits=context.settings_viewmodel.archive_open_limits,
            thumbnail_cache_client=context.cache_service.issue_thumbnail_client(),
            archive_warmup_coordinator=context.archive_warmup_coordinator,
            page_decoder=QtPageFrameDecoder(),
            thumbnail_renderer=context.thumbnail_renderer or QtThumbnailRenderer(),
        )
        self.header.set_bookmarks_enabled(self.viewmodel.can_use_bookmarks)
        self.header.set_contents_enabled(self.viewmodel.can_use_contents)
        self._connect_signals()
        self._install_auto_hide()

        # Defer ``open_path`` by one event-loop tick. Constructing the
        # widget tree synchronously starts the open task immediately, but
        # the viewport size is still 0×0 until the first layout pass runs;
        # opening here would compute a layout against bogus dimensions and
        # need to redo it. A 0 ms timer lets Qt finish the initial layout
        # pass so the first archive read sees the real viewport.
        self._open_timer = QTimer(self)
        self._open_timer.setSingleShot(True)
        self._open_timer.timeout.connect(lambda: self.viewmodel.open_path(self._source_path))
        self._open_timer.start(0)

    def cancel(self) -> None:
        if hasattr(self, "_open_timer"):
            self._open_timer.stop()
        self._rapid_navigation_timer.stop()
        self._rapid_navigation_active = False
        self.canvas.clear_pages()
        self.viewmodel.cancel()

    def open_with_password(self, password: str) -> None:
        self.viewmodel.open_path(self._source_path, password=password)

    def skip_password_request(self) -> None:
        self.viewmodel.skip_password_request()

    def _connect_signals(self) -> None:
        self.header.back_requested.connect(self.back_requested.emit)
        self.header.mouse_activity.connect(lambda: self._show_controls((self.header,), reset_timer=True))
        self.footer.mouse_activity.connect(lambda: self._show_controls((self.footer,), reset_timer=True))
        self.footer.start_requested.connect(self._activate_left_outer)
        self.footer.previous_requested.connect(self._activate_left_inner)
        self.footer.next_requested.connect(self._activate_right_inner)
        self.footer.end_requested.connect(self._activate_right_outer)
        self.footer.rapid_navigation_started.connect(self._begin_rapid_navigation)
        self.footer.seek_requested.connect(self.viewmodel.seek)
        self.footer.direction_changed.connect(self._set_reader_direction)
        self.footer.transition_changed.connect(self._set_transition_mode)
        self.footer.spread_shift_requested.connect(self.viewmodel.shift_to_next_index)
        self.footer.settings_requested.connect(self._toggle_settings_panel)
        self.header.topic_mode_requested.connect(self._show_topic_panel)
        self.canvas.mouse_moved.connect(self._handle_canvas_mouse_move)
        self.canvas.left_clicked.connect(self._hide_floating_panels_if_visible)
        self.canvas.right_clicked.connect(lambda: self._show_controls(reset_timer=True))
        self.canvas.wheel_scrolled.connect(self.viewmodel.handle_vertical_scroll)
        self.left_arrow.clicked.connect(self._activate_left_side)
        self.right_arrow.clicked.connect(self._activate_right_side)
        self.left_arrow.rapid_navigation_started.connect(self._begin_rapid_navigation)
        self.right_arrow.rapid_navigation_started.connect(self._begin_rapid_navigation)
        self.topic_panel.thumbnail_interest_changed.connect(self.viewmodel.set_topic_thumbnail_interest)
        self.topic_panel.thumbnail_interest_released.connect(self.viewmodel.release_topic_thumbnail_interest)
        self.topic_panel.thumbnail_selected.connect(self._seek_from_topic_panel)
        self.topic_panel.contents_selected.connect(self._seek_from_topic_panel)
        self.topic_panel.bookmark_selected.connect(self._seek_from_topic_panel)
        self.topic_panel.new_bookmark_requested.connect(self.viewmodel.add_bookmark)
        self.topic_panel.bookmark_rename_requested.connect(self._show_rename_bookmark_dialog)
        self.topic_panel.bookmark_delete_requested.connect(self.viewmodel.delete_bookmark)
        self.settings_panel.custom_enabled_changed.connect(self.viewmodel.set_custom_enabled)
        self.settings_panel.always_one_page_changed.connect(self.viewmodel.set_always_one_page)
        self.settings_panel.fit_mode_changed.connect(self.viewmodel.set_fit_mode)
        self.settings_panel.vertical_custom_enabled_changed.connect(self.viewmodel.set_vertical_custom_enabled)
        self.settings_panel.vertical_fit_width_changed.connect(self.viewmodel.set_vertical_fit_width)
        self.settings_panel.page_spacing_changed.connect(self.viewmodel.set_page_spacing)
        self.settings_panel.zoom_percent_changed.connect(self.viewmodel.set_vertical_zoom_percent)
        self.viewmodel.state_changed.connect(self._sync_state)
        self.viewmodel.layout_changed.connect(self._sync_layout)
        self.viewmodel.page_ready.connect(self._sync_page)
        self.viewmodel.page_failed.connect(self.canvas.set_page_failed)
        self.viewmodel.error_changed.connect(self._show_reader_error)
        self.viewmodel.password_required.connect(self._show_password_dialog)
        self.viewmodel.progress_changed.connect(self._emit_progress_changed)
        self.viewmodel.bookmarks_changed.connect(self.topic_panel.set_bookmarks)
        self.viewmodel.contents_changed.connect(self._handle_contents_changed)
        self.viewmodel.bookmark_error_changed.connect(
            lambda message: self.dialog_overlay.show_info(t("reader.bookmarks"), message)
        )
        self.viewmodel.topic_thumbnail_ready.connect(self.topic_panel.set_thumbnail)

    def _install_auto_hide(self) -> None:
        control_widgets = (self.header, self.footer, self.left_arrow, self.right_arrow)
        # Late-bind both callbacks via lambdas: tests monkeypatch
        # ``_control_interaction_active`` on the shell to force hide
        # behaviour, and the panel-raise helper depends on panel state
        # that changes after the controller is constructed.
        self.auto_hide = AutoHideController(
            self,
            control_widgets,
            delay_ms=Theme.reader_auto_hide_delay_ms,
            interaction_predicate=lambda: self._control_interaction_active(),
            on_after_show=lambda: self._raise_settings_panel_if_visible(),
        )
        # Header still needs the shell as an event filter for window-drag,
        # so install the shell on it directly. Other control widgets only
        # need the auto-hide controller's filter for reveal-on-hover.
        self.header.installEventFilter(self)
        self.auto_hide.start()

    def _sync_state(self) -> None:
        self.header.set_title(self.viewmodel.title)
        self.header.set_bookmarks_enabled(self.viewmodel.can_use_bookmarks)
        self.header.set_contents_enabled(self.viewmodel.can_use_contents)
        if not self.viewmodel.can_use_contents and self.topic_panel.mode == ReaderTopicMode.CONTENTS:
            self._hide_topic_panel()
        if not self.viewmodel.can_use_bookmarks and self.topic_panel.mode == ReaderTopicMode.BOOKMARKS:
            self._hide_topic_panel()
        self.footer.set_page_state(
            self.viewmodel.current_index,
            self.viewmodel.page_count,
            self.viewmodel.settings.direction,
        )
        self.footer.set_direction(self.viewmodel.settings.direction)
        self.footer.set_transition_mode(self.viewmodel.settings.transition_mode)
        self.settings_panel.set_settings(self.viewmodel.settings)
        if self._topic_page_count != self.viewmodel.page_count:
            self._topic_page_count = self.viewmodel.page_count
            self.topic_panel.reset_thumbnails(self.viewmodel.page_count)
        if self.viewmodel.error_message:
            self.canvas.set_status_text(self.viewmodel.error_message)
        elif self.viewmodel.is_loading:
            self.canvas.set_status_text(t("reader.loading"))
        elif self.viewmodel.loading_page_index is not None:
            self.canvas.set_status_text(t("reader.loading_page", index=self.viewmodel.loading_page_index + 1))
        elif self.viewmodel.page_count <= 0:
            self.canvas.set_status_text(t("reader.no_readable_pages"))

    def _sync_layout(self, _result) -> None:  # noqa: ANN001 - signal carries the layout dataclass.
        self.canvas.set_layout_result(self.viewmodel.layout_result, self.viewmodel.pan_x)

    def _sync_page(self, image: PreparedReaderPage) -> None:
        self.canvas.set_page_frame(image)

    def _show_reader_error(self, message: str | None) -> None:
        if message:
            self.canvas.set_status_text(message)

    def _show_password_dialog(self, prompt: ReaderPasswordPrompt) -> None:
        self.dialog_overlay.show_password_input(
            t("reader.archive_password_title"),
            t("reader.archive_password_header", name=prompt.display_name),
            on_confirm=self.open_with_password,
            confirm_text=t("reader.open"),
            cancel_text=t("dialog.btn_cancel"),
            skip_text=t("reader.skip"),
            validator=lambda value: None if value else t("reader.password_required"),
            on_cancel=self.viewmodel.cancel_password_request,
            on_skip=self.skip_password_request,
            detail_text=prompt.archive_path,
            state_prompt=t("reader.password_retry") if prompt.is_retry else None,
        )

    def _toggle_settings_panel(self) -> None:
        if self.settings_panel.isVisible():
            self._hide_settings_panel()
            return
        self._hide_topic_panel()
        self._position_settings_panel()
        self.settings_panel.show()
        self.panel_scrim.set_dismiss_callback(self._hide_settings_panel)
        self.panel_scrim.show()
        self._raise_settings_panel_if_visible()
        self._start_hide_timer_if_allowed()

    def _show_topic_panel(self, mode: ReaderTopicMode) -> None:
        if mode == ReaderTopicMode.CONTENTS and not self.viewmodel.can_use_contents:
            self.header.clear_topic_active_mode()
            return
        if mode == ReaderTopicMode.BOOKMARKS and not self.viewmodel.can_use_bookmarks:
            self.header.clear_topic_active_mode()
            return
        self._hide_settings_panel()
        self.header.set_topic_active_mode(mode)
        self.topic_panel.set_mode(mode)
        if mode == ReaderTopicMode.BOOKMARKS:
            self.viewmodel.refresh_bookmarks()
        self._position_topic_panel()
        self.topic_panel.show()
        self.panel_scrim.set_dismiss_callback(self._hide_topic_panel)
        self.panel_scrim.show()
        self._raise_settings_panel_if_visible()
        self._start_hide_timer_if_allowed()

    def _handle_contents_changed(self, items) -> None:  # noqa: ANN001 - signal carries immutable TOC items.
        self.topic_panel.set_contents(items)
        self.header.set_contents_enabled(bool(items))

    def _handle_canvas_mouse_move(self, position: QPoint) -> None:
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

    def _control_interaction_active(self) -> bool:
        if (
            self.dialog_overlay.isVisible()
            or self.footer.is_slider_active()
            or self.panel_scrim.isVisible()
        ):
            # panel_scrim now sits above header/footer/arrows whenever a
            # floating panel is open, so widgetAt() below would otherwise
            # never resolve to those widgets even while the cursor is
            # sitting right over them -- treat any open panel as active so
            # auto-hide doesn't pull the chrome out from under it.
            return True
        widget = QApplication.widgetAt(QCursor.pos())
        while widget is not None:
            if widget in {
                self.header,
                self.footer,
                self.left_arrow,
                self.right_arrow,
                self.settings_panel,
                self.topic_panel,
                self.dialog_overlay,
            }:
                return True
            widget = widget.parentWidget()
        return False

    def _activate_left_outer(self) -> None:
        if self.viewmodel.is_right_to_left:
            self.viewmodel.jump_to_end()
        else:
            self.viewmodel.jump_to_start()

    def _activate_left_inner(self) -> None:
        if self.viewmodel.is_right_to_left:
            self._navigate_step(self.viewmodel.go_next, is_next=True)
        else:
            self._navigate_step(self.viewmodel.go_previous, is_next=False)

    def _activate_right_inner(self) -> None:
        if self.viewmodel.is_right_to_left:
            self._navigate_step(self.viewmodel.go_previous, is_next=False)
        else:
            self._navigate_step(self.viewmodel.go_next, is_next=True)

    def _activate_right_outer(self) -> None:
        if self.viewmodel.is_right_to_left:
            self.viewmodel.jump_to_start()
        else:
            self.viewmodel.jump_to_end()

    def _emit_progress_changed(self, book_uuid: str, page_index: int, progress_percent: float) -> None:
        self.progress_changed.emit(book_uuid, page_index, progress_percent)

    def _set_reader_direction(self, direction: ReaderDirection) -> None:
        # Direction re-anchors the wide pan, so a glide in flight is heading
        # for an offset the new direction no longer wants.
        self.canvas.cancel_page_slide()
        self.canvas.cancel_pan_slide()
        self.viewmodel.set_direction(direction)

    def _set_transition_mode(self, mode: ReaderTransitionMode) -> None:
        if mode != ReaderTransitionMode.SLIDE:
            self.canvas.cancel_page_slide()
            self.canvas.cancel_pan_slide()
        self.viewmodel.set_transition_mode(mode)

    def _activate_left_side(self) -> None:
        self._navigate_step(
            self.viewmodel.activate_left_side,
            is_next=self.viewmodel.is_right_to_left,
        )

    def _activate_right_side(self) -> None:
        self._navigate_step(
            self.viewmodel.activate_right_side,
            is_next=not self.viewmodel.is_right_to_left,
        )

    def _navigate_horizontal_key(self, side: str, *, rapid: bool) -> None:
        is_next = (
            side == "left"
            if self.viewmodel.is_right_to_left
            else side == "right"
        )
        self._navigate_step(
            lambda: self.viewmodel.handle_horizontal_key(side),
            is_next=is_next,
            rapid=rapid,
        )

    def _navigate_step(
        self,
        action: Callable[[], None],
        *,
        is_next: bool,
        rapid: bool = False,
    ) -> None:
        """Run one logical navigation step and animate only its ready endpoint.

        A step is a page turn or, in wide-pan mode, a pan move within the
        page. Steps arriving inside a rapid burst skip animation, and only a
        step that actually moved may extend the burst's quiet period --
        mashing a boundary must not keep the reader in rapid mode.
        """

        burst = (
            rapid
            or self._rapid_navigation_active
            or self.canvas.is_page_slide_active
            or self.canvas.is_pan_slide_active
        )
        if burst:
            # The user is already ahead of whatever is still moving, so skip
            # it straight to its endpoint. Arming the quiet period waits
            # until we know this step actually went somewhere.
            self.canvas.cancel_page_slide()
            self.canvas.cancel_pan_slide()
        source = None if burst else self._capture_page_slide_source()
        previous_indices = self.viewmodel.current_display_indices
        previous_pan = self.viewmodel.pan_x
        action()
        pages_changed = previous_indices != self.viewmodel.current_display_indices
        # A page turn re-anchors the pan for the new spread, so only treat a
        # pan move as its own step when the spread itself stayed put.
        pan_changed = not pages_changed and previous_pan != self.viewmodel.pan_x
        if not pages_changed and not pan_changed:
            # Blocked navigation (a boundary, a still-loading spread). Mashing
            # against it must not extend the quiet period, so leave any
            # running timer to expire on the last step that actually moved.
            return
        if burst:
            self._begin_rapid_navigation()
            return
        if pan_changed:
            if self.viewmodel.settings.transition_mode == ReaderTransitionMode.SLIDE:
                self.canvas.start_pan_slide(previous_pan)
            return
        if source is None:
            return
        self.canvas.start_page_slide(
            source,
            incoming_from_right=self._slide_incoming_from_right(is_next),
        )

    def _capture_page_slide_source(self):  # noqa: ANN202 - private canvas frame type.
        layout = self.viewmodel.layout_result
        if (
            self._rapid_navigation_active
            or self.viewmodel.settings.transition_mode != ReaderTransitionMode.SLIDE
            or self.viewmodel.settings.direction == ReaderDirection.TOP_TO_BOTTOM
            or layout is None
            or layout.mode == ReaderDisplayMode.WIDE_PAN
        ):
            return None
        return self.canvas.capture_page_slide_frame()

    def _slide_incoming_from_right(self, is_next: bool) -> bool:
        return is_next != self.viewmodel.is_right_to_left

    def _begin_rapid_navigation(self) -> None:
        """Enter (or extend) animation-suppressed navigation.

        The quiet period is measured from this call, so callers reach here
        only for input that actually moved the reader -- restarting it on a
        blocked step would keep a burst alive that the user already ended.
        """

        self._rapid_navigation_active = True
        self.canvas.cancel_page_slide()
        self.canvas.cancel_pan_slide()
        self._rapid_navigation_timer.start(Theme.reader_rapid_navigation_quiet_ms)

    def _end_rapid_navigation(self) -> None:
        self._rapid_navigation_active = False

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        rect = self.rect()
        self.canvas.setGeometry(rect)
        self.panel_scrim.setGeometry(rect)
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
        self._position_settings_panel()
        self._raise_settings_panel_if_visible()
        self.viewmodel.set_viewport_size(
            self.width(),
            self.height(),
            self.devicePixelRatioF(),
        )

    def handle_key_press(self, event: QKeyEvent) -> bool:
        if event.key() == Qt.Key.Key_Left:
            self._navigate_horizontal_key("left", rapid=event.isAutoRepeat())
            event.accept()
            return True
        if event.key() == Qt.Key.Key_Right:
            self._navigate_horizontal_key("right", rapid=event.isAutoRepeat())
            event.accept()
            return True
        if event.key() == Qt.Key.Key_Escape:
            if self.topic_panel.isVisible():
                self._hide_topic_panel()
            elif self.settings_panel.isVisible():
                self._hide_settings_panel()
            elif self._show_back_button:
                self.back_requested.emit()
            else:
                self.window().close()
            event.accept()
            return True
        return False

    def closeEvent(self, event: QCloseEvent) -> None:
        logger.info(
            "ReaderShellWidget close: path=%s book=%s",
            self._source_path,
            getattr(self.viewmodel, "book_uuid", None),
        )
        self.cancel()
        super().closeEvent(event)

    def _position_settings_panel(self) -> None:
        self.settings_panel.setFixedHeight(self.height())
        x = max(0, self.width() - self.settings_panel.width())
        self.settings_panel.move(x, 0)

    def _hide_settings_panel_if_visible(self) -> None:
        if self.settings_panel.isVisible():
            self._hide_settings_panel()

    def _hide_floating_panels_if_visible(self) -> None:
        self._hide_settings_panel_if_visible()
        if self.topic_panel.isVisible():
            self._hide_topic_panel()

    def _hide_settings_panel(self) -> None:
        if self.settings_panel.isHidden():
            return
        self.settings_panel.hide()
        self.panel_scrim.hide()
        self._start_hide_timer_if_allowed()

    def _raise_settings_panel_if_visible(self) -> None:
        # AutoHideController raises revealed chrome controls. Re-raise the
        # scrim first, then the active floating panel, so the scrim keeps
        # receiving every click outside that panel.
        if self.panel_scrim.isVisible():
            self.panel_scrim.raise_()
        if self.settings_panel.isVisible():
            self.settings_panel.raise_()
        if self.topic_panel.isVisible():
            self.topic_panel.raise_()
        if self.dialog_overlay.isVisible():
            self.dialog_overlay.raise_()


def _reader_settings_for_book(context: AppContext, book: Book | None) -> ReaderSettings:
    if book is None:
        return ReaderSettings()
    try:
        return context.library_service.get_reader_settings(book.uuid) or ReaderSettings()
    except Exception as exc:
        # Falling back silently here is how the ``vertical_fit_width`` schema
        # drift hid for so long: bookkeeping read errors made every reload
        # look like fresh defaults. Log loudly before swallowing.
        logger.error(
            "Loading reader settings failed for book=%s; using defaults: %s",
            book.uuid,
            exc,
            exc_info=True,
        )
        return ReaderSettings()


def _reader_progress_for_book(
    context: AppContext,
    book: Book | None,
    start_page_index: int | None = None,
) -> ReaderProgress | None:
    progress: ReaderProgress | None = None
    if book is not None:
        try:
            progress = context.library_service.get_progress(book.uuid)
        except Exception as exc:
            logger.error(
                "Loading reader progress failed for book=%s; ignoring stored progress: %s",
                book.uuid,
                exc,
                exc_info=True,
            )
            progress = None

    if start_page_index is None:
        return progress

    normalized_index = max(0, start_page_index)
    percent = progress.progress_percent if progress is not None else 0.0
    return ReaderProgress(page_index=normalized_index, progress_percent=percent)


def _side_button(resources, icon_name: str, parent: QWidget) -> ReaderStepButton:  # noqa: ANN001
    button = ReaderStepButton(parent)
    button.setProperty("class", "ReaderSideButton")
    button.setIcon(QIcon(str(resources.icon_path(icon_name))))
    button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setFixedSize(Theme.reader_side_button_width, Theme.reader_side_button_height)
    return button
