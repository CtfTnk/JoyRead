"""Canonical file extensions supported by JoyRead readers.

This module deliberately has no Qt or reader-engine imports. Packaging reads
it directly so the macOS document declarations cannot drift from runtime
dispatch support.
"""

from __future__ import annotations


ARCHIVE_EXTENSIONS: frozenset[str] = frozenset(
    {".zip", ".cbz", ".7z", ".cb7", ".rar", ".cbr"}
)
PDF_EXTENSIONS: frozenset[str] = frozenset({".pdf"})
EPUB_EXTENSIONS: frozenset[str] = frozenset({".epub"})
EPUB_ACCESS_ENABLED = False
SUPPORTED_READER_EXTENSIONS: frozenset[str] = (
    ARCHIVE_EXTENSIONS
    | PDF_EXTENSIONS
    | (EPUB_EXTENSIONS if EPUB_ACCESS_ENABLED else frozenset())
)

#: A Hidden Space book sealed to the Library's vault (``joyread.core.vault``).
#: Deliberately *not* in ``SUPPORTED_READER_EXTENSIONS``: a sealed file opens
#: only through the Library that owns its key, so it must never become an OS
#: document type, a launch target or an import candidate.
SEALED_EXTENSION = ".jrl"
