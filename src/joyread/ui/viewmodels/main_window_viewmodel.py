"""Top-level window ViewModel."""

from __future__ import annotations

import logging

from joyread.ui.viewmodels.signals import Signal


logger = logging.getLogger(__name__)


class MainWindowViewModel:
    """Tracks the main window's current title and notifies the view when it
    changes. Owned by :class:`MainWindow` and updated whenever the user
    switches the shelf section (All / Recent / a collection / a tag).
    """

    def __init__(self) -> None:
        self.title_changed: Signal[str] = Signal("main_window.title_changed")
        self._title = "All"

    @property
    def title(self) -> str:
        return self._title

    def set_title(self, title: str) -> None:
        if title == self._title:
            return
        logger.debug("MainWindowViewModel title %r -> %r", self._title, title)
        self._title = title
        self.title_changed.emit(title)
