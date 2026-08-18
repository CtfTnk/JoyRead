"""The seam the novel reader is plugged into, and the only thing the app knows about it.

The main application never imports :mod:`joyread.novel`. Instead the
composition root builds one of these -- or does not, when the feature is
switched off -- and hands it to the window manager, which asks it whether a
path is its business and lets it construct its own windows.

Nothing here imports novel code, so this module stays importable whether or
not that package (and its ``lxml`` dependency) is installed at all. The
factories are :class:`Protocol` rather than the ``Callable`` aliases used
elsewhere in this module's neighbours only because the embedded-shell factory
takes keyword arguments, which a ``Callable`` alias cannot express.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from PySide6.QtWidgets import QMainWindow, QWidget

from joyread.app.windows.requests import StandaloneReaderRequest


if TYPE_CHECKING:  # Import cycle: AppContext reaches back into this layer.
    from joyread.app.app_context import AppContext
    from joyread.core.models.book import Book


class NovelWindowFactory(Protocol):
    """Builds a standalone novel reader window for one open request."""

    def __call__(self, context: AppContext, request: StandaloneReaderRequest) -> QMainWindow: ...


class NovelShellFactory(Protocol):
    """Builds an embedded novel reader shell hosted inside Main."""

    def __call__(
        self,
        context: AppContext,
        path: Path,
        *,
        book: Book | None,
        show_back_button: bool,
        start_page_index: int | None,
        parent: QWidget | None,
    ) -> QWidget: ...


@dataclass(frozen=True)
class NovelReaderProvider:
    """Everything the app needs to route a novel format, and nothing more.

    ``extensions`` is the authority on what this provider claims. The app
    tests membership rather than consulting a format constant, so a provider
    that is absent claims nothing and every path falls through to the manga
    and PDF reader -- which is exactly the behaviour when the feature is off.
    """

    extensions: frozenset[str]
    create_window: NovelWindowFactory
    create_embedded_shell: NovelShellFactory
