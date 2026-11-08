"""What an archive holds, counted during the scan that opens it.

The Reader shows image pages and silently leaves everything else out. That is
right for a comic with a ``ComicInfo.xml`` beside it and wrong for a folder of
phone photos or a document dump, where the reader would otherwise open on six
stray JPEGs out of a hundred files. The profile is the evidence for telling
those apart: counts only, never names, so it can be logged and kept around.

Counting is a side effect of :class:`ArchiveScanner.scan`, which already looks
at every entry and its suffix. No second listing is made, and a profile costs
one dictionary update per entry.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath


# Sidecars that travel with a book and are never meant to be pages. They are
# left out of the ratio entirely: a scanlation's credits.txt neither helps nor
# hurts the case that this is a book. Junk (``__MACOSX``, ``.DS_Store``) is
# dropped earlier by ``is_junk_entry`` and never reaches the tally.
METADATA_FILE_NAMES = frozenset({"comicinfo.xml", "thumbs.db", "desktop.ini"})
METADATA_SUFFIXES = frozenset(
    {".nfo", ".txt", ".sfv", ".md5", ".sha1", ".sha256", ".url", ".xml", ".json", ".diz"}
)
# How many unsupported file types a profile keeps by name. The dialog shows at
# most three; the rest are folded into one "other files" count.
TOP_SUFFIX_LIMIT = 3


def is_metadata_entry(logical_name: str) -> bool:
    path = PurePosixPath(logical_name)
    return path.name.lower() in METADATA_FILE_NAMES or path.suffix.lower() in METADATA_SUFFIXES


@dataclass(frozen=True)
class ArchiveContentProfile:
    """Counts of what one archive open found, nested archives included.

    ``pages`` are the images the Reader will show. Everything the Reader cannot
    show is split by why:

    * ``other_files``: any other file type, including image formats JoyRead
      cannot decode (HEIC, PSD, AVIF, SVG).
    * ``locked_archives``: nested archives left out because they are
      password-protected.
    * ``skipped_archives``: nested archives left out for any other reason
      (unreadable, nested too deep, inside a sealed book).
    * ``out_of_reach``: pages inside folders deeper than the depth limit.

    A nested archive that opened contributes its own contents and is not
    counted as a file itself.
    """

    pages: int = 0
    other_files: int = 0
    locked_archives: int = 0
    skipped_archives: int = 0
    out_of_reach: int = 0
    metadata_files: int = 0
    # The most common unsupported suffixes, most frequent first, as
    # ``(".heic", 30)``. A file with no suffix is listed as ``""``.
    top_other_suffixes: tuple[tuple[str, int], ...] = ()

    @property
    def unshown(self) -> int:
        return self.other_files + self.locked_archives + self.skipped_archives + self.out_of_reach

    @property
    def countable(self) -> int:
        """Files that count toward the ratio: metadata and junk are left out."""

        return self.pages + self.unshown

    @property
    def shown_fraction(self) -> float:
        return self.pages / self.countable if self.countable else 0.0


@dataclass
class ArchiveContentTally:
    """Mutable counter shared by every level of one recursive scan."""

    pages: int = 0
    locked_archives: int = 0
    skipped_archives: int = 0
    out_of_reach: int = 0
    metadata_files: int = 0
    other_suffixes: Counter[str] = field(default_factory=Counter)

    def add_page(self) -> None:
        self.pages += 1

    def add_out_of_reach_page(self) -> None:
        self.out_of_reach += 1

    def add_locked_archive(self) -> None:
        self.locked_archives += 1

    def add_skipped_archive(self) -> None:
        self.skipped_archives += 1

    def add_file(self, logical_name: str, suffix: str) -> None:
        """Count a file that is neither a page nor an archive the scan opened."""

        if is_metadata_entry(logical_name):
            self.metadata_files += 1
        else:
            self.other_suffixes[suffix] += 1

    def snapshot(self) -> ArchiveContentProfile:
        return ArchiveContentProfile(
            pages=self.pages,
            other_files=sum(self.other_suffixes.values()),
            locked_archives=self.locked_archives,
            skipped_archives=self.skipped_archives,
            out_of_reach=self.out_of_reach,
            metadata_files=self.metadata_files,
            top_other_suffixes=tuple(
                sorted(self.other_suffixes.items(), key=lambda item: (-item[1], item[0]))[
                    :TOP_SUFFIX_LIMIT
                ]
            ),
        )
