"""Repackaging an archive into JoyRead's canonical container.

Three properties carry the feature, and each is load-bearing for something
outside this module: the output is byte-deterministic (or ``stored_hash`` is not
an integrity baseline), re-reading it reproduces the reader's page order *and*
table of contents (or conversion silently reorders books), and it contains only
images and metadata (or import becomes a way to store arbitrary files).
"""

from __future__ import annotations

from pathlib import Path
import zipfile

import pytest
from PIL import Image

from joyread.core.archive import ArchiveImageService, CanonicalWriteCancelled
from joyread.core.archive.errors import ArchiveEmptyError
from joyread.core.archive.metadata import select_sidecars


def _png(color: str = "#336699", size: tuple[int, int] = (8, 12)) -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def _names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return archive.namelist()


def _convert_with_sidecars(service: ArchiveImageService, source: Path, destination: Path):
    """Convert the way import does: sidecars come from the inspection it ran.

    The converter deliberately does not go looking for them itself -- that would
    mean walking and materializing every nested container a second time.
    """

    inspection = service.inspect_for_import(source)
    return service.convert_to_canonical(
        source, destination, sidecars=select_sidecars(inspection.metadata_entries)
    )


# ----------------------------------------------------------------------
# Determinism
# ----------------------------------------------------------------------


def test_the_same_source_converts_to_byte_identical_output(tmp_path: Path) -> None:
    """``stored_hash`` is the integrity baseline maintenance re-hashes against.

    If conversion embedded a real timestamp or the host OS, a book would appear
    to change every time it was written and the baseline would mean nothing.
    """

    source = _zip(tmp_path / "book.cbz", {"002.png": _png(), "001.png": _png("#993366")})
    service = ArchiveImageService()

    first = tmp_path / "out" / "first.cbz"
    second = tmp_path / "out" / "second.cbz"
    service.convert_to_canonical(source, first)
    service.convert_to_canonical(source, second)

    assert first.read_bytes() == second.read_bytes()


def test_entry_metadata_carries_no_host_or_clock_state(tmp_path: Path) -> None:
    """The fields that would differ between two machines writing one book."""

    source = _zip(tmp_path / "book.cbz", {"001.png": _png()})
    destination = tmp_path / "canonical.cbz"

    ArchiveImageService().convert_to_canonical(source, destination)

    with zipfile.ZipFile(destination) as archive:
        info = archive.getinfo("001.png")

    assert info.date_time == (1980, 1, 1, 0, 0, 0)
    assert info.create_system == 0
    assert info.comment == b""


def test_pages_are_stored_rather_than_deflated(tmp_path: Path) -> None:
    """Images are already compressed. This container exists to make reads cheap,
    and deflate costs CPU on every page for a fraction of a percent."""

    source = _zip(tmp_path / "book.cbz", {"001.png": _png()})
    destination = tmp_path / "canonical.cbz"

    ArchiveImageService().convert_to_canonical(source, destination)

    with zipfile.ZipFile(destination) as archive:
        assert archive.getinfo("001.png").compress_type == zipfile.ZIP_STORED


# ----------------------------------------------------------------------
# Structure: nesting becomes directories, so the ToC survives
# ----------------------------------------------------------------------


def test_a_nested_archive_becomes_a_directory_and_keeps_the_contents_entry(
    tmp_path: Path,
) -> None:
    """The whole reason to flatten containers rather than paths: a reader builds
    Contents from folder nodes, so a directory reproduces what the container
    contributed."""

    inner = _zip(tmp_path / "src" / "Vol01.cbz", {"001.png": _png(), "002.png": _png()})
    outer = _zip(
        tmp_path / "outer.cbz",
        {"Vol01.cbz": inner.read_bytes(), "cover.png": _png("#112233")},
    )
    destination = tmp_path / "canonical.cbz"
    service = ArchiveImageService()

    service.convert_to_canonical(outer, destination)

    assert _names(destination) == ["cover.png", "Vol01/001.png", "Vol01/002.png"]

    before = service.open(outer)
    after = service.open(destination)
    try:
        assert [entry.label for entry in after.contents] == [
            entry.label for entry in before.contents
        ]
        assert after.page_count == before.page_count
    finally:
        before.close()
        after.close()


def test_folder_depth_inside_a_container_is_preserved(tmp_path: Path) -> None:
    source = _zip(
        tmp_path / "book.cbz",
        {"ch01/001.png": _png(), "ch01/002.png": _png(), "ch02/001.png": _png()},
    )
    destination = tmp_path / "canonical.cbz"

    ArchiveImageService().convert_to_canonical(source, destination)

    assert _names(destination) == ["ch01/001.png", "ch01/002.png", "ch02/001.png"]


def test_two_levels_of_nesting_flatten_into_two_levels_of_directory(
    tmp_path: Path,
) -> None:
    innermost = _zip(tmp_path / "src" / "Ch01.cbz", {"001.png": _png()})
    middle = _zip(tmp_path / "src" / "Vol01.cbz", {"Ch01.cbz": innermost.read_bytes()})
    outer = _zip(tmp_path / "outer.cbz", {"Vol01.cbz": middle.read_bytes()})
    destination = tmp_path / "canonical.cbz"

    ArchiveImageService().convert_to_canonical(outer, destination)

    assert _names(destination) == ["Vol01/Ch01/001.png"]


def test_page_order_matches_what_a_reader_would_show(tmp_path: Path) -> None:
    """Natural sort, not lexicographic -- ``10.png`` follows ``9.png``."""

    source = _zip(
        tmp_path / "book.cbz",
        {f"{index}.png": _png() for index in (10, 2, 1, 9)},
    )
    destination = tmp_path / "canonical.cbz"

    ArchiveImageService().convert_to_canonical(source, destination)

    assert _names(destination) == ["1.png", "2.png", "9.png", "10.png"]


# ----------------------------------------------------------------------
# Sanitization
# ----------------------------------------------------------------------


def test_only_images_and_the_chosen_sidecars_reach_the_artifact(tmp_path: Path) -> None:
    """Import must not become a way to store arbitrary files.

    Nothing here is filtered by a blocklist in the writer: these entries never
    become pages during the scan, so the writer never sees them.
    """

    source = _zip(
        tmp_path / "book.cbz",
        {
            "001.png": _png(),
            "payload.exe": b"MZ\x90\x00",
            "notes.txt": b"hello",
            "__MACOSX/._001.png": b"resource fork",
            ".DS_Store": b"junk",
            "ComicInfo.xml": b"<ComicInfo><Title>Kept</Title></ComicInfo>",
        },
    )
    destination = tmp_path / "canonical.cbz"

    _convert_with_sidecars(ArchiveImageService(), source, destination)

    assert _names(destination) == ["001.png", "ComicInfo.xml"]


def test_a_traversal_entry_name_never_reaches_the_artifact(tmp_path: Path) -> None:
    source = tmp_path / "evil.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("../../escape.png", _png())
        archive.writestr("001.png", _png())
    destination = tmp_path / "canonical.cbz"

    ArchiveImageService().convert_to_canonical(source, destination)

    assert _names(destination) == ["001.png"]


def test_the_winning_sidecar_is_carried_over_and_chapter_copies_are_not(
    tmp_path: Path,
) -> None:
    """One per kind, and the same one the library read its title from."""

    source = _zip(
        tmp_path / "book.cbz",
        {
            "001.png": _png(),
            "ComicInfo.xml": b"<ComicInfo><Title>Volume</Title></ComicInfo>",
            "ch01/ComicInfo.xml": b"<ComicInfo><Title>Chapter</Title></ComicInfo>",
            "ch01/001.png": _png(),
        },
    )
    destination = tmp_path / "canonical.cbz"

    _convert_with_sidecars(ArchiveImageService(), source, destination)

    with zipfile.ZipFile(destination) as archive:
        sidecars = [name for name in archive.namelist() if name.endswith(".xml")]
        assert sidecars == ["ComicInfo.xml"]
        assert b"Volume" in archive.read("ComicInfo.xml")


# ----------------------------------------------------------------------
# Failure and cancellation
# ----------------------------------------------------------------------


def test_cancelling_midway_leaves_no_artifact(tmp_path: Path) -> None:
    """A partial file that survived would be hashed and recorded as a book."""

    source = _zip(tmp_path / "book.cbz", {f"{i:03d}.png": _png() for i in range(6)})
    destination = tmp_path / "canonical.cbz"
    seen: list[int] = []

    def cancel_after_two() -> bool:
        seen.append(1)
        return len(seen) > 2

    with pytest.raises(CanonicalWriteCancelled):
        ArchiveImageService().convert_to_canonical(
            source, destination, is_cancelled=cancel_after_two
        )

    assert not destination.exists()


def test_a_read_failure_midway_leaves_no_artifact(tmp_path: Path) -> None:
    source = _zip(tmp_path / "book.cbz", {f"{i:03d}.png": _png() for i in range(4)})
    destination = tmp_path / "canonical.cbz"

    from joyread.core.archive.canonical import CbzWriter

    class ExplodingWriter(CbzWriter):
        pass

    def boom(_page):
        raise OSError("disk went away")

    from joyread.core.archive.tree import flatten_archive_tree_for_writing  # noqa: F401

    with pytest.raises(OSError):
        ExplodingWriter().write(
            destination,
            [("", _FakePage("001.png"))],
            (),
            read_page=boom,
        )

    assert not destination.exists()


def test_an_archive_with_no_pages_is_refused_rather_than_written_empty(
    tmp_path: Path,
) -> None:
    source = _zip(tmp_path / "empty.cbz", {"notes.txt": b"nothing to see"})

    with pytest.raises(ArchiveEmptyError):
        ArchiveImageService().convert_to_canonical(source, tmp_path / "out.cbz")


def test_progress_counts_every_page_against_a_known_total(tmp_path: Path) -> None:
    source = _zip(tmp_path / "book.cbz", {f"{i:03d}.png": _png() for i in range(3)})
    seen: list[tuple[int, int]] = []

    ArchiveImageService().convert_to_canonical(
        source, tmp_path / "out.cbz", on_page=lambda done, total: seen.append((done, total))
    )

    assert seen == [(1, 3), (2, 3), (3, 3)]


class _FakePage:
    """Minimal stand-in for a ``PageRecord``: the writer only reads its path."""

    def __init__(self, name: str) -> None:
        self.display_path = name
        self.name = name
        self.password = None
        self.source = None
        self.size = None


# ----------------------------------------------------------------------
# How the pages are read out of the source
# ----------------------------------------------------------------------


def test_pages_are_read_a_container_at_a_time_not_one_at_a_time(tmp_path: Path) -> None:
    """The difference between linear and quadratic on a solid archive.

    Every read of a solid 7z has to decompress the whole solid block to reach
    one member, and with the 7-Zip executable it also costs a subprocess. Asking
    per page made a 60-page book 60 full decompressions; measured, that was 39s
    against 2.7s for the batched form.
    """

    source = _zip(tmp_path / "book.cbz", {f"{i:03d}.png": _png() for i in range(40)})
    service = ArchiveImageService()
    # Bulk extraction off, so this exercises the fallback every nested archive
    # and every py7zr-only install takes.
    service._bulk_extract_for = lambda _source: None
    calls: list[int] = []
    original = service._read_entries

    def counted(archive_source, entries, **kwargs):
        calls.append(len(entries))
        return original(archive_source, entries, **kwargs)

    service._read_entries = counted

    result = service.convert_to_canonical(source, tmp_path / "out.cbz")

    assert result.page_count == 40
    assert len(calls) < 40
    assert sum(calls) == 40  # every page still read exactly once


def test_a_bulk_capable_backend_extracts_the_whole_container_in_one_pass(
    tmp_path: Path,
) -> None:
    """The path a real 7-Zip install takes: one process for the whole book.

    Verified with a stand-in because the executable is not a test dependency --
    what matters is that conversion asks for every member at once and then reads
    the extracted files, rather than going back to the archive per page.
    """

    pages = {f"{i:03d}.png": _png() for i in range(12)}
    source = _zip(tmp_path / "book.cbz", pages)
    service = ArchiveImageService()
    bulk_calls: list[tuple[str, ...]] = []

    def fake_bulk(archive_source, members, destination, password, **kwargs):
        bulk_calls.append(tuple(members))
        with zipfile.ZipFile(archive_source.path) as archive:
            for member in members:
                target = destination / member
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))

    service._bulk_extract_for = lambda _source: fake_bulk
    read_entries_calls: list[int] = []
    original = service._read_entries
    service._read_entries = lambda *a, **k: (
        read_entries_calls.append(1) or original(*a, **k)
    )

    result = service.convert_to_canonical(source, tmp_path / "out.cbz")

    assert len(bulk_calls) == 1
    assert set(bulk_calls[0]) == set(pages)
    assert read_entries_calls == []  # nothing fell back to per-entry reads
    assert result.page_count == 12
    with zipfile.ZipFile(tmp_path / "out.cbz") as written:
        assert written.read("000.png") == pages["000.png"]


def test_a_backend_that_cannot_bulk_extract_falls_back_rather_than_failing(
    tmp_path: Path,
) -> None:
    """A member name 7-Zip cannot express is a capability gap, not a bad book."""

    from joyread.core.archive.errors import ArchiveBulkUnsupported

    source = _zip(tmp_path / "book.cbz", {"001.png": _png(), "002.png": _png()})
    service = ArchiveImageService()

    def refusing_bulk(*_args, **_kwargs):
        raise ArchiveBulkUnsupported("cannot express these names")

    service._bulk_extract_for = lambda _source: refusing_bulk

    result = service.convert_to_canonical(source, tmp_path / "out.cbz")

    assert result.page_count == 2
    assert _names(tmp_path / "out.cbz") == ["001.png", "002.png"]


def test_the_workspace_is_cleaned_up_after_a_conversion(tmp_path: Path) -> None:
    """Staged pages are a full second copy of the book; leaving them behind
    would quietly double the disk cost of every import."""

    import tempfile

    source = _zip(tmp_path / "book.cbz", {f"{i:03d}.png": _png() for i in range(5)})
    before = set(Path(tempfile.gettempdir()).glob("joyread-canonical-*"))

    ArchiveImageService().convert_to_canonical(source, tmp_path / "out.cbz")

    assert set(Path(tempfile.gettempdir()).glob("joyread-canonical-*")) == before


def test_a_bulk_extracted_page_is_still_held_to_the_item_limit(tmp_path: Path) -> None:
    """The case that gets past every earlier check: an under-declared entry.

    The scan admits it because its header says it is small, and bulk extraction
    is a subprocess writing a directory tree, so the per-member limit is never
    applied to what actually lands on disk. Reading that back unbounded is the
    whole bypass — the limit has to be enforced where the bytes are, not where
    the header claimed they would be.
    """

    from joyread.core.archive.errors import ArchiveResourceLimitError
    from joyread.core.archive.limits import ArchiveOpenLimits

    source = _zip(tmp_path / "book.cbz", {"001.png": _png()})
    service = ArchiveImageService()

    def under_declaring_bulk(archive_source, members, destination, password, **kwargs):
        # What the listing promised was small; what the extractor produced is not.
        for member in members:
            target = destination / member
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x00" * (4 * 1024 * 1024))

    service._bulk_extract_for = lambda _source: under_declaring_bulk

    with pytest.raises(ArchiveResourceLimitError) as excinfo:
        service.convert_to_canonical(
            source,
            tmp_path / "out.cbz",
            limits=ArchiveOpenLimits(max_extracted_item_bytes=64 * 1024),
        )

    assert excinfo.value.limit == "extracted_item_bytes"


def test_bulk_extracted_pages_are_charged_to_the_operation_budget(
    tmp_path: Path,
) -> None:
    """Otherwise a bulk-extracted book costs nothing, and every nested read
    after it still sees the whole operation budget unspent."""

    from joyread.core.archive.errors import ArchiveResourceLimitError
    from joyread.core.archive.limits import ArchiveOpenLimits

    pages = {f"{i:03d}.png": _png(color) for i, color in enumerate(("#111", "#222", "#333"))}
    source = _zip(tmp_path / "book.cbz", pages)
    service = ArchiveImageService()

    def fake_bulk(archive_source, members, destination, password, **kwargs):
        with zipfile.ZipFile(archive_source.path) as archive:
            for member in members:
                target = destination / member
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))

    service._bulk_extract_for = lambda _source: fake_bulk
    # One byte short of the whole book, so reading it all must trip the budget.
    total = sum(len(payload) for payload in pages.values())
    limits = ArchiveOpenLimits(max_operation_bytes=total - 1)

    with pytest.raises(ArchiveResourceLimitError) as excinfo:
        service.convert_to_canonical(source, tmp_path / "out.cbz", limits=limits)

    assert excinfo.value.limit == "operation_bytes"


def test_unknown_size_entries_are_never_batched_together(tmp_path: Path) -> None:
    """A declared size is attacker-controlled and may be absent entirely.

    Grouping on an assumed size is how a handful of under-declared pages become
    a whole manga resident at once, which AGENTS.md forbids outright. The
    shared batch planner isolates them; a second batching rule written here
    would not have.
    """

    source = _zip(tmp_path / "book.cbz", {f"{i:03d}.png": _png() for i in range(6)})
    service = ArchiveImageService()
    service._bulk_extract_for = lambda _source: None
    batch_sizes: list[int] = []
    original = service._read_entries

    def counted(archive_source, entries, **kwargs):
        batch_sizes.append(len(entries))
        return original(archive_source, entries, **kwargs)

    service._read_entries = counted

    # Strip the declared sizes the way a listing that omits them would.
    original_scan = service._scanner.scan

    def sizeless_scan(*args, **kwargs):
        from dataclasses import replace as _replace

        root = original_scan(*args, **kwargs)
        pending = [root]
        while pending:
            node = pending.pop()
            node.pages[:] = [_replace(page, size=None) for page in node.pages]
            pending.extend(node.children)
        return root

    service._scanner.scan = sizeless_scan

    service.convert_to_canonical(source, tmp_path / "out.cbz")

    assert batch_sizes == [1] * 6


def test_an_archive_that_under_declares_its_pages_cannot_batch_them(
    tmp_path: Path, monkeypatch
) -> None:
    """A declared size is the archive's claim about itself, not a fact.

    Eight entries that each say "16 bytes" and each expand toward the per-item
    limit are a legal archive, and the batch planner groups all eight — bounded
    only by the 4 GiB operation budget. That is a whole manga resident at once,
    which AGENTS.md rules out.

    The batch allowance is shrunk rather than the pages inflated: the mechanism
    under test is "the scoped budget catches the lie", not the size of the
    constant.
    """

    from joyread.core.archive import service as service_module

    page = _png(size=(200, 200))
    source = _zip(tmp_path / "book.cbz", {f"{i:03d}.png": page for i in range(8)})
    service = ArchiveImageService()
    service._bulk_extract_for = lambda _source: None
    monkeypatch.setattr(service_module, "_CONVERSION_BATCH_BYTES", len(page) * 2)

    # Every page claims to be tiny, so the planner puts all eight in one batch.
    original_scan = service._scanner.scan

    def lying_scan(*args, **kwargs):
        from dataclasses import replace as _replace

        root = original_scan(*args, **kwargs)
        pending = [root]
        while pending:
            node = pending.pop()
            node.pages[:] = [_replace(entry, size=16) for entry in node.pages]
            pending.extend(node.children)
        return root

    service._scanner.scan = lying_scan

    batch_sizes: list[int] = []
    original_read = service._read_entries

    def counted(archive_source, entries, **kwargs):
        batch_sizes.append(len(entries))
        return original_read(archive_source, entries, **kwargs)

    service._read_entries = counted

    result = service.convert_to_canonical(source, tmp_path / "out.cbz")

    assert result.page_count == 8
    # The optimistic batch is attempted, overruns, and is re-read page by page.
    assert batch_sizes[0] == 8
    assert batch_sizes[1:] == [1] * 8
    # And the pages still came out intact.
    with zipfile.ZipFile(tmp_path / "out.cbz") as written:
        assert written.read("000.png") == page


def test_an_honest_archive_still_gets_whole_batches(tmp_path: Path) -> None:
    """The protection must not cost the common case its batching — that is the
    entire reason a solid archive is not decompressed once per page."""

    source = _zip(tmp_path / "book.cbz", {f"{i:03d}.png": _png() for i in range(8)})
    service = ArchiveImageService()
    service._bulk_extract_for = lambda _source: None
    batch_sizes: list[int] = []
    original = service._read_entries

    def counted(archive_source, entries, **kwargs):
        batch_sizes.append(len(entries))
        return original(archive_source, entries, **kwargs)

    service._read_entries = counted

    service.convert_to_canonical(source, tmp_path / "out.cbz")

    assert batch_sizes == [8]  # one call, no retry


def test_a_nested_container_is_given_a_path_so_it_can_be_bulk_extracted(
    tmp_path: Path,
) -> None:
    """A nested archive is carried as bytes, and bulk extraction needs a file.

    Without this every page of a nested 7z goes through the pure-Python
    backend: measured at 97% of the conversion, 4.3s against 1.7s for the same
    60 pages once the container is spilled to the workspace.
    """

    inner = _zip(tmp_path / "src" / "Vol01.cbz", {f"{i:03d}.png": _png() for i in range(4)})
    outer = _zip(tmp_path / "outer.cbz", {"Vol01.cbz": inner.read_bytes()})
    service = ArchiveImageService()
    bulk_sources: list[object] = []

    def fake_bulk(archive_source, members, destination, password, **kwargs):
        bulk_sources.append(archive_source)
        with zipfile.ZipFile(archive_source.path) as archive:
            for member in members:
                target = destination / member
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))

    # Offered only to path-backed sources, exactly as a real backend does.
    service._bulk_extract_for = lambda source: fake_bulk if source.path is not None else None

    result = service.convert_to_canonical(outer, tmp_path / "out.cbz")

    assert result.page_count == 4
    # The nested container was spilled and then bulk-extracted from that file.
    assert [source.path is not None for source in bulk_sources] == [True]
    assert _names(tmp_path / "out.cbz") == [f"Vol01/{i:03d}.png" for i in range(4)]


def test_spilling_a_container_does_not_change_the_bytes_written(
    tmp_path: Path,
) -> None:
    """Determinism is the property the whole artifact hash rests on, so how the
    pages were obtained must not be visible in the output."""

    inner = _zip(tmp_path / "src" / "Vol01.cbz", {f"{i:03d}.png": _png() for i in range(3)})
    outer = _zip(tmp_path / "outer.cbz", {"Vol01.cbz": inner.read_bytes()})

    in_memory = ArchiveImageService()
    in_memory._bulk_extract_for = lambda _source: None
    in_memory.convert_to_canonical(outer, tmp_path / "memory.cbz")

    spilled = ArchiveImageService()
    spilled.convert_to_canonical(outer, tmp_path / "spilled.cbz")

    assert (tmp_path / "memory.cbz").read_bytes() == (tmp_path / "spilled.cbz").read_bytes()


# ----------------------------------------------------------------------
# Budget accounting: the two extraction paths must agree
# ----------------------------------------------------------------------


def _budget_used(service: ArchiveImageService, source: Path, destination: Path) -> int:
    """Operation bytes a conversion actually charges."""

    from joyread.core.archive import service as service_module

    captured: list = []
    original = service_module._StagedPageReader

    class Spy(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured.append(self._budget)

    service_module._StagedPageReader = Spy
    try:
        service.convert_to_canonical(source, destination)
    finally:
        service_module._StagedPageReader = original
    return captured[0].used


def test_a_conversion_charges_each_byte_to_the_budget_exactly_once(
    tmp_path: Path,
) -> None:
    """The two staging paths charge in different places and must not both.

    Bulk extraction runs a subprocess that writes straight to disk and charges
    nothing, so the read-back is where its bytes meet the budget. A batched read
    already charged them on the way in. Charging on read-back regardless counted
    every byte twice, which silently stopped conversion for any book over about
    half the operation allowance.
    """

    pages = {f"{i:03d}.png": _png("#%02x3366" % (i * 8)) for i in range(6)}
    source = _zip(tmp_path / "book.cbz", pages)
    total = sum(len(payload) for payload in pages.values())

    bulk = ArchiveImageService()
    batched = ArchiveImageService()
    batched._bulk_extract_for = lambda _source: None

    assert _budget_used(bulk, source, tmp_path / "bulk.cbz") == total
    assert _budget_used(batched, source, tmp_path / "batched.cbz") == total


def test_a_book_larger_than_half_the_budget_still_converts(tmp_path: Path) -> None:
    """The user-visible shape of the double charge.

    It did not even surface as an error: import catches the limit failure and
    falls back, so the book was stored verbatim and reported as imported.
    """

    from joyread.core.archive.limits import ArchiveOpenLimits

    pages = {f"{i:03d}.png": _png("#%02x3366" % (i * 8)) for i in range(6)}
    source = _zip(tmp_path / "book.cbz", pages)
    total = sum(len(payload) for payload in pages.values())
    limits = ArchiveOpenLimits(max_operation_bytes=int(total * 1.5))

    for label, disable_bulk in (("bulk", False), ("batched", True)):
        service = ArchiveImageService()
        if disable_bulk:
            service._bulk_extract_for = lambda _source: None
        result = service.convert_to_canonical(
            source, tmp_path / f"{label}.cbz", limits=limits
        )
        assert result.page_count == 6, label


def test_bulk_extraction_is_capped_by_what_the_budget_has_left(tmp_path: Path) -> None:
    """Staging is lazy and per container, so passing the whole ceiling every
    time lets an N-container archive write N times the configured limit."""

    from joyread.core.archive.limits import ArchiveOpenLimits

    inner = _zip(tmp_path / "src" / "Vol01.cbz", {f"{i:03d}.png": _png() for i in range(3)})
    inner2 = _zip(tmp_path / "src" / "Vol02.cbz", {f"{i:03d}.png": _png("#993366") for i in range(3)})
    outer = _zip(
        tmp_path / "outer.cbz",
        {"Vol01.cbz": inner.read_bytes(), "Vol02.cbz": inner2.read_bytes()},
    )
    service = ArchiveImageService()
    caps: list[int | None] = []

    def fake_bulk(archive_source, members, destination, password, *, max_output_bytes, **kwargs):
        caps.append(max_output_bytes)
        with zipfile.ZipFile(archive_source.path) as archive:
            for member in members:
                target = destination / member
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))

    service._bulk_extract_for = lambda source: fake_bulk if source.path is not None else None
    service.convert_to_canonical(
        outer, tmp_path / "out.cbz", limits=ArchiveOpenLimits(max_operation_bytes=10_000_000)
    )

    assert len(caps) == 2
    # The second container is offered strictly less than the first.
    assert caps[1] < caps[0]


def test_no_configured_ceiling_stays_uncapped(tmp_path: Path) -> None:
    """Turning the guardrail off must not be quietly replaced by a number."""

    from joyread.core.archive.limits import ArchiveOpenLimits

    source = _zip(tmp_path / "book.cbz", {"001.png": _png()})
    service = ArchiveImageService()
    caps: list[int | None] = []

    def fake_bulk(archive_source, members, destination, password, *, max_output_bytes, **kwargs):
        caps.append(max_output_bytes)
        with zipfile.ZipFile(archive_source.path) as archive:
            for member in members:
                (destination / member).write_bytes(archive.read(member))

    service._bulk_extract_for = lambda _source: fake_bulk
    service.convert_to_canonical(
        source, tmp_path / "out.cbz", limits=ArchiveOpenLimits(max_operation_bytes=None)
    )

    assert caps == [None]


def test_each_container_reports_before_it_is_extracted(tmp_path: Path) -> None:
    """Staging a container emits no page events, so without this the caller's
    progress freezes for the whole extraction — once per container."""

    inner = _zip(tmp_path / "src" / "Vol01.cbz", {f"{i:03d}.png": _png() for i in range(2)})
    inner2 = _zip(tmp_path / "src" / "Vol02.cbz", {f"{i:03d}.png": _png("#993366") for i in range(2)})
    outer = _zip(
        tmp_path / "outer.cbz",
        {"Vol01.cbz": inner.read_bytes(), "Vol02.cbz": inner2.read_bytes()},
    )
    events: list[str] = []

    ArchiveImageService().convert_to_canonical(
        outer,
        tmp_path / "out.cbz",
        on_extract=lambda: events.append("extract"),
        on_page=lambda done, total: events.append(f"page{done}"),
    )

    # One extract per container, and each lands before that container's pages.
    assert events.count("extract") == 2
    assert events[0] == "extract"
    assert events.index("extract", 1) < events.index("page3")


def test_the_workspace_survives_a_cleanup_failure(tmp_path: Path) -> None:
    """Teardown runs after the artifact is written and verified.

    A file still held by a scanner would otherwise raise from the context
    manager and turn a finished conversion into a failed import.
    """

    import tempfile as tempfile_module

    source = _zip(tmp_path / "book.cbz", {"001.png": _png()})
    real = tempfile_module.TemporaryDirectory
    seen: list[bool] = []

    def recording(*args, **kwargs):
        seen.append(kwargs.get("ignore_cleanup_errors", False))
        return real(*args, **kwargs)

    from joyread.core.archive import service as service_module

    original = service_module.TemporaryDirectory
    service_module.TemporaryDirectory = recording
    try:
        ArchiveImageService().convert_to_canonical(source, tmp_path / "out.cbz")
    finally:
        service_module.TemporaryDirectory = original

    assert seen == [True]


def test_a_member_listed_twice_is_extracted_once_and_read_twice(
    tmp_path: Path,
) -> None:
    """A container may legally list one member twice, and the scan turns that
    into two pages sharing one staged file.

    The staged file must survive the first read and be unlinked only after the
    last, which is why uses are counted rather than pages.
    """

    page = _png()
    source = tmp_path / "dupe.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.png", page)
        archive.writestr("001.png", page)  # same name, listed twice

    service = ArchiveImageService()
    service._bulk_extract_for = lambda _source: None
    read_calls: list[int] = []
    original = service._read_entries

    def counted(archive_source, entries, **kwargs):
        read_calls.append(len(entries))
        return original(archive_source, entries, **kwargs)

    service._read_entries = counted

    result = service.convert_to_canonical(source, tmp_path / "out.cbz")

    # Extracted once...
    assert sum(read_calls) == 1
    # ...and written for both pages, under de-duplicated names.
    assert result.page_count == 2
    assert _names(tmp_path / "out.cbz") == ["001.png", "001-1.png"]
    with zipfile.ZipFile(tmp_path / "out.cbz") as written:
        assert written.read("001.png") == page
        assert written.read("001-1.png") == page
