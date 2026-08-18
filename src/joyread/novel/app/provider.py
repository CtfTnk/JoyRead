"""Binds the novel reader's concrete widgets to the app's provider seam.

This is the one module outside the novel feature that imports novel classes,
and the only importer of *it* is the composition root, which does so lazily
and only when the feature is switched on. Keeping the import surface to this
single file is what lets the rest of the app -- and its test suite -- run with
the novel package and ``lxml`` absent entirely.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QMainWindow, QWidget

from joyread.app.windows.novel_provider import NovelReaderProvider
from joyread.app.windows.requests import StandaloneReaderRequest
from joyread.core.file_types import EPUB_EXTENSIONS
from joyread.novel.ui.novel_reader_shell import NovelReaderShellWidget
from joyread.novel.ui.novel_reader_window import NovelReaderWindow


if TYPE_CHECKING:
    from joyread.app.app_context import AppContext
    from joyread.core.models.book import Book


def _create_window(context: AppContext, request: StandaloneReaderRequest) -> QMainWindow:
    return NovelReaderWindow(
        context,
        request.path,
        book=request.book,
        title=request.title,
        start_page_index=request.start_page_index,
    )


def _create_embedded_shell(
    context: AppContext,
    path: Path,
    *,
    book: Book | None,
    show_back_button: bool,
    start_page_index: int | None,
    parent: QWidget | None,
) -> QWidget:
    return NovelReaderShellWidget(
        context,
        path,
        book=book,
        show_back_button=show_back_button,
        start_page_index=start_page_index,
        parent=parent,
    )


def create_novel_reader_provider() -> NovelReaderProvider:
    """The novel reader, described in the only terms the app needs."""

    return NovelReaderProvider(
        extensions=EPUB_EXTENSIONS,
        create_window=_create_window,
        create_embedded_shell=_create_embedded_shell,
    )
