"""Reusable reader shell used by embedded and independent reader hosts."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, QTimer, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QCloseEvent, QCursor, QIcon, QKeyEvent, QMouseEvent, QPainter, QPainterPath, QPaintEvent
from PySide6.QtWidgets import QApplication, QToolButton, QWidget

from joyread.app.app_context import AppContext
from joyread.core.models.book import Book
from joyread.core.reader import ReaderDirection, ReaderPageImage, ReaderProgress, ReaderSettings
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.reader_viewmodel import ReaderPasswordPrompt, ReaderViewModel
from joyread.ui.widgets.dialogs import JoyReadDialogOverlay
from joyread.ui.widgets.reader_canvas import ReaderCanvas
from joyread.ui.widgets.reader_controls import ReaderFooter, ReaderHeader
from joyread.ui.widgets.reader_settings_panel import ReaderSettingsPanel
from joyread.ui.widgets.reader_topic_panel import ReaderTopicMode, ReaderTopicPanel


logger = logging.getLogger(__name__)


class ReaderShellWidget(QWidget):
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
        self._drag_position: QPoint | None = None
        self._show_back_button = show_back_button
        self._control_widgets: tuple[QWidget, ...] = ()
        self._visible_controls: set[QWidget] = set()
        self._app_event_filter_installed = False
        self._settings_event_filter_installed = False
        self._topic_event_filter_installed = False
        self._topic_page_count = -1

        self.setObjectName("ReaderRootPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        self.canvas = ReaderCanvas(self)
        self.header = ReaderHeader(context.resources, self)
        self.header.set_back_visible(show_back_button)
        self.footer = ReaderFooter(context.resources, self)
        self.left_arrow = _side_button(context.resources, "icon_left.svg", self)
        self.right_arrow = _side_button(context.resources, "icon_right.svg", self)
        self.settings_panel = ReaderSettingsPanel(context.resources, self)
        self.settings_panel.hide()
        self.topic_panel = ReaderTopicPanel(context.resources, self)
        self.topic_panel.hide()
        self.dialog_overlay = JoyReadDialogOverlay(self, context.resources)
        self.dialog_overlay.hide()

        app_settings = context.settings_store.load()
        context.settings = app_settings
        logger.info(
            "ReaderShellWidget init: path=%s book=%s embedded=%s",
            self._source_path,
            book.uuid if book is not None else None,
            show_back_button,
        )
        self.viewmodel = ReaderViewModel(
            context.reader_session_service,
            context.task_service,
            context.cache_service.issue_reader_namespace(),
            context.library_service if book is not None else None,
            book_uuid=book.uuid if book is not None else None,
            title=title or (book.title if book is not None else self._source_path.stem),
            settings=_reader_settings_for_book(context, book),
            progress=_reader_progress_for_book(context, book, start_page_index),
            prefetch_before=context.config.page_prefetch_before,
            prefetch_after=context.config.page_prefetch_after,
            archive_internal_max_depth=app_settings.archive_internal_max_depth,
        )
        self.header.set_bookmarks_enabled(self.viewmodel.can_use_bookmarks)
        self._connect_signals()
        self._install_auto_hide()

        self._open_timer = QTimer(self)
        self._open_timer.setSingleShot(True)
        self._open_timer.timeout.connect(lambda: self.viewmodel.open_path(self._source_path))
        self._open_timer.start(0)

    def cancel(self) -> None:
        if hasattr(self, "_open_timer"):
            self._open_timer.stop()
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
        self.footer.seek_requested.connect(self.viewmodel.seek)
        self.footer.direction_changed.connect(self._set_reader_direction)
        self.footer.transition_changed.connect(self.viewmodel.set_transition_mode)
        self.footer.spread_shift_requested.connect(self.viewmodel.shift_to_next_index)
        self.footer.settings_requested.connect(self._toggle_settings_panel)
        self.header.topic_mode_requested.connect(self._show_topic_panel)
        self.canvas.mouse_moved.connect(self._handle_canvas_mouse_move)
        self.canvas.left_clicked.connect(self._hide_floating_panels_if_visible)
        self.canvas.right_clicked.connect(lambda: self._show_controls(reset_timer=True))
        self.canvas.wheel_scrolled.connect(self.viewmodel.handle_vertical_scroll)
        self.left_arrow.clicked.connect(self.viewmodel.activate_left_side)
        self.right_arrow.clicked.connect(self.viewmodel.activate_right_side)
        self.topic_panel.thumbnail_batch_requested.connect(self.viewmodel.request_topic_thumbnail_batch)
        self.topic_panel.thumbnail_selected.connect(self.viewmodel.seek)
        self.topic_panel.bookmark_selected.connect(self.viewmodel.seek)
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
        self.viewmodel.error_changed.connect(self._show_reader_error)
        self.viewmodel.password_required.connect(self._show_password_dialog)
        self.viewmodel.progress_changed.connect(self._emit_progress_changed)
        self.viewmodel.bookmarks_changed.connect(self.topic_panel.set_bookmarks)
        self.viewmodel.bookmark_error_changed.connect(
            lambda message: self.dialog_overlay.show_info("Bookmarks", message)
        )
        self.viewmodel.topic_thumbnail_batch_ready.connect(self.topic_panel.apply_thumbnail_batch)

    def _install_auto_hide(self) -> None:
        self._control_widgets = (self.header, self.footer, self.left_arrow, self.right_arrow)
        for widget in self._control_widgets:
            self._visible_controls.add(widget)
            widget.installEventFilter(self)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(Theme.reader_auto_hide_delay_ms)
        self._hide_timer.timeout.connect(self._hide_inactive_controls)
        self._hide_timer.start()

    def _sync_state(self) -> None:
        self.header.set_title(self.viewmodel.title)
        self.header.set_bookmarks_enabled(self.viewmodel.can_use_bookmarks)
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
            self.canvas.set_status_text("Loading...")
        elif self.viewmodel.loading_page_index is not None:
            self.canvas.set_status_text(f"Loading page {self.viewmodel.loading_page_index + 1}...")
        elif self.viewmodel.page_count <= 0:
            self.canvas.set_status_text("No readable pages.")

    def _sync_layout(self, _result) -> None:  # noqa: ANN001 - signal carries the layout dataclass.
        self.canvas.set_layout_result(self.viewmodel.layout_result, self.viewmodel.pan_x)

    def _sync_page(self, image: ReaderPageImage) -> None:
        self.canvas.set_page_image(image)

    def _show_reader_error(self, message: str | None) -> None:
        if message:
            self.canvas.set_status_text(message)

    def _show_password_dialog(self, prompt: ReaderPasswordPrompt) -> None:
        self.dialog_overlay.show_password_input(
            "Archive Password",
            f"Password for: {prompt.display_name}",
            on_confirm=self.open_with_password,
            confirm_text="Open",
            cancel_text="Cancel",
            skip_text="Skip",
            validator=lambda value: None if value else "Password cannot be empty.",
            on_cancel=self.viewmodel.cancel_password_request,
            on_skip=self.skip_password_request,
            detail_text=prompt.archive_path,
            state_prompt="Incorrect password. Please try again." if prompt.is_retry else None,
        )

    def _toggle_settings_panel(self) -> None:
        if self.settings_panel.isVisible():
            self._hide_settings_panel()
            return
        self._hide_topic_panel()
        self._position_settings_panel()
        self.settings_panel.show()
        self.settings_panel.raise_()
        self._install_settings_event_filter()
        self._start_hide_timer_if_allowed()

    def _show_topic_panel(self, mode: ReaderTopicMode) -> None:
        if mode == ReaderTopicMode.CONTENTS:
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
        self.topic_panel.raise_()
        self._install_topic_event_filter()
        self._start_hide_timer_if_allowed()

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

    def _show_controls(self, widgets: tuple[QWidget, ...] | None = None, *, reset_timer: bool) -> None:
        target_widgets = widgets or self._control_widgets
        for widget in target_widgets:
            self._set_control_visible(widget, True)
        self._raise_settings_panel_if_visible()
        if reset_timer:
            self._start_hide_timer_if_allowed()

    def _hide_inactive_controls(self) -> None:
        if self._control_interaction_active():
            self._hide_timer.start()
            return
        for widget in self._control_widgets:
            self._set_control_visible(widget, False)

    def _set_control_visible(self, widget: QWidget, visible: bool) -> None:
        if widget not in self._control_widgets:
            return
        if visible:
            self._visible_controls.add(widget)
            widget.show()
            widget.raise_()
            self._raise_settings_panel_if_visible()
        else:
            self._visible_controls.discard(widget)
            widget.hide()

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
                self.settings_panel,
                self.topic_panel,
                self.dialog_overlay,
            }:
                return True
            widget = widget.parentWidget()
        return False

    def _start_hide_timer_if_allowed(self) -> None:
        if self.dialog_overlay.isVisible():
            return
        self._hide_timer.start()

    def _activate_left_outer(self) -> None:
        if self.viewmodel.is_right_to_left:
            self.viewmodel.jump_to_end()
        else:
            self.viewmodel.jump_to_start()

    def _activate_left_inner(self) -> None:
        if self.viewmodel.is_right_to_left:
            self.viewmodel.go_next()
        else:
            self.viewmodel.go_previous()

    def _activate_right_inner(self) -> None:
        if self.viewmodel.is_right_to_left:
            self.viewmodel.go_previous()
        else:
            self.viewmodel.go_next()

    def _activate_right_outer(self) -> None:
        if self.viewmodel.is_right_to_left:
            self.viewmodel.jump_to_start()
        else:
            self.viewmodel.jump_to_end()

    def _emit_progress_changed(self, book_uuid: str, page_index: int, progress_percent: float) -> None:
        self.progress_changed.emit(book_uuid, page_index, progress_percent)

    def _set_reader_direction(self, direction: ReaderDirection) -> None:
        self.viewmodel.set_direction(direction)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        rect = self.rect()
        self.canvas.setGeometry(rect)
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
        self.viewmodel.set_viewport_size(self.width(), self.height())

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
        if event.key() == Qt.Key.Key_Left:
            self.viewmodel.handle_horizontal_key("left")
            event.accept()
            return True
        if event.key() == Qt.Key.Key_Right:
            self.viewmodel.handle_horizontal_key("right")
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

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if (
            self._settings_event_filter_installed
            and self.settings_panel.isVisible()
            and event.type() == QEvent.Type.MouseButtonPress
            and isinstance(event, QMouseEvent)
        ):
            widget = watched if isinstance(watched, QWidget) else QApplication.widgetAt(QCursor.pos())
            if not self._is_settings_safe_click(widget):
                self._hide_settings_panel()
        if (
            self._topic_event_filter_installed
            and self.topic_panel.isVisible()
            and event.type() == QEvent.Type.MouseButtonPress
            and isinstance(event, QMouseEvent)
        ):
            widget = watched if isinstance(watched, QWidget) else QApplication.widgetAt(QCursor.pos())
            if not self._is_topic_safe_click(widget):
                self._hide_topic_panel()
        if watched in self._control_widgets and event.type() in {
            QEvent.Type.Enter,
            QEvent.Type.MouseMove,
        }:
            self._show_controls((watched,), reset_timer=True)  # type: ignore[arg-type]
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
        self._remove_settings_event_filter()
        self._remove_topic_event_filter()
        super().closeEvent(event)

    def _position_settings_panel(self) -> None:
        self.settings_panel.setFixedHeight(self.height())
        x = max(0, self.width() - self.settings_panel.width())
        self.settings_panel.move(x, 0)

    def _position_topic_panel(self) -> None:
        width = min(Theme.reader_topic_panel_width, max(Theme.reader_topic_panel_min_width, self.width() - 32))
        height = min(Theme.reader_topic_panel_height, max(Theme.reader_topic_panel_min_height, self.height() - 32))
        self.topic_panel.setFixedSize(width, height)
        self.topic_panel.move((self.width() - width) // 2, (self.height() - height) // 2)

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
        self._remove_settings_event_filter()
        self._start_hide_timer_if_allowed()

    def _hide_topic_panel(self) -> None:
        if self.topic_panel.isHidden():
            return
        self.topic_panel.hide()
        self.header.clear_topic_active_mode()
        self._remove_topic_event_filter()
        self._start_hide_timer_if_allowed()

    def _raise_settings_panel_if_visible(self) -> None:
        if self.settings_panel.isVisible():
            self.settings_panel.raise_()
        if self.topic_panel.isVisible():
            self.topic_panel.raise_()
        if self.dialog_overlay.isVisible():
            self.dialog_overlay.raise_()

    def _install_settings_event_filter(self) -> None:
        if self._settings_event_filter_installed:
            return
        self._settings_event_filter_installed = True
        self._ensure_app_event_filter()

    def _remove_settings_event_filter(self) -> None:
        if not self._settings_event_filter_installed:
            return
        self._settings_event_filter_installed = False
        self._remove_app_event_filter_if_unused()

    def _install_topic_event_filter(self) -> None:
        if self._topic_event_filter_installed:
            return
        self._topic_event_filter_installed = True
        self._ensure_app_event_filter()

    def _remove_topic_event_filter(self) -> None:
        if not self._topic_event_filter_installed:
            return
        self._topic_event_filter_installed = False
        self._remove_app_event_filter_if_unused()

    def _ensure_app_event_filter(self) -> None:
        if self._app_event_filter_installed:
            return
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            self._app_event_filter_installed = True

    def _remove_app_event_filter_if_unused(self) -> None:
        if self._settings_event_filter_installed or self._topic_event_filter_installed:
            return
        if not self._app_event_filter_installed:
            return
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._app_event_filter_installed = False

    def _is_settings_safe_click(self, widget: QWidget | None) -> bool:
        while widget is not None:
            if widget in {self.settings_panel, self.footer.settings_button}:
                return True
            if widget.window().windowFlags() & Qt.WindowType.Popup:
                return True
            widget = widget.parentWidget()
        return False

    def _is_topic_safe_click(self, widget: QWidget | None) -> bool:
        while widget is not None:
            if widget in {
                self.topic_panel,
                self.header.topic_button_group,
                self.header.detail_button,
                self.header.bookmark_button,
                self.header.thumbnail_button,
            }:
                return True
            if widget.window().windowFlags() & Qt.WindowType.Popup:
                return True
            widget = widget.parentWidget()
        return False

    def _show_rename_bookmark_dialog(self, bookmark_uuid: str, current_name: str) -> None:
        self.dialog_overlay.show_input(
            "Rename Bookmark",
            "Bookmark Name",
            on_confirm=lambda name, bookmark_uuid=bookmark_uuid: self.viewmodel.rename_bookmark(bookmark_uuid, name),
            initial_text=current_name,
            confirm_text="Rename",
            cancel_text="Cancel",
            validator=lambda value: None if value.strip() else "Bookmark name cannot be empty.",
        )


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


def _side_button(resources, icon_name: str, parent: QWidget) -> QToolButton:  # noqa: ANN001
    button = QToolButton(parent)
    button.setProperty("class", "ReaderSideButton")
    button.setIcon(QIcon(str(resources.icon_path(icon_name))))
    button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setFixedSize(Theme.reader_side_button_width, Theme.reader_side_button_height)
    return button
