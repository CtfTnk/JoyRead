"""Reader-domain models and services.

Image-paged formats only (archive and PDF). The novel reader's EPUB parser
and chapter-flow session live in ``joyread.novel`` and are deliberately not
re-exported here: this package is imported by nearly every reader module, so
anything surfaced here becomes a hard dependency of the whole app -- which is
how the EPUB parser and ``lxml`` were previously loaded by every import of
``joyread.core.reader``.
"""

from joyread.core.file_types import SUPPORTED_READER_EXTENSIONS
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
from joyread.core.reader.session_service import ReaderImageSession, ReaderSessionService

__all__ = [
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
