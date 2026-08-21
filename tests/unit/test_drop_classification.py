"""What a dropped set of paths affords.

The drag overlay draws its two zones straight from this, so these tests are the
availability table itself: which drops can be read, which can only be imported,
and which are refused before any UI appears.
"""

from __future__ import annotations

from pathlib import Path

from joyread.app.launch.intent import (
    DropPayload,
    ReadUnavailable,
    classify_drop_paths,
)


def _cbz(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_bytes(b"not a real archive")
    return path


def test_a_single_supported_file_can_be_read_and_imported(tmp_path: Path) -> None:
    source = _cbz(tmp_path, "Volume 01.cbz")

    payload = classify_drop_paths([source])

    assert payload.can_read
    assert payload.can_import
    assert payload.read_path == source
    assert payload.read_unavailable is None
    assert payload.item_count == 1


def test_several_files_import_but_cannot_be_read(tmp_path: Path) -> None:
    """Reading opens one window onto one source; several files have no single
    target, so the Read zone is drawn disabled."""

    payload = classify_drop_paths([_cbz(tmp_path, "a.cbz"), _cbz(tmp_path, "b.cbz")])

    assert not payload.can_read
    assert payload.can_import
    assert payload.read_path is None
    assert payload.read_unavailable is ReadUnavailable.MULTIPLE_ITEMS
    assert payload.item_count == 2


def test_a_folder_imports_but_cannot_be_read(tmp_path: Path) -> None:
    folder = tmp_path / "Series"
    folder.mkdir()

    payload = classify_drop_paths([folder])

    assert not payload.can_read
    assert payload.can_import
    assert payload.folders == (folder,)
    # The folder-specific hint only makes sense when folders are all there is.
    assert payload.read_unavailable is ReadUnavailable.FOLDER


def test_a_folder_mixed_with_a_file_reads_as_too_many_things(tmp_path: Path) -> None:
    folder = tmp_path / "Series"
    folder.mkdir()

    payload = classify_drop_paths([folder, _cbz(tmp_path, "a.cbz")])

    assert not payload.can_read
    assert payload.can_import
    assert payload.read_unavailable is ReadUnavailable.MULTIPLE_ITEMS
    assert payload.item_count == 2


def test_unsupported_files_are_dropped_before_they_reach_a_zone(tmp_path: Path) -> None:
    supported = _cbz(tmp_path, "a.cbz")
    (tmp_path / "notes.txt").write_text("nope")
    (tmp_path / "cover.png").write_bytes(b"nope")

    payload = classify_drop_paths([supported, tmp_path / "notes.txt", tmp_path / "cover.png"])

    # One supported file survives, so this is still a readable drop -- the
    # unsupported siblings must not count toward the "too many things" rule.
    assert payload.files == (supported,)
    assert payload.can_read


def test_a_drop_with_nothing_usable_is_refused_outright(tmp_path: Path) -> None:
    """``can_import`` false is the signal to ignore the drag entirely, so the
    OS shows a no-drop cursor instead of a UI that could not act."""

    (tmp_path / "notes.txt").write_text("nope")

    payload = classify_drop_paths([tmp_path / "notes.txt"])

    assert not payload.can_import
    assert not payload.can_read
    assert payload.read_unavailable is None
    assert payload.import_paths == ()


def test_an_empty_drop_is_refused() -> None:
    assert not classify_drop_paths([]).can_import
    assert not DropPayload().can_import


def test_the_same_file_dropped_twice_counts_once(tmp_path: Path) -> None:
    """Finder can hand over an alias and its target together. Counting both
    would disable Read for what the user experiences as one file."""

    source = _cbz(tmp_path, "a.cbz")

    payload = classify_drop_paths([source, source])

    assert payload.item_count == 1
    assert payload.can_read


def test_the_same_folder_dropped_twice_counts_once(tmp_path: Path) -> None:
    folder = tmp_path / "Series"
    folder.mkdir()

    payload = classify_drop_paths([folder, folder])

    assert payload.folders == (folder,)


def test_import_paths_carries_files_then_folders(tmp_path: Path) -> None:
    """The import call is built from this, so both kinds have to survive it."""

    folder = tmp_path / "Series"
    folder.mkdir()
    source = _cbz(tmp_path, "a.cbz")

    payload = classify_drop_paths([folder, source])

    assert payload.import_paths == (source, folder)


def test_classification_does_not_walk_symlinks(tmp_path: Path) -> None:
    """This runs on the UI thread inside ``dragEnterEvent``.

    Resolving is six times the cost of the directory probe and can block for
    seconds on a network volume mid-drag, so paths come back exactly as the OS
    handed them over. Import de-duplicates on resolved paths from a worker
    thread instead.
    """

    real = tmp_path / "real"
    real.mkdir()
    source = _cbz(real, "a.cbz")
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real)
    via_link = link_dir / "a.cbz"

    payload = classify_drop_paths([via_link])

    assert payload.files == (via_link,)
    assert payload.files != (source,)
    assert payload.can_read

    folder_payload = classify_drop_paths([link_dir])
    assert folder_payload.folders == (link_dir,)


def test_launch_normalisation_still_resolves(tmp_path: Path) -> None:
    """The opt-out is per-caller. Launch must keep resolving, or two argv
    spellings of one document open two windows."""

    from joyread.app.launch.intent import normalize_launch_paths

    real = tmp_path / "real"
    real.mkdir()
    source = _cbz(real, "a.cbz")
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real)

    assert normalize_launch_paths([link_dir / "a.cbz", source]) == (source,)


def test_dropping_books_touches_the_filesystem_zero_times(tmp_path, monkeypatch) -> None:
    """This runs inside ``dragEnterEvent``, which Qt requires to answer the OS
    synchronously, so every stat here is on the UI thread.

    A recognised book suffix settles file-vs-folder without asking the disk.
    That matters most for exactly the case it looks least like: a large drop off
    an SMB/NFS share, where each stat can block.
    """

    probes: list[Path] = []
    real_is_dir = Path.is_dir
    monkeypatch.setattr(
        Path, "is_dir", lambda self, *a, **k: (probes.append(self), real_is_dir(self, *a, **k))[1]
    )
    books = [_cbz(tmp_path, f"Volume {i:02d}.cbz") for i in range(20)]

    payload = classify_drop_paths(books)

    assert probes == []
    assert len(payload.files) == 20


def test_a_folder_still_costs_one_probe_and_only_one(tmp_path, monkeypatch) -> None:
    """Nothing but the filesystem can say whether a suffix-less path is a
    directory, so folders are the irreducible cost -- but books alongside them
    must not add to it."""

    probes: list[Path] = []
    real_is_dir = Path.is_dir
    monkeypatch.setattr(
        Path, "is_dir", lambda self, *a, **k: (probes.append(self), real_is_dir(self, *a, **k))[1]
    )
    folder = tmp_path / "Series"
    folder.mkdir()
    books = [_cbz(tmp_path, f"Volume {i:02d}.cbz") for i in range(20)]

    payload = classify_drop_paths([folder, *books])

    assert probes == [folder]
    assert payload.folders == (folder,)
    assert len(payload.files) == 20
