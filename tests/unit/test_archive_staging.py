"""Tests for the archive staging directories and their orphan sweep."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from joyread.core.archive.staging import (
    create_spill_directory,
    sweep_orphaned_staging,
)


def _staging_dir(root: Path, name: str, *, age_seconds: float = 0.0) -> Path:
    directory = root / name
    directory.mkdir()
    (directory / "page.jpg").write_bytes(b"spilled")
    if age_seconds:
        stamp = os.stat(directory).st_mtime - age_seconds
        os.utime(directory, (stamp, stamp))
    return directory


def test_a_spill_directory_names_the_process_that_owns_it() -> None:
    spill = create_spill_directory()
    try:
        assert spill.name.startswith(f"joyread-nested-{os.getpid()}-")
    finally:
        spill.rmdir()


@pytest.mark.skipif(os.name == "nt", reason="Windows uses age rather than PID liveness")
def test_the_sweep_reclaims_a_spill_directory_whose_process_is_gone(
    tmp_path: Path,
) -> None:
    # A pid that cannot be running: 0 is never a user process.
    orphan = _staging_dir(tmp_path, "joyread-nested-0-abandoned")

    assert sweep_orphaned_staging(tmp_path) == 1
    assert not orphan.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific age fallback")
def test_windows_sweeps_stamped_directories_only_after_the_age_threshold(tmp_path: Path) -> None:
    fresh = _staging_dir(tmp_path, "joyread-nested-0-fresh")
    stale = _staging_dir(tmp_path, "joyread-nested-0-stale", age_seconds=48 * 60 * 60)

    assert sweep_orphaned_staging(tmp_path) == 1
    assert fresh.exists()
    assert not stale.exists()


def test_the_sweep_leaves_a_live_readers_spill_directory_alone(tmp_path: Path) -> None:
    mine = _staging_dir(tmp_path, f"joyread-nested-{os.getpid()}-live")

    assert sweep_orphaned_staging(tmp_path) == 0
    assert (mine / "page.jpg").exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows has no process-id probe")
def test_a_recycled_process_id_keeps_the_sweep_conservative(tmp_path: Path) -> None:
    """A stamp naming a live process is never swept on the pid rule alone."""

    borrowed = _staging_dir(
        tmp_path,
        f"joyread-nested-{os.getppid()}-borrowed",
        age_seconds=48 * 60 * 60,
    )

    assert sweep_orphaned_staging(tmp_path) == 0
    assert borrowed.exists()


def test_an_unstamped_staging_tree_is_swept_only_once_it_is_stale(
    tmp_path: Path,
) -> None:
    fresh = _staging_dir(tmp_path, "joyread-7z-fresh")
    stale = _staging_dir(tmp_path, "joyread-convert-stale", age_seconds=48 * 60 * 60)

    assert sweep_orphaned_staging(tmp_path) == 1
    assert fresh.exists()
    assert not stale.exists()


def test_the_sweep_only_touches_directories_it_recognizes(tmp_path: Path) -> None:
    """The app puts other things in the temp directory; they are not ours."""

    runtime = _staging_dir(tmp_path, "joyread-tests-runtime", age_seconds=48 * 60 * 60)
    unrelated = _staging_dir(tmp_path, "some-other-tool", age_seconds=48 * 60 * 60)
    loose_file = tmp_path / "joyread-nested-0-notadirectory"
    loose_file.write_bytes(b"not a staging tree")

    assert sweep_orphaned_staging(tmp_path) == 0
    assert runtime.exists()
    assert unrelated.exists()
    assert loose_file.exists()


def test_a_missing_temp_directory_is_not_an_error(tmp_path: Path) -> None:
    assert sweep_orphaned_staging(tmp_path / "gone") == 0
