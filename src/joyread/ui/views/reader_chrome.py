"""Shared chrome controllers for the manga and novel reader shells.

Both reader shells need identical auto-hide of the header/footer/paddles
and identical app-level outside-click closing of popup panels. The two
controllers here encapsulate those behaviours so the shells stay focused
on their content-specific wiring.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QPoint, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget


class AutoHideController(QObject):
    """Hide reader control widgets after inactivity; reveal on cursor proximity.

    The controller installs itself as an event filter on each control widget
    so Enter/MouseMove events transparently restart the hide timer. Shells
    inject an ``interaction_predicate`` and an ``on_after_show`` callback so
    the controller never needs to know shell-specific state (e.g. that a
    settings panel is open and must be re-raised after a show).
    """

    def __init__(
        self,
        owner: QWidget,
        control_widgets: Sequence[QWidget],
        *,
        delay_ms: int,
        interaction_predicate: Callable[[], bool],
        on_after_show: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(owner)
        self._control_widgets: tuple[QWidget, ...] = tuple(control_widgets)
        self._delay_ms = delay_ms
        self._interaction_predicate = interaction_predicate
        self._on_after_show = on_after_show
        self._visible: set[QWidget] = set(self._control_widgets)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._hide_inactive)
        for widget in self._control_widgets:
            widget.installEventFilter(self)

    @property
    def control_widgets(self) -> tuple[QWidget, ...]:
        return self._control_widgets

    def start(self) -> None:
        """Begin the inactivity countdown. Safe to call repeatedly."""
        self._timer.start()

    def restart(self) -> None:
        """Restart the inactivity countdown unconditionally."""
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def is_visible(self, widget: QWidget) -> bool:
        return widget in self._visible

    def show(
        self,
        widgets: Sequence[QWidget] | None = None,
        *,
        reset_timer: bool = True,
    ) -> None:
        targets = self._control_widgets if widgets is None else widgets
        for widget in targets:
            self._set_visible(widget, True)
        if self._on_after_show is not None:
            self._on_after_show()
        if reset_timer:
            self._timer.start()

    def hide_inactive(self) -> None:
        """Public alias for the timer callback (used by tests)."""
        self._hide_inactive()

    def _hide_inactive(self) -> None:
        # The shell knows when an interaction is in flight (slider drag,
        # cursor over a control); defer hiding by one cycle so the user
        # doesn't lose the control mid-gesture.
        if self._interaction_predicate():
            self._timer.start()
            return
        for widget in self._control_widgets:
            self._set_visible(widget, False)

    def _set_visible(self, widget: QWidget, visible: bool) -> None:
        if widget not in self._control_widgets:
            return
        if visible:
            self._visible.add(widget)
            widget.show()
            widget.raise_()
        else:
            self._visible.discard(widget)
            widget.hide()

    def eventFilter(self, watched: object, event: QEvent) -> bool:  # noqa: N802 - Qt API.
        # Reveal a control widget when the cursor enters or moves over it,
        # mirroring native widget hover affordances. Returning False keeps
        # the event flowing to the shell's own filter (header drag-move
        # depends on it).
        if (
            watched in self._control_widgets
            and event.type() in {QEvent.Type.Enter, QEvent.Type.MouseMove}
        ):
            self.show((watched,), reset_timer=True)  # type: ignore[arg-type]
        return False


@dataclass
class _PanelEntry:
    #: Receives the widget under the click and the click's own global
    #: position. The position is passed rather than read from ``QCursor``
    #: because a predicate that samples the cursor answers "where is the
    #: pointer now", which is not the same question once the event has been
    #: queued behind anything.
    safe_click_predicate: Callable[[QWidget | None, QPoint], bool]
    on_outside_click: Callable[[], None]
    active: bool = False


class PanelOutsideClickFilter(QObject):
    """App-level mouse-press filter that hides popup panels on outside clicks.

    Panels register their "safe click" predicate (a click on the panel or
    its trigger button should not dismiss it). The QApplication-level event
    filter is only installed while at least one registered panel is active,
    so the filter has zero cost while all popups are closed.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._registrations: dict[QWidget, _PanelEntry] = {}
        self._installed = False

    def register(
        self,
        panel: QWidget,
        *,
        safe_click_predicate: Callable[[QWidget | None, QPoint], bool],
        on_outside_click: Callable[[], None],
    ) -> None:
        self._registrations[panel] = _PanelEntry(
            safe_click_predicate=safe_click_predicate,
            on_outside_click=on_outside_click,
        )

    def activate(self, panel: QWidget) -> None:
        entry = self._registrations.get(panel)
        if entry is None or entry.active:
            return
        entry.active = True
        self._ensure_installed()

    def deactivate(self, panel: QWidget) -> None:
        entry = self._registrations.get(panel)
        if entry is None or not entry.active:
            return
        entry.active = False
        self._remove_if_unused()

    def deactivate_all(self) -> None:
        for entry in self._registrations.values():
            entry.active = False
        self._remove_if_unused()

    def is_active(self, panel: QWidget) -> bool:
        entry = self._registrations.get(panel)
        return bool(entry and entry.active)

    def eventFilter(self, watched: object, event: QEvent) -> bool:  # noqa: N802 - Qt API.
        if event.type() != QEvent.Type.MouseButtonPress:
            return False
        if not isinstance(event, QMouseEvent):
            return False
        # Classify by where the click happened, not by who received it and not
        # by where the pointer is now. `watched` can be an ancestor or a
        # grabbing widget rather than the thing actually clicked, and
        # `QCursor.pos()` has already moved on if the event waited. Selecting a
        # thumbnail inside a panel could therefore be read as a click outside
        # it, dismissing the panel the user was navigating with.
        global_position = event.globalPosition().toPoint()
        widget = QApplication.widgetAt(global_position)
        if widget is None and isinstance(watched, QWidget):
            widget = watched
        # Snapshot before iteration: an on_outside_click callback hides its
        # panel, which calls deactivate() and mutates the dict.
        for panel, entry in tuple(self._registrations.items()):
            if not entry.active or panel.isHidden():
                continue
            if not entry.safe_click_predicate(widget, global_position):
                entry.on_outside_click()
        return False

    def _ensure_installed(self) -> None:
        if self._installed:
            return
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            self._installed = True

    def _remove_if_unused(self) -> None:
        if any(entry.active for entry in self._registrations.values()):
            return
        if not self._installed:
            return
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._installed = False


def walk_ancestors(widget: QWidget | None) -> "list[QWidget]":
    """Return the widget and each parentWidget(), useful for safe-click checks."""
    chain: list[QWidget] = []
    while widget is not None:
        chain.append(widget)
        widget = widget.parentWidget()
    return chain


__all__ = [
    "AutoHideController",
    "PanelOutsideClickFilter",
    "walk_ancestors",
]
