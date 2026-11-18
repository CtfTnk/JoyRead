"""Retire child widgets when rebuilding a Qt layout."""

from PySide6.QtWidgets import QLayout, QWidget


def detach_and_hide(widget: QWidget) -> None:
    """Remove a retired widget from active child lookups before disposal or reuse."""
    # Reparenting hides the widget implicitly. An explicit hide after detaching
    # prevents a queued layout show from displaying it as a top-level window.
    widget.setParent(None)
    widget.hide()


def clear_widget_layout(layout: QLayout) -> None:
    """Remove rows and spacers, deferring widget deletion past active callbacks."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            detach_and_hide(widget)
            widget.deleteLater()
