"""Decide whether an archive opened for reading is mostly something else.

The Reader shows image pages and leaves every other file out without a word.
For a comic that is right. For a folder of phone photos, a PSD artbook or a
document dump zipped together, the reader would open on the handful of JPEGs
it can show and say nothing about the rest. This module turns the archive's
:class:`ArchiveContentProfile` into a warning the Reader asks about before it
shows any page.

The rule is deliberately count-based. Bytes would let one bonus video or a few
PSD sources outweigh a whole book of pages, which is the opposite of what the
reader would conclude by looking at the file list.

Qt-free and stateless apart from :class:`ContentWarningMemory`, so the rule is
testable without a GUI.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Lock

from joyread.core.archive.content_profile import ArchiveContentProfile


# Warn when fewer than half of the countable files are pages...
CONTENT_WARNING_MAX_SHOWN_FRACTION = 0.5
# ...and at least this many are not. One page and one .docx is a 50/50 split
# of almost nothing, and asking about it would be noise.
CONTENT_WARNING_MIN_UNSHOWN = 3
# The dialog names this many groups and folds the rest into one count.
CONTENT_WARNING_MAX_GROUPS = 3


class UnshownKind(StrEnum):
    FILE_TYPE = "file_type"
    LOCKED_ARCHIVES = "locked_archives"
    SKIPPED_ARCHIVES = "skipped_archives"
    OUT_OF_REACH = "out_of_reach"
    OTHER_FILES = "other_files"


@dataclass(frozen=True)
class UnshownGroup:
    """One line of "the rest": a file type, or a reason files were left out."""

    kind: UnshownKind
    count: int
    # Upper-case type without the dot ("HEIC"), for ``FILE_TYPE`` only. Empty
    # for a file with no suffix.
    file_type: str = ""


@dataclass(frozen=True)
class ReaderContentWarning:
    pages: int
    countable: int
    shown_percent: int
    groups: tuple[UnshownGroup, ...]


def assess_archive_content(profile: ArchiveContentProfile | None) -> ReaderContentWarning | None:
    """The warning to show before reading, or ``None`` when the archive is a book.

    An archive with no pages never gets here: the open has already failed as
    empty, which is a better answer than asking whether to read nothing.
    """

    if profile is None or profile.pages <= 0:
        return None
    if profile.unshown < CONTENT_WARNING_MIN_UNSHOWN:
        return None
    if profile.shown_fraction >= CONTENT_WARNING_MAX_SHOWN_FRACTION:
        return None
    return ReaderContentWarning(
        pages=profile.pages,
        countable=profile.countable,
        # Floored, so an archive just under the threshold never reads "50%".
        shown_percent=int(profile.shown_fraction * 100),
        groups=_unshown_groups(profile),
    )


def _unshown_groups(profile: ArchiveContentProfile) -> tuple[UnshownGroup, ...]:
    groups = [
        UnshownGroup(UnshownKind.FILE_TYPE, count, suffix.lstrip(".").upper())
        for suffix, count in profile.top_other_suffixes
    ]
    for kind, count in (
        (UnshownKind.LOCKED_ARCHIVES, profile.locked_archives),
        (UnshownKind.SKIPPED_ARCHIVES, profile.skipped_archives),
        (UnshownKind.OUT_OF_REACH, profile.out_of_reach),
    ):
        if count:
            groups.append(UnshownGroup(kind, count))
    # Stable: ties keep file types ahead of the reasons, in the order above.
    groups.sort(key=lambda group: -group.count)
    shown = groups[:CONTENT_WARNING_MAX_GROUPS]
    # Everything not named above, including file types past the profile's
    # own top list, which ``unshown`` counts but ``top_other_suffixes`` omits.
    rest = profile.unshown - sum(group.count for group in shown)
    if rest > 0:
        shown.append(UnshownGroup(UnshownKind.OTHER_FILES, rest))
    return tuple(shown)


SourceChangeToken = tuple[int, int, int]


class ContentWarningMemory:
    """Files the user chose to read anyway, for the rest of this process.

    Kept in memory only. Writing it to settings would put the paths of files
    the user opened outside the Library on disk, which nothing else does for a
    read-only open. A file that changed since (size, mtime or inode) is a new
    question, so the key carries its change token.
    """

    def __init__(self) -> None:
        self._accepted: set[tuple[str, SourceChangeToken]] = set()
        self._lock = Lock()

    def remember(self, path: Path, token: SourceChangeToken | None) -> None:
        if token is None:
            return
        with self._lock:
            self._accepted.add((str(path), token))

    def was_accepted(self, path: Path, token: SourceChangeToken | None) -> bool:
        if token is None:
            return False
        with self._lock:
            return (str(path), token) in self._accepted
