"""Top-level window ViewModel."""

from __future__ import annotations

from joyread.ui.viewmodels.signals import Signal


class MainWindowViewModel:
    def __init__(self) -> None:
        self.title_changed: Signal[str] = Signal()
        self._title = "All"

    @property
    def title(self) -> str:
        return self._title

    def set_title(self, title: str) -> None:
        if title == self._title:
            return
        self._title = title
        self.title_changed.emit(title)
