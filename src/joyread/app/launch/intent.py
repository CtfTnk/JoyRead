"""Qt-free launch requests shared by bootstrap and local IPC.

Also home to drag-and-drop classification. A drop is not a launch, but it asks
the identical question -- which of these paths can this app actually open? --
and answering it twice is how the two answers drift apart. ``classify_drop_paths``
is built on the same :func:`normalize_launch_paths` the launch path uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import os
from pathlib import Path
from typing import Iterable

from joyread.core.file_types import SUPPORTED_READER_EXTENSIONS


LAUNCH_PROTOCOL_VERSION = 1
MAX_LAUNCH_MESSAGE_BYTES = 256 * 1024


class LaunchAction(StrEnum):
    SHOW_LIBRARY = "show_library"
    OPEN_FILES = "open_files"


@dataclass(frozen=True)
class LaunchIntent:
    action: LaunchAction
    paths: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if self.action == LaunchAction.SHOW_LIBRARY and self.paths:
            raise ValueError("SHOW_LIBRARY cannot carry file paths.")
        if self.action == LaunchAction.OPEN_FILES and not self.paths:
            raise ValueError("OPEN_FILES requires at least one supported path.")

    @classmethod
    def show_library(cls) -> LaunchIntent:
        return cls(LaunchAction.SHOW_LIBRARY)

    @classmethod
    def open_files(
        cls,
        paths: Iterable[str | Path],
        *,
        supported_extensions: Iterable[str] = SUPPORTED_READER_EXTENSIONS,
    ) -> LaunchIntent:
        normalized = normalize_launch_paths(paths, supported_extensions=supported_extensions)
        if not normalized:
            raise ValueError("OPEN_FILES requires at least one supported path.")
        return cls(LaunchAction.OPEN_FILES, normalized)


def intent_from_arguments(
    arguments: Iterable[str],
    *,
    supported_extensions: Iterable[str] = SUPPORTED_READER_EXTENSIONS,
) -> LaunchIntent | None:
    paths = normalize_launch_paths(arguments, supported_extensions=supported_extensions)
    return LaunchIntent(LaunchAction.OPEN_FILES, paths) if paths else None


def normalize_launch_paths(
    paths: Iterable[str | Path],
    *,
    supported_extensions: Iterable[str] = SUPPORTED_READER_EXTENSIONS,
    resolve: bool = True,
) -> tuple[Path, ...]:
    """Filter to supported suffixes, de-duplicate, and normalise.

    ``resolve=False`` skips the symlink walk. Launch needs resolution -- two
    argv spellings of one document must not open two windows -- but resolving
    is by far the expensive part (measured 13ms for 500 six-level paths against
    2ms for the suffix and directory checks), so callers on a latency-sensitive
    path can opt out and de-duplicate on the literal path instead.
    """

    extensions = frozenset(str(extension).lower() for extension in supported_extensions)
    normalized: list[Path] = []
    seen: set[str] = set()
    for value in paths:
        path = Path(value).expanduser()
        if path.suffix.lower() not in extensions:
            continue
        if resolve:
            path = path.resolve(strict=False)
        # Deliberately not ``canonical_path_key`` here: it resolves again, and
        # this path is already in its final form either way. That second walk
        # was doubling the cost of every call.
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        normalized.append(path)
    return tuple(normalized)


class ReadUnavailable(StrEnum):
    """Why a dropped payload cannot be opened in a reader.

    The drop overlay dims its Read zone and swaps in a matching hint, so the
    reason is part of the model rather than something the widget re-derives.
    """

    MULTIPLE_ITEMS = "multiple_items"
    FOLDER = "folder"


@dataclass(frozen=True)
class DropPayload:
    """What a set of dropped paths affords.

    ``files`` are supported, de-duplicated reader sources, spelled exactly as
    they were handed over -- :func:`classify_drop_paths` does not resolve
    symlinks, so these may be aliases or relative paths. ``folders`` are
    directories: import expands them, but no reader can open one, which is the
    whole reason the two zones can disagree about the same drop.
    """

    files: tuple[Path, ...] = ()
    folders: tuple[Path, ...] = ()

    @property
    def can_import(self) -> bool:
        return bool(self.files or self.folders)

    @property
    def can_read(self) -> bool:
        # Reading opens one window onto one source: several files have no single
        # target, and a folder is not a document.
        return len(self.files) == 1 and not self.folders

    @property
    def read_path(self) -> Path | None:
        return self.files[0] if self.can_read else None

    @property
    def read_unavailable(self) -> ReadUnavailable | None:
        if self.can_read or not self.can_import:
            return None
        # A folder mixed in with files still reads as "too many things"; the
        # folder-specific hint is only useful when folders are all there is.
        if self.folders and not self.files:
            return ReadUnavailable.FOLDER
        return ReadUnavailable.MULTIPLE_ITEMS

    @property
    def import_paths(self) -> tuple[Path, ...]:
        return self.files + self.folders

    @property
    def item_count(self) -> int:
        return len(self.files) + len(self.folders)


def classify_drop_paths(
    paths: Iterable[str | Path],
    *,
    supported_extensions: Iterable[str] = SUPPORTED_READER_EXTENSIONS,
) -> DropPayload:
    """Split dropped paths into readable files and importable folders.

    A path with an unsupported suffix is dropped, leaving an empty payload the
    caller refuses outright. Existence is *not* checked: a dangling symlink
    named ``Volume 01.cbz`` still counts as a file here, and fails later in the
    reader or the import batch, which both already report a missing source.

    **Runs on the UI thread**, inside ``dragEnterEvent``, which Qt requires to
    answer the OS synchronously -- there is no way to defer the accept/ignore
    decision to a worker and still report an honest drop cursor. So this does
    as little I/O as it can:

    * It never resolves symlinks. Resolution is six times the cost of a stat
      and can block for seconds on a network volume mid-drag.
    * It only probes ``is_dir`` for paths a suffix has *not* already settled.
      Dropping books -- the overwhelmingly common case, and the one most likely
      to come off a slow network share -- touches the filesystem zero times.

    The residual cost is one stat per path that is neither a recognised book
    nor already known, which is folders and junk: few, and unavoidable, since
    nothing but the filesystem can say whether a suffix-less path is a
    directory. A directory *named* like a book (``Series.cbz/``) is the one
    thing this misreads; it is treated as a file and fails in the reader or the
    import batch, both of which already report an unopenable source.

    De-duplication here is literal, so an alias dropped alongside its target
    reads as two items and dims the Read zone. Import is unaffected, because
    ``ImportService.import_paths`` de-duplicates on resolved paths from a
    worker thread, which is where that work belongs.
    """

    extensions = frozenset(str(extension).lower() for extension in supported_extensions)
    folders: list[Path] = []
    seen_folders: set[str] = set()
    file_candidates: list[str | Path] = []
    for value in paths:
        path = Path(value).expanduser()
        # A recognised book suffix answers "file or folder?" without touching
        # the disk, which is the whole point: a drop of books costs no I/O.
        if path.suffix.lower() in extensions:
            file_candidates.append(path)
            continue
        # Everything else might be a folder, and only the filesystem knows.
        # ``is_dir`` swallows its own OSErrors.
        if path.is_dir():
            key = os.path.normcase(str(path))
            if key not in seen_folders:
                seen_folders.add(key)
                folders.append(path)
            continue
        file_candidates.append(path)
    files = normalize_launch_paths(
        file_candidates,
        supported_extensions=supported_extensions,
        resolve=False,
    )
    return DropPayload(files=files, folders=tuple(folders))


def canonical_path_key(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve(strict=False)
    return os.path.normcase(str(resolved))


def merge_open_intents(*intents: LaunchIntent | None) -> LaunchIntent | None:
    paths: list[Path] = []
    for intent in intents:
        if intent is not None and intent.action == LaunchAction.OPEN_FILES:
            paths.extend(intent.paths)
    normalized = normalize_launch_paths(paths)
    return LaunchIntent(LaunchAction.OPEN_FILES, normalized) if normalized else None


def encode_launch_intent(intent: LaunchIntent) -> bytes:
    payload = {
        "version": LAUNCH_PROTOCOL_VERSION,
        "action": intent.action.value,
        "paths": [str(path) for path in intent.paths],
    }
    # POSIX argv can contain surrogate-escaped filenames. ASCII JSON escaping
    # keeps the local transport valid UTF-8 while preserving those code points.
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > MAX_LAUNCH_MESSAGE_BYTES:
        raise ValueError("Launch request exceeds the maximum IPC message size.")
    return encoded


def decode_launch_intent(data: bytes) -> LaunchIntent:
    if not data or len(data) > MAX_LAUNCH_MESSAGE_BYTES:
        raise ValueError("Invalid launch request size.")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Launch request is not valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict) or payload.get("version") != LAUNCH_PROTOCOL_VERSION:
        raise ValueError("Unsupported launch request protocol.")
    try:
        action = LaunchAction(payload.get("action"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Unknown launch request action.") from exc

    raw_paths = payload.get("paths", [])
    if not isinstance(raw_paths, list) or any(not isinstance(path, str) for path in raw_paths):
        raise ValueError("Launch request paths must be a list of strings.")
    if action == LaunchAction.SHOW_LIBRARY:
        if raw_paths:
            raise ValueError("SHOW_LIBRARY cannot carry file paths.")
        return LaunchIntent.show_library()
    return LaunchIntent.open_files(raw_paths)
