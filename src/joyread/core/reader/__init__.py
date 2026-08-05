"""Reader-domain models and services."""

from joyread.core.reader.epub import (
    EpubAssetReader,
    EpubBook,
    EpubMetadata,
    EpubPasswordRequired,
    EpubSpineItem,
    EpubTocItem,
    flatten_toc,
    open_epub,
)
from joyread.core.reader.epub_session import (
    EPUB_EXTENSIONS,
    EpubChapter,
    EpubReaderSession,
    open_epub_session,
)
from joyread.core.reader.layout import SmartLayoutEngine
from joyread.core.reader.models import (
    PageDraw,
    ReaderDirection,
    ReaderDisplayMode,
    ReaderFitMode,
    ReaderLayoutResult,
    ReaderLayoutSettings,
    ReaderPageImage,
    ReaderProgress,
    ReaderSettings,
    ReaderTransitionMode,
    RectF,
    SizeF,
)
from joyread.core.reader.session_service import ReaderImageSession, ReaderSessionService, SUPPORTED_READER_EXTENSIONS

__all__ = [
    "EPUB_EXTENSIONS",
    "EpubAssetReader",
    "EpubBook",
    "EpubChapter",
    "EpubMetadata",
    "EpubPasswordRequired",
    "EpubReaderSession",
    "EpubSpineItem",
    "EpubTocItem",
    "flatten_toc",
    "open_epub",
    "open_epub_session",
    "PageDraw",
    "ReaderDirection",
    "ReaderDisplayMode",
    "ReaderFitMode",
    "ReaderLayoutResult",
    "ReaderLayoutSettings",
    "ReaderPageImage",
    "ReaderProgress",
    "ReaderSettings",
    "ReaderImageSession",
    "ReaderSessionService",
    "ReaderTransitionMode",
    "RectF",
    "SizeF",
    "SmartLayoutEngine",
    "SUPPORTED_READER_EXTENSIONS",
]
