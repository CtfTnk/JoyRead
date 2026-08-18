"""Plain view-model items both reader kinds hand to the shared chrome.

These live apart from either viewmodel because the widgets that render them
(the topic panel, the password dialog) are shared, and so are the two
viewmodels that produce them. Keeping them here means the novel reader does
not import the manga viewmodel just to name a bookmark, and the topic panel
does not import a viewmodel at all.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReaderPasswordPrompt:
    archive_path: str
    display_name: str
    message: str
    is_retry: bool = False


@dataclass(frozen=True)
class ReaderBookmarkItem:
    uuid: str
    name: str
    page_index: int


@dataclass(frozen=True)
class ReaderContentsItem:
    """One TOC entry surfaced to the topic panel's CONTENTS mode.

    ``page_index`` is an opaque seek target: an archive page index in the
    manga reader and a spine index in the novel reader.
    """

    label: str
    page_index: int
    depth: int = 0
