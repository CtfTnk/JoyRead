"""Import gate: prove a whole archive tree is usable before it enters the library.

:func:`ArchiveImageService.probe_archive` answers a cheaper question -- can this
one container be listed -- and is deliberately shallow. That is enough to decide
whether a reader may *try* a file, because the reader can ask for a password and
can survive a broken branch. It is not enough to decide what the library *keeps*:

* an unencrypted archive can hold an encrypted one, so a shallow probe accepts a
  file that later demands a password mid-read;
* the scanner skips a nested archive it cannot read (``scanner.py``, "Skipping
  unreadable nested archive"), which is right for reading -- show the pages that
  do work -- and wrong for importing, where the skipped branch silently becomes
  pages the library does not have.

So this module walks the whole tree once, under the configured limits, and
answers a stricter question: *is every container in here readable without a
password, and did we see all of it?* Anything short of yes is a rejection with a
reason, not a partial success.

Two rules follow from that and are load-bearing:

* **It never asks for a password.** There is no provider parameter to pass, so
  encryption can only ever be reported. An import that prompted would be asking
  the user to unlock something before they have chosen to keep it.
* **A limit is a rejection, not a truncation.** A bounded walk cannot prove an
  unbounded tree is clean, so reaching a configured limit means we did not see
  the whole document and must not claim we did.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
import logging
from pathlib import PurePosixPath
from typing import Callable

from joyread.core.archive.errors import (
    ArchiveDependencyMissing,
    ArchiveError,
    ArchivePasswordRejected,
    ArchivePasswordRequired,
    ArchiveResourceLimitError,
)
from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget, ensure_item_size
from joyread.core.archive.records import ArchiveContainerProbe, ArchiveSource
from joyread.core.archive.scanner import IMAGE_EXTENSIONS
from joyread.core.archive.tree import is_junk_entry, safe_entry_name
from joyread.core.file_types import ARCHIVE_EXTENSIONS


logger = logging.getLogger(__name__)

#: Entry basenames carrying document metadata, matched case-insensitively.
METADATA_ENTRY_NAMES = frozenset({"meta.json", "comicinfo.xml"})

#: Metadata is a small sidecar. Skipping an oversized one must never fail an
#: otherwise good import, so every cap here drops the sidecar and reads on.
#:
#: The per-entry cap is enforced twice on purpose. The declared size in an
#: archive listing is attacker-controlled and often simply absent (7Z and RAR
#: listings frequently report ``None``), so trusting it alone let a crafted
#: entry be read under the general 1 GiB item limit and then held in the
#: result forever.
METADATA_MAX_BYTES = 1 * 1024 * 1024

#: ...and per-entry caps alone still bound nothing: a legal archive may hold
#: any number of ``folder-N/meta.json`` sidecars. Nested payloads are freed as
#: the recursion unwinds, but anything kept in the result is resident for the
#: whole import, so the collection is bounded in total as well as per item.
METADATA_TOTAL_MAX_BYTES = 8 * 1024 * 1024
METADATA_MAX_ENTRIES = 64


class ImportRejection(StrEnum):
    """Why a source may not enter the library.

    Deliberately coarse. The value reaches the user through an import result
    row, so it names the *class* of problem and never the offending path's
    contents, an entry listing, or anything password-shaped.
    """

    ENCRYPTED_ROOT = "encrypted_root"
    ENCRYPTED_NESTED = "encrypted_nested"
    MALFORMED_ROOT = "malformed_root"
    MALFORMED_CHILD = "malformed_child"
    LIMIT_EXCEEDED = "limit_exceeded"
    DEPENDENCY_MISSING = "dependency_missing"
    EMPTY = "empty"


@dataclass(frozen=True)
class ArchiveMetadataEntry:
    """One metadata sidecar found during the walk, with its bytes already read.

    Read here rather than in a later pass because the walk has the container
    open and the budget charged; re-opening a nested archive to fetch a 2 KB
    file would mean materializing its parent chain a second time.
    """

    container: str
    #: Full POSIX path inside *container*, directories included. The directory
    #: half is what separates a book-level sidecar from a chapter's own, so it
    #: cannot be reduced to the basename.
    path: str
    #: Basename of :attr:`path`, which is what names the *kind* of sidecar.
    name: str
    data: bytes


@dataclass(frozen=True)
class ArchiveImportInspection:
    """The verdict, plus what the walk saw on the way to it."""

    accepted: bool
    message: str
    rejection: ImportRejection | None = None
    #: Display name of the container that caused a rejection. Never a password,
    #: and for nested containers a ``parent::child`` label rather than a path.
    rejected_at: str | None = None
    image_count: int = 0
    nested_archive_count: int = 0
    deepest_nesting: int = 0
    metadata_entries: tuple[ArchiveMetadataEntry, ...] = ()


class _Rejected(Exception):
    """Internal control flow: unwind the walk with a verdict."""

    def __init__(self, rejection: ImportRejection, message: str, at: str | None) -> None:
        super().__init__(message)
        self.rejection = rejection
        self.message = message
        self.at = at


@dataclass
class _WalkState:
    image_count: int = 0
    nested_archive_count: int = 0
    deepest_nesting: int = 0
    metadata: list[ArchiveMetadataEntry] = field(default_factory=list)
    metadata_bytes: int = 0
    metadata_capped: bool = False


ProbeEntries = Callable[[ArchiveSource], ArchiveContainerProbe]
ReadEntry = Callable[..., bytes]


class ArchiveImportInspector:
    """Walk every container in one source tree and decide if it may be imported.

    Takes the two backend operations as callables, the same shape
    :class:`~joyread.core.archive.scanner.ArchiveScanner` uses, so the walk can
    be tested against fakes without a real archive or the service facade.
    """

    def __init__(self, probe_entries: ProbeEntries, read_entry: ReadEntry) -> None:
        self._probe_entries = probe_entries
        self._read_entry = read_entry

    def inspect(
        self,
        source: ArchiveSource,
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> ArchiveImportInspection:
        state = _WalkState()
        try:
            self._walk(source, limits=limits, budget=budget, state=state, nested_depth=0, global_base=0)
        except _Rejected as rejected:
            logger.info(
                "Import inspection rejected the source",
                extra={
                    "event": "archive.inspect.rejected",
                    "category": "archive",
                    "status": "rejected",
                    "error_code": rejected.rejection.value,
                },
            )
            return ArchiveImportInspection(
                accepted=False,
                message=rejected.message,
                rejection=rejected.rejection,
                rejected_at=rejected.at,
            )

        if state.image_count == 0:
            return ArchiveImportInspection(
                accepted=False,
                message=f"No supported image pages found in {source.display_name}.",
                rejection=ImportRejection.EMPTY,
                rejected_at=source.display_name,
            )
        return ArchiveImportInspection(
            accepted=True,
            message="Every container in this archive is readable without a password.",
            image_count=state.image_count,
            nested_archive_count=state.nested_archive_count,
            deepest_nesting=state.deepest_nesting,
            metadata_entries=tuple(state.metadata),
        )

    # ------------------------------------------------------------------
    # Walk
    # ------------------------------------------------------------------

    def _walk(
        self,
        source: ArchiveSource,
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
        state: _WalkState,
        nested_depth: int,
        global_base: int,
    ) -> None:
        probe = self._probe_container(source, nested_depth)
        if probe.is_encrypted:
            raise _Rejected(
                ImportRejection.ENCRYPTED_ROOT
                if nested_depth == 0
                else ImportRejection.ENCRYPTED_NESTED,
                f"Password-protected archive cannot be imported: {source.display_name}",
                source.display_name,
            )
        state.deepest_nesting = max(state.deepest_nesting, nested_depth)

        for entry in probe.entries:
            safe_name = safe_entry_name(entry.name)
            if safe_name is None or is_junk_entry(safe_name):
                continue
            parts = PurePosixPath(safe_name).parts
            suffix = PurePosixPath(safe_name).suffix.lower()
            entry_depth = global_base + max(0, len(parts) - 1)

            if suffix in IMAGE_EXTENSIONS:
                self._accept_image(entry, safe_name, entry_depth, limits, state, source)
            elif suffix in ARCHIVE_EXTENSIONS:
                self._descend(
                    entry,
                    safe_name,
                    entry_depth,
                    limits=limits,
                    budget=budget,
                    state=state,
                    parent=source,
                    nested_depth=nested_depth,
                )
            elif parts[-1].casefold() in METADATA_ENTRY_NAMES:
                self._collect_metadata(entry, safe_name, source, limits, budget, state)

    def _accept_image(
        self,
        entry: object,
        safe_name: str,
        entry_depth: int,
        limits: ArchiveOpenLimits,
        state: _WalkState,
        source: ArchiveSource,
    ) -> None:
        # The scanner drops an over-deep page and carries on. Importing cannot:
        # a dropped page is one the library will never have.
        _require_depth(
            entry_depth,
            limits.global_file_max_depth,
            limit="global_file_max_depth",
            subject=safe_name,
            at=source.display_name,
        )
        try:
            ensure_item_size(
                getattr(entry, "size", None), limits.max_extracted_item_bytes, safe_name
            )
        except ArchiveResourceLimitError as exc:
            raise _limit_rejection(exc, source.display_name) from exc
        state.image_count += 1

    def _descend(
        self,
        entry: object,
        safe_name: str,
        entry_depth: int,
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
        state: _WalkState,
        parent: ArchiveSource,
        nested_depth: int,
    ) -> None:
        next_nested = nested_depth + 1
        _require_depth(
            next_nested,
            limits.nested_archive_max_depth,
            limit="nested_archive_max_depth",
            subject=safe_name,
            at=parent.display_name,
        )
        _require_depth(
            entry_depth + 1,
            limits.global_file_max_depth,
            limit="global_file_max_depth",
            subject=safe_name,
            at=parent.display_name,
        )
        label = f"{parent.label}::{safe_name}"
        try:
            ensure_item_size(
                getattr(entry, "size", None), limits.max_extracted_item_bytes, safe_name
            )
            # ``None`` password: the parent was proven unencrypted above, so no
            # entry inside it can require one. This is why the walk can never
            # reach the password machinery.
            data = self._read_entry(
                parent,
                safe_name,
                None,
                limits=limits,
                budget=budget,
            )
        except ArchiveResourceLimitError as exc:
            raise _limit_rejection(exc, label) from exc
        except (ArchivePasswordRequired, ArchivePasswordRejected) as exc:
            raise _Rejected(
                ImportRejection.ENCRYPTED_NESTED,
                f"Password-protected archive cannot be imported: {label}",
                label,
            ) from exc
        except ArchiveDependencyMissing as exc:
            raise _Rejected(ImportRejection.DEPENDENCY_MISSING, str(exc), label) from exc
        except ArchiveError as exc:
            raise _Rejected(
                ImportRejection.MALFORMED_CHILD,
                f"Unreadable archive inside this file: {label} ({exc})",
                label,
            ) from exc

        state.nested_archive_count += 1
        nested_source = ArchiveSource(label=label, suffix=PurePosixPath(safe_name).suffix.lower(), data=data)
        self._walk(
            nested_source,
            limits=limits,
            budget=budget,
            state=state,
            nested_depth=next_nested,
            global_base=entry_depth + 1,
        )

    def _collect_metadata(
        self,
        entry: object,
        safe_name: str,
        source: ArchiveSource,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
        state: _WalkState,
    ) -> None:
        # What is left of the total budget, not merely whether any is left. A
        # "still under the cap" test lets the final sidecar carry a whole extra
        # item past it, so the cap has to be arithmetic on this read.
        remaining = METADATA_TOTAL_MAX_BYTES - state.metadata_bytes
        if len(state.metadata) >= METADATA_MAX_ENTRIES or remaining <= 0:
            self._note_metadata_cap(state)
            return
        allowance = min(METADATA_MAX_BYTES, remaining)
        declared = getattr(entry, "size", None)
        if declared is not None and declared > allowance:
            self._note_metadata_cap(state)
            return
        # Read under a metadata-sized item limit rather than the archive-wide
        # one, so a listing that under-declares (or declares nothing) cannot
        # get a huge payload materialized in the first place.
        metadata_limits = replace(
            limits,
            max_extracted_item_bytes=min(
                allowance,
                limits.max_extracted_item_bytes or allowance,
            ),
        )
        try:
            data = self._read_entry(
                source, safe_name, None, limits=metadata_limits, budget=budget
            )
        except ArchiveResourceLimitError:
            # The backend enforced the cap above. That is a sidecar we skip,
            # never a reason to refuse the book.
            self._note_metadata_cap(state)
            return
        except ArchiveError as exc:
            # Metadata is an enhancement. A sidecar this archive cannot produce
            # must not cost the user an import that is otherwise perfectly good.
            logger.info(
                "Could not read metadata entry; continuing without it",
                extra={
                    "event": "archive.inspect.metadata_unreadable",
                    "category": "archive",
                    "status": "skipped",
                    "error_type": type(exc).__name__,
                },
            )
            return
        # Backstop: a backend that does not honour the item limit must still
        # not be able to park an unbounded payload in the result, nor push the
        # running total past the cap.
        if len(data) > allowance:
            self._note_metadata_cap(state)
            return
        state.metadata.append(
            ArchiveMetadataEntry(
                container=source.display_name,
                path=safe_name,
                name=PurePosixPath(safe_name).name,
                data=data,
            )
        )
        state.metadata_bytes += len(data)

    @staticmethod
    def _note_metadata_cap(state: _WalkState) -> None:
        if state.metadata_capped:
            return
        state.metadata_capped = True
        logger.info(
            "Metadata collection hit its cap; continuing without the rest",
            extra={
                "event": "archive.inspect.metadata_capped",
                "category": "archive",
                "status": "skipped",
            },
        )

    def _probe_container(self, source: ArchiveSource, nested_depth: int) -> ArchiveContainerProbe:
        try:
            return self._probe_entries(source)
        except (ArchivePasswordRequired, ArchivePasswordRejected) as exc:
            raise _Rejected(
                ImportRejection.ENCRYPTED_ROOT
                if nested_depth == 0
                else ImportRejection.ENCRYPTED_NESTED,
                f"Password-protected archive cannot be imported: {source.display_name}",
                source.display_name,
            ) from exc
        except ArchiveResourceLimitError as exc:
            raise _limit_rejection(exc, source.display_name) from exc
        except ArchiveDependencyMissing as exc:
            raise _Rejected(
                ImportRejection.DEPENDENCY_MISSING, str(exc), source.display_name
            ) from exc
        except ArchiveError as exc:
            raise _Rejected(
                ImportRejection.MALFORMED_ROOT
                if nested_depth == 0
                else ImportRejection.MALFORMED_CHILD,
                f"Archive could not be read: {source.display_name} ({exc})",
                source.display_name,
            ) from exc


def _require_depth(depth: int, maximum: int | None, *, limit: str, subject: str, at: str) -> None:
    """Reject when *depth* is past *maximum*. ``None`` means the user chose no limit."""

    if maximum is None or depth <= maximum:
        return
    raise _Rejected(
        ImportRejection.LIMIT_EXCEEDED,
        (
            f"This archive is deeper than the configured {limit} of {maximum}. "
            "Raise the limit in Settings to import it."
        ),
        at,
    )


def _limit_rejection(error: ArchiveResourceLimitError, at: str) -> _Rejected:
    return _Rejected(
        ImportRejection.LIMIT_EXCEEDED,
        (
            f"This archive exceeds the configured {error.limit} limit. "
            "Raise the limit in Settings to import it."
        ),
        at,
    )


__all__ = [
    "ArchiveImportInspection",
    "ArchiveImportInspector",
    "ArchiveMetadataEntry",
    "ImportRejection",
    "METADATA_ENTRY_NAMES",
    "METADATA_MAX_BYTES",
    "METADATA_MAX_ENTRIES",
    "METADATA_TOTAL_MAX_BYTES",
]
