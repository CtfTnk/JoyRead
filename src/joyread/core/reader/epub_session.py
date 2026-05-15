"""Reader-side facade around the EPUB parser.

Mirrors the shape of :mod:`pdf_session` so the session service can
dispatch on suffix without engine knowledge leaking into the
viewmodel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from joyread.core.reader.epub import (
    EpubAssetReader,
    EpubBook,
    EpubSpineItem,
    EpubTocItem,
    InvalidEpubError,
    open_epub,
)


logger = logging.getLogger(__name__)


EPUB_EXTENSIONS: frozenset[str] = frozenset({".epub"})


@dataclass(frozen=True)
class EpubChapter:
    """Decoded chapter payload handed to the content widget."""

    spine_index: int
    href: str  # archive-relative, no anchor
    title: str
    html: str
    language: str | None


class EpubReaderSession:
    """Opens an EPUB and exposes spine + TOC + chapter loading.

    The session owns the underlying :class:`EpubAssetReader` so the
    content widget can resolve ``<img src>`` and ``<link rel="stylesheet">``
    references through the same reader (encryption seam stays intact).
    """

    def __init__(self, book: EpubBook, asset_reader: EpubAssetReader, source_path: Path) -> None:
        self._book = book
        self._asset_reader = asset_reader
        self._source_path = source_path
        # Cache decoded chapter HTML so repeated seeks (e.g. opening the
        # topic panel, picking a bookmark) don't re-decode for free.
        self._chapter_cache: dict[int, EpubChapter] = {}
        # Map spine_index → its TOC label, when one is known. Lets the
        # viewmodel default bookmark names without re-walking the TOC.
        self._chapter_title_by_spine: dict[int, str] = {}
        for item in self._flat_toc():
            if item.spine_index >= 0 and item.spine_index not in self._chapter_title_by_spine:
                self._chapter_title_by_spine[item.spine_index] = item.label
        logger.info(
            "EpubReaderSession ready: path=%s spine=%d toc_resolved=%d",
            source_path,
            len(self.spine),
            len(self._chapter_title_by_spine),
        )

    # --- Public properties --------------------------------------------------

    @property
    def book(self) -> EpubBook:
        return self._book

    @property
    def asset_reader(self) -> EpubAssetReader:
        return self._asset_reader

    @property
    def spine(self) -> tuple[EpubSpineItem, ...]:
        return self._book.spine

    @property
    def toc(self) -> tuple[EpubTocItem, ...]:
        return self._book.toc

    @property
    def chapter_count(self) -> int:
        return len(self._book.spine)

    @property
    def source_path(self) -> Path:
        return self._source_path

    # --- Chapter loading ----------------------------------------------------

    def load_chapter(self, spine_index: int) -> EpubChapter:
        if not 0 <= spine_index < self.chapter_count:
            raise IndexError(f"spine index out of range: {spine_index}")
        cached = self._chapter_cache.get(spine_index)
        if cached is not None:
            return cached
        spine_item = self._book.spine[spine_index]
        raw = self._asset_reader.read(spine_item.href)
        html = _decode_xhtml(raw)
        title = self._chapter_title_by_spine.get(spine_index, "")
        chapter = EpubChapter(
            spine_index=spine_index,
            href=spine_item.href,
            title=title,
            html=html,
            language=self._book.metadata.language,
        )
        self._chapter_cache[spine_index] = chapter
        return chapter

    def chapter_title(self, spine_index: int) -> str:
        return self._chapter_title_by_spine.get(spine_index, "")

    # --- Lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._chapter_cache.clear()
        try:
            self._asset_reader.close()
        except Exception:
            logger.exception("Closing EPUB asset reader failed for %s", self._source_path)

    # --- Helpers ------------------------------------------------------------

    def _flat_toc(self) -> tuple[EpubTocItem, ...]:
        from joyread.core.reader.epub import flatten_toc

        return flatten_toc(self._book.toc)


def open_epub_session(source_path: str | Path) -> EpubReaderSession:
    """Open an EPUB file and return a ready-to-use session."""
    path = Path(source_path)
    try:
        book, reader = open_epub(path)
    except InvalidEpubError:
        raise
    except Exception as exc:
        raise InvalidEpubError(f"Could not open EPUB at {path}: {exc}") from exc
    return EpubReaderSession(book, reader, path)


def _decode_xhtml(data: bytes) -> str:
    """Decode XHTML bytes; tolerate missing/non-UTF-8 declarations.

    XML files are required to be UTF-8 unless an explicit encoding
    declaration says otherwise. We try UTF-8 first, then UTF-8 with
    BOM stripping, then fall back to ISO-8859-1 (which never fails)
    so a malformed chapter doesn't take the whole reader down.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    for encoding in ("utf-8", "utf-8-sig", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    # latin-1 above never raises; this is defensive.
    return data.decode("utf-8", errors="replace")


__all__ = [
    "EPUB_EXTENSIONS",
    "EpubChapter",
    "EpubReaderSession",
    "open_epub_session",
]
