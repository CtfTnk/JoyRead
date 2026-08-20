"""Shared chrome controller for the manga and novel reader shells.

Both reader shells need identical auto-hide of the header/footer/paddles.
This controller encapsulates that behaviour so the shells stay focused on
their content-specific wiring. Outside-click closing of popup panels is
handled by :class:`~joyread.ui.views.floating_panel_scrim.FloatingPanelScrim`
instead of anything in this module.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QWidget


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


__all__ = [
    "AutoHideController",
]
