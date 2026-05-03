"""Small controller for Figma-style scroll handles that disappear at rest."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QAbstractScrollArea, QScrollBar

from joyread.ui.resources.styles.theme import Theme


class AutoHideScrollHandle(QObject):
    """Reveal a scroll handle during interaction, then hide only the handle.

    The scrollbar itself keeps its fixed width so the shelf/content layout does
    not jump when the handle fades out visually through QSS.
    """

    _visible_property = "scrollHandleVisible"

    def __init__(
        self,
        scroll_area: QAbstractScrollArea,
        parent: QObject | None = None,
        *,
        hide_delay_ms: int = Theme.scrollbar_handle_hide_delay_ms,
    ) -> None:
        super().__init__(parent or scroll_area)
        self._scrollbar = scroll_area.verticalScrollBar()
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(hide_delay_ms)
        self._hide_timer.timeout.connect(self.hide_handle_now)

        self._scrollbar.valueChanged.connect(self._reveal_temporarily)
        self._scrollbar.actionTriggered.connect(lambda _action: self._reveal_temporarily())
        self._scrollbar.rangeChanged.connect(self._sync_range)
        self._scrollbar.sliderPressed.connect(self._reveal_while_dragging)
        self._scrollbar.sliderReleased.connect(self._restart_hide_timer)
        self._scrollbar.installEventFilter(self)
        scroll_area.viewport().installEventFilter(self)

        self.hide_handle_now()

    @property
    def is_handle_visible(self) -> bool:
        return self._scrollbar.property(self._visible_property) == "true"

    def hide_handle_now(self) -> None:
        self._hide_timer.stop()
        self._set_handle_visible(False)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched is self._scrollbar and event.type() in (
            QEvent.Type.Enter,
            QEvent.Type.MouseMove,
            QEvent.Type.HoverMove,
        ):
            self._reveal_temporarily()
        elif event.type() == QEvent.Type.Wheel:
            self._reveal_temporarily()
        return super().eventFilter(watched, event)

    def _sync_range(self, minimum: int, maximum: int) -> None:
        if maximum <= minimum:
            self.hide_handle_now()

    def _reveal_while_dragging(self) -> None:
        if not self._has_scrollable_range():
            return
        self._hide_timer.stop()
        self._set_handle_visible(True)

    def _reveal_temporarily(self) -> None:
        if not self._has_scrollable_range():
            self.hide_handle_now()
            return
        self._set_handle_visible(True)
        self._restart_hide_timer()

    def _restart_hide_timer(self) -> None:
        if self._has_scrollable_range():
            self._hide_timer.start()

    def _has_scrollable_range(self) -> bool:
        return self._scrollbar.maximum() > self._scrollbar.minimum()

    def _set_handle_visible(self, visible: bool) -> None:
        value = "true" if visible else "false"
        if self._scrollbar.property(self._visible_property) == value:
            return
        self._scrollbar.setProperty(self._visible_property, value)
        self._scrollbar.style().unpolish(self._scrollbar)
        self._scrollbar.style().polish(self._scrollbar)
        self._scrollbar.update()
