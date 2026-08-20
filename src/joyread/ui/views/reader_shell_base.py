"""Behaviour the manga and novel reader shells share verbatim.

The two shells are different readers -- one pages images, the other flows
chapters -- but their chrome behaves the same: the same auto-hide rules, the
same centred topic panel, the same frameless-window drag, the same rename
dialog. That part was copy-pasted, and it drifted: a fix to the topic panel's
dismiss-on-select behaviour had to be written twice, and a locale test had to
assert on *both* shells to catch the copies diverging.

Everything here was lifted verbatim from ``ReaderShellWidget`` after
confirming, method by method, that the two copies were identical (nine byte
for byte; ``_seek_from_topic_panel`` and ``_show_rename_bookmark_dialog``
differed only in docstring wording and line wrapping). Methods that genuinely
differ between the shells -- construction, signal wiring, panel policy, key
handling, cancellation -- deliberately stay with each shell.

Subclasses must provide, by the time these methods can run:

``_context``           the :class:`AppContext`, for ``settings_viewmodel``
``_drag_position``     ``QPoint | None``, initialised to ``None``
``auto_hide``          an :class:`~joyread.ui.views.reader_chrome.AutoHideController`
``dialog_overlay``     the overlay used for input dialogs
``header``             the reader header, with ``clear_topic_active_mode()``
                       and ``set_title_control_mode()``
``panel_scrim``        a :class:`~joyread.ui.views.floating_panel_scrim.FloatingPanelScrim`
                       shared by every floating panel in the shell
``topic_panel``        the contents/bookmarks/thumbnails panel
``viewmodel``          exposing ``seek()`` and ``rename_bookmark()``
``handle_key_press``   returning ``True`` when it consumed the event
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPainterPath, QPaintEvent
from PySide6.QtWidgets import QWidget

from joyread.infrastructure.i18n.locale_service import t
from joyread.ui.resources.styles.theme import Theme


class ReaderShellBase(QWidget):
    """Shared chrome for both reader shells.

    A base class rather than a controller because three of these are Qt
    virtuals (``eventFilter``, ``keyPressEvent``, ``paintEvent``) that Qt
    dispatches to the widget itself; a helper object could not receive them
    without each shell forwarding by hand, which is the duplication being
    removed.
    """

    def _show_controls(self, widgets: tuple[QWidget, ...] | None = None, *, reset_timer: bool) -> None:
        self.auto_hide.show(widgets, reset_timer=False)
        if reset_timer:
            self._start_hide_timer_if_allowed()

    def _hide_inactive_controls(self) -> None:
        self.auto_hide.hide_inactive()

    def _start_hide_timer_if_allowed(self) -> None:
        if self.dialog_overlay.isVisible():
            return
        self.auto_hide.restart()

    def _sync_title_control_mode(self) -> None:
        settings_vm = self._context.settings_viewmodel
        self.header.set_title_control_mode(
            force_non_macos_title_controls=bool(settings_vm.inspect_non_native_title_control),
        )

    def _position_topic_panel(self) -> None:
        width = min(Theme.reader_topic_panel_width, max(Theme.reader_topic_panel_min_width, self.width() - 32))
        height = min(Theme.reader_topic_panel_height, max(Theme.reader_topic_panel_min_height, self.height() - 32))
        self.topic_panel.setFixedSize(width, height)
        self.topic_panel.move((self.width() - width) // 2, (self.height() - height) // 2)

    def _hide_topic_panel(self) -> None:
        if self.topic_panel.isHidden():
            return
        self.topic_panel.hide()
        self.header.clear_topic_active_mode()
        self.panel_scrim.hide()
        self._start_hide_timer_if_allowed()

    def _seek_from_topic_panel(self, page_index: int) -> None:
        """Navigate, then dismiss the panel that asked for it.

        Every mode of this panel is a way of saying "take me there", so the
        panel has done its job once it has; leaving it up covers the page the
        user just chose. Hiding happens after the seek so the panel's own
        selection handling finishes first.
        """

        self.viewmodel.seek(page_index)
        self._hide_topic_panel()

    def _show_rename_bookmark_dialog(self, bookmark_uuid: str, current_name: str) -> None:
        self.dialog_overlay.show_input(
            t("reader.bookmark_rename_title"),
            t("reader.bookmark_name_header"),
            on_confirm=lambda name, bookmark_uuid=bookmark_uuid: self.viewmodel.rename_bookmark(bookmark_uuid, name),
            initial_text=current_name,
            confirm_text=t("reader.bookmark_rename"),
            cancel_text=t("dialog.btn_cancel"),
            validator=lambda value: None if value.strip() else t("reader.bookmark_name_required"),
        )

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

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self.handle_key_press(event):
            return
        super().keyPressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), Theme.reader_radius, Theme.reader_radius)
        painter.fillPath(path, QColor(Theme.color_reader_background))
        painter.end()
