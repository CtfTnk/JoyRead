"""Independent archive-backed manga reader window."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QPropertyAnimation, QSize, QTimer, Qt
from PySide6.QtGui import QCloseEvent, QIcon, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QMainWindow,
    QToolButton,
    QWidget,
)

from joyread.app.app_context import AppContext
from joyread.core.models.book import Book
from joyread.core.reader import (
    ReaderPageImage,
    ReaderProgress,
    ReaderSettings,
)
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.reader_viewmodel import ReaderViewModel
from joyread.ui.widgets.dialogs import JoyReadDialogOverlay
from joyread.ui.widgets.reader_canvas import ReaderCanvas
from joyread.ui.widgets.reader_controls import ReaderFooter, ReaderHeader
from joyread.ui.widgets.reader_settings_panel import ReaderSettingsPanel


class ReaderWindow(QMainWindow):
    """Frameless Figma reader shell backed by a lazy archive session."""

    def __init__(
        self,
        context: AppContext,
        source_path: str | Path,
        *,
        book: Book | None = None,
        title: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._context = context
        self._source_path = Path(source_path)
        self._drag_position: QPoint | None = None
        self._control_effects: dict[QWidget, QGraphicsOpacityEffect] = {}
        self._control_animations: dict[QWidget, QPropertyAnimation] = {}
        self._controls_visible = True

        self.setObjectName("ReaderWindow")
        self.setWindowTitle(title or (book.title if book is not None else self._source_path.stem))
        self.setWindowIcon(QIcon(str(context.resources.app_icon_path())))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(Theme.reader_width, Theme.reader_height)
        self.setMinimumSize(Theme.reader_min_width, Theme.reader_min_height)

        root = QWidget()
        root.setObjectName("ReaderRootPanel")
        root.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCentralWidget(root)

        self.canvas = ReaderCanvas(root)
        self.header = ReaderHeader(context.resources, root)
        self.footer = ReaderFooter(context.resources, root)
        self.left_arrow = _side_button(context.resources, "icon_left.svg", root)
        self.right_arrow = _side_button(context.resources, "icon_right.svg", root)
        self.settings_panel = ReaderSettingsPanel(root)
        self.settings_panel.hide()
        self.dialog_overlay = JoyReadDialogOverlay(root, context.resources)
        self.dialog_overlay.hide()

        self.viewmodel = ReaderViewModel(
            context.reader_session_service,
            context.task_service,
            context.cache_service,
            context.library_service if book is not None else None,
            book_uuid=book.uuid if book is not None else None,
            title=title or (book.title if book is not None else self._source_path.stem),
            settings=_reader_settings_for_book(context, book),
            progress=_reader_progress_for_book(context, book),
        )
        self._connect_signals()
        self._install_auto_hide()

        QTimer.singleShot(0, lambda: self.viewmodel.open_path(self._source_path))

    def open_with_password(self, password: str) -> None:
        self.viewmodel.open_path(self._source_path, password=password)

    def _connect_signals(self) -> None:
        self.header.back_requested.connect(self.close)
        self.header.mouse_activity.connect(lambda: self._show_controls(reset_timer=True))
        self.footer.mouse_activity.connect(lambda: self._show_controls(reset_timer=True))
        self.footer.start_requested.connect(self.viewmodel.jump_to_start)
        self.footer.previous_requested.connect(self.viewmodel.go_previous)
        self.footer.next_requested.connect(self.viewmodel.go_next)
        self.footer.end_requested.connect(self.viewmodel.jump_to_end)
        self.footer.seek_requested.connect(self.viewmodel.seek)
        self.footer.direction_changed.connect(self.viewmodel.set_direction)
        self.footer.transition_changed.connect(self.viewmodel.set_transition_mode)
        self.footer.spread_shift_requested.connect(self.viewmodel.toggle_spread_offset)
        self.footer.settings_requested.connect(self._toggle_settings_panel)
        self.canvas.mouse_moved.connect(self._handle_canvas_mouse_move)
        self.canvas.right_clicked.connect(lambda: self._show_controls(reset_timer=True))
        self.canvas.left_side_requested.connect(self.viewmodel.activate_left_side)
        self.canvas.right_side_requested.connect(self.viewmodel.activate_right_side)
        self.left_arrow.clicked.connect(self.viewmodel.activate_left_side)
        self.right_arrow.clicked.connect(self.viewmodel.activate_right_side)
        self.settings_panel.custom_enabled_changed.connect(self.viewmodel.set_custom_enabled)
        self.settings_panel.always_one_page_changed.connect(self.viewmodel.set_always_one_page)
        self.settings_panel.fit_mode_changed.connect(self.viewmodel.set_fit_mode)
        self.settings_panel.page_spacing_changed.connect(self.viewmodel.set_page_spacing)
        self.viewmodel.state_changed.connect(self._sync_state)
        self.viewmodel.layout_changed.connect(self._sync_layout)
        self.viewmodel.page_ready.connect(self._sync_page)
        self.viewmodel.error_changed.connect(self._show_reader_error)
        self.viewmodel.password_required.connect(self._show_password_dialog)

    def _install_auto_hide(self) -> None:
        for widget in (self.header, self.footer, self.left_arrow, self.right_arrow):
            effect = QGraphicsOpacityEffect(widget)
            effect.setOpacity(1.0)
            widget.setGraphicsEffect(effect)
            self._control_effects[widget] = effect
            animation = QPropertyAnimation(effect, b"opacity", self)
            animation.setDuration(Theme.reader_fade_duration_ms)
            animation.finished.connect(
                lambda widget=widget, effect=effect: widget.hide()
                if not self._controls_visible and effect.opacity() <= 0.0
                else None
            )
            self._control_animations[widget] = animation

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(Theme.reader_auto_hide_delay_ms)
        self._hide_timer.timeout.connect(lambda: self._set_controls_opacity(0.0))
        self._hide_timer.start()
        self.header.installEventFilter(self)

    def _sync_state(self) -> None:
        self.header.set_title(self.viewmodel.title)
        self.footer.set_page_state(self.viewmodel.current_index, self.viewmodel.page_count)
        self.footer.set_direction(self.viewmodel.settings.direction)
        self.footer.set_transition_mode(self.viewmodel.settings.transition_mode)
        self.footer.set_spread_shifted(bool(self.viewmodel.settings.spread_offset))
        self.settings_panel.set_settings(self.viewmodel.settings)
        if self.viewmodel.error_message:
            self.canvas.set_status_text(self.viewmodel.error_message)
        elif self.viewmodel.is_loading:
            self.canvas.set_status_text("Loading...")
        elif self.viewmodel.page_count <= 0:
            self.canvas.set_status_text("No readable pages.")

    def _sync_layout(self, _result) -> None:  # noqa: ANN001 - signal carries the layout dataclass.
        self.canvas.set_layout_result(self.viewmodel.layout_result, self.viewmodel.pan_x)

    def _sync_page(self, image: ReaderPageImage) -> None:
        self.canvas.set_page_image(image)

    def _show_reader_error(self, message: str | None) -> None:
        if message:
            self.canvas.set_status_text(message)

    def _show_password_dialog(self, message: str) -> None:
        self.dialog_overlay.show_password_input(
            "Archive Password",
            "Password",
            on_confirm=self.open_with_password,
            confirm_text="Open",
            cancel_text="Cancel",
            validator=lambda value: None if value else "Password cannot be empty.",
        )

    def _toggle_settings_panel(self) -> None:
        if self.settings_panel.isVisible():
            self.settings_panel.hide()
            return
        self._position_settings_panel()
        self.settings_panel.show()
        self.settings_panel.raise_()
        self._hide_timer.stop()
        self._show_controls(reset_timer=True)

    def _handle_canvas_mouse_move(self, position: QPoint) -> None:
        edge = Theme.reader_edge_reveal_distance
        if position.y() <= edge or position.y() >= self.height() - edge:
            self._show_controls(reset_timer=True)
            return
        if position.x() <= edge or position.x() >= self.width() - edge:
            self._show_controls(reset_timer=True)

    def _show_controls(self, *, reset_timer: bool) -> None:
        self._set_controls_opacity(1.0)
        if reset_timer and not self.settings_panel.isVisible() and not self.dialog_overlay.isVisible():
            self._hide_timer.start()

    def _set_controls_opacity(self, opacity: float) -> None:
        self._controls_visible = opacity > 0
        for widget, effect in self._control_effects.items():
            if opacity > 0:
                widget.show()
            animation = self._control_animations[widget]
            animation.stop()
            animation.setStartValue(effect.opacity())
            animation.setEndValue(opacity)
            animation.start()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        root = self.centralWidget()
        if root is None:
            return
        rect = root.rect()
        self.canvas.setGeometry(rect)
        self.header.setGeometry(0, 0, root.width(), Theme.reader_banner_height)
        self.footer.setGeometry(
            0,
            root.height() - Theme.reader_footer_height,
            root.width(),
            Theme.reader_footer_height,
        )
        self.left_arrow.setGeometry(
            Theme.reader_side_button_margin,
            (root.height() - Theme.reader_side_button_height) // 2,
            Theme.reader_side_button_width,
            Theme.reader_side_button_height,
        )
        self.right_arrow.setGeometry(
            root.width() - Theme.reader_side_button_margin - Theme.reader_side_button_width,
            (root.height() - Theme.reader_side_button_height) // 2,
            Theme.reader_side_button_width,
            Theme.reader_side_button_height,
        )
        self.dialog_overlay.setGeometry(rect)
        self._position_settings_panel()
        self.viewmodel.set_viewport_size(root.width(), root.height())

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Left:
            self.viewmodel.handle_horizontal_key("left")
            event.accept()
            return
        if event.key() == Qt.Key.Key_Right:
            self.viewmodel.handle_horizontal_key("right")
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            if self.settings_panel.isVisible():
                self.settings_panel.hide()
            else:
                self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched is self.header:
            if event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
                if event.button() == Qt.MouseButton.LeftButton:
                    self._drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            if event.type() == QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
                if self._drag_position is not None and event.buttons() & Qt.MouseButton.LeftButton:
                    if not self.isMaximized():
                        self.move(event.globalPosition().toPoint() - self._drag_position)
                    return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self._drag_position = None
        return super().eventFilter(watched, event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.viewmodel.cancel()
        super().closeEvent(event)

    def _position_settings_panel(self) -> None:
        if self.centralWidget() is None:
            return
        self.settings_panel.adjustSize()
        x = max(0, (self.width() - self.settings_panel.width()) // 2)
        y = max(0, (self.height() - self.settings_panel.height()) // 2)
        self.settings_panel.move(x, y)


def _reader_settings_for_book(context: AppContext, book: Book | None) -> ReaderSettings:
    if book is None:
        return ReaderSettings()
    try:
        return context.library_service.get_reader_settings(book.uuid) or ReaderSettings()
    except Exception:
        return ReaderSettings()


def _reader_progress_for_book(context: AppContext, book: Book | None) -> ReaderProgress | None:
    if book is None:
        return None
    try:
        return context.library_service.get_progress(book.uuid)
    except Exception:
        return None

def _side_button(resources, icon_name: str, parent: QWidget) -> QToolButton:  # noqa: ANN001
    button = QToolButton(parent)
    button.setProperty("class", "ReaderSideButton")
    button.setIcon(QIcon(str(resources.icon_path(icon_name))))
    button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setFixedSize(Theme.reader_side_button_width, Theme.reader_side_button_height)
    return button
