"""The import gate: what the library is allowed to keep.

``probe_archive`` decides whether a *reader* may try a file; these cover the
stricter question importing has to answer. The load-bearing cases are the ones
where the shallow probe says yes and the gate must still say no -- an encrypted
archive nested inside a clean one, and a nested archive the scanner would
happily skip.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from PIL import Image

pyzipper = pytest.importorskip("pyzipper")

from joyread.core.archive import ArchiveImageService, ImportRejection
from joyread.core.archive.limits import ArchiveOpenLimits


def _png(size: tuple[int, int] = (10, 20)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, "#336699").save(buffer, format="PNG")
    return buffer.getvalue()


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _write_zip(path: Path, entries: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_zip_bytes(entries))
    return path


def _encrypted_zip_bytes(password: str = "secret") -> bytes:
    buffer = BytesIO()
    with pyzipper.AESZipFile(
        buffer, "w", compression=ZIP_DEFLATED, encryption=pyzipper.WZ_AES
    ) as archive:
        archive.setpassword(password.encode())
        archive.writestr("001.png", _png())
    return buffer.getvalue()


def _inspect(path: Path, **kwargs):
    return ArchiveImageService().inspect_for_import(path, **kwargs)


# ----------------------------------------------------------------------
# Encryption
# ----------------------------------------------------------------------


def test_a_clean_archive_is_accepted(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "clean.cbz", {"001.png": _png(), "002.png": _png()})

    result = _inspect(archive)

    assert result.accepted
    assert result.rejection is None
    assert result.image_count == 2


def test_an_encrypted_root_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "locked.cbz"
    archive.write_bytes(_encrypted_zip_bytes())

    result = _inspect(archive)

    assert not result.accepted
    assert result.rejection is ImportRejection.ENCRYPTED_ROOT


def test_an_encrypted_nested_archive_is_rejected(tmp_path: Path) -> None:
    """The case the shallow probe cannot see, and the reason this gate exists.

    ``probe_archive`` accepts this file: its own entries list fine and one of
    them merely looks like an archive. Importing it would put a book in the
    library that demands a password the moment it is opened.
    """

    archive = _write_zip(
        tmp_path / "outer.cbz",
        {"001.png": _png(), "inner.cbz": _encrypted_zip_bytes()},
    )

    assert ArchiveImageService().probe_archive(archive).is_valid is True

    result = _inspect(archive)

    assert not result.accepted
    assert result.rejection is ImportRejection.ENCRYPTED_NESTED
    assert result.rejected_at == "outer.cbz::inner.cbz"


def test_encryption_two_levels_down_is_still_rejected(tmp_path: Path) -> None:
    deep = _zip_bytes({"002.png": _png(), "locked.cbz": _encrypted_zip_bytes()})
    archive = _write_zip(tmp_path / "outer.cbz", {"001.png": _png(), "mid.cbz": deep})

    result = _inspect(archive, limits=ArchiveOpenLimits(nested_archive_max_depth=3))

    assert not result.accepted
    assert result.rejection is ImportRejection.ENCRYPTED_NESTED


def test_inspection_never_takes_a_password(tmp_path: Path) -> None:
    """The guarantee is structural: there is no provider parameter to pass."""

    archive = _write_zip(tmp_path / "clean.cbz", {"001.png": _png()})

    with pytest.raises(TypeError):
        _inspect(archive, password_provider=lambda _request: "secret")


# ----------------------------------------------------------------------
# Malformed children
# ----------------------------------------------------------------------


def test_a_malformed_nested_archive_is_rejected_not_skipped(tmp_path: Path) -> None:
    """The scanner skips an unreadable child so a reader still sees the pages
    that work. Importing cannot: the skipped branch would silently become pages
    the library does not have."""

    archive = _write_zip(
        tmp_path / "outer.cbz",
        {"001.png": _png(), "broken.cbz": b"PK\x03\x04 truncated garbage"},
    )

    result = _inspect(archive)

    assert not result.accepted
    assert result.rejection is ImportRejection.MALFORMED_CHILD
    assert "broken.cbz" in (result.rejected_at or "")


def test_a_malformed_root_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "broken.cbz"
    archive.write_bytes(b"not a zip at all")

    result = _inspect(archive)

    assert not result.accepted
    assert result.rejection is ImportRejection.MALFORMED_ROOT


# ----------------------------------------------------------------------
# Limits are rejections, not truncations
# ----------------------------------------------------------------------


def test_a_nested_archive_past_the_depth_limit_rejects(tmp_path: Path) -> None:
    """A bounded walk cannot prove an unbounded tree is clean.

    The reader stops descending and shows what it found; importing must not,
    because "we did not look" would become "the library is missing pages".
    """

    # Two levels of nesting against a limit of one. (Zero is not expressible:
    # ``ArchiveOpenLimits`` requires None or >= 1.)
    deep = _zip_bytes({"003.png": _png()})
    mid = _zip_bytes({"002.png": _png(), "deep.cbz": deep})
    archive = _write_zip(tmp_path / "outer.cbz", {"001.png": _png(), "mid.cbz": mid})

    result = _inspect(archive, limits=ArchiveOpenLimits(nested_archive_max_depth=1))

    assert not result.accepted
    assert result.rejection is ImportRejection.LIMIT_EXCEEDED
    assert "nested_archive_max_depth" in result.message
    assert "Settings" in result.message


def test_an_oversized_source_rejects_before_any_backend_runs(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "big.cbz", {"001.png": _png()})

    result = _inspect(archive, limits=ArchiveOpenLimits(max_source_bytes=1))

    assert not result.accepted
    assert result.rejection is ImportRejection.LIMIT_EXCEEDED


def test_an_unlimited_depth_setting_does_not_reject(tmp_path: Path) -> None:
    """``None`` is the user choosing no limit, not a limit of zero."""

    inner = _zip_bytes({"002.png": _png()})
    archive = _write_zip(tmp_path / "outer.cbz", {"001.png": _png(), "mid.cbz": inner})

    result = _inspect(archive, limits=ArchiveOpenLimits(nested_archive_max_depth=None))

    assert result.accepted
    assert result.image_count == 2
    assert result.nested_archive_count == 1


# ----------------------------------------------------------------------
# What the walk reports
# ----------------------------------------------------------------------


def test_an_archive_with_no_images_is_rejected(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "empty.cbz", {"readme.txt": b"nothing here"})

    result = _inspect(archive)

    assert not result.accepted
    assert result.rejection is ImportRejection.EMPTY


def test_nested_pages_and_depth_are_counted_across_the_whole_tree(tmp_path: Path) -> None:
    inner = _zip_bytes({"a.png": _png(), "b.png": _png()})
    archive = _write_zip(tmp_path / "outer.cbz", {"001.png": _png(), "vol.cbz": inner})

    result = _inspect(archive)

    assert result.accepted
    assert result.image_count == 3
    assert result.nested_archive_count == 1
    assert result.deepest_nesting == 1


def test_macos_junk_never_counts_as_content(tmp_path: Path) -> None:
    archive = _write_zip(
        tmp_path / "noisy.cbz",
        {
            "001.png": _png(),
            "__MACOSX/._001.png": b"resource fork",
            ".DS_Store": b"finder junk",
        },
    )

    result = _inspect(archive)

    assert result.accepted
    assert result.image_count == 1


# ----------------------------------------------------------------------
# Metadata is collected in the same pass
# ----------------------------------------------------------------------


def test_metadata_sidecars_are_read_during_the_walk(tmp_path: Path) -> None:
    """Collected here because the walk already has the container open; a later
    pass would have to materialize the parent chain again."""

    inner = _zip_bytes({"a.png": _png(), "ComicInfo.xml": b"<ComicInfo/>"})
    archive = _write_zip(
        tmp_path / "outer.cbz",
        {"001.png": _png(), "meta.json": b'{"title": {}}', "vol.cbz": inner},
    )

    result = _inspect(archive)

    assert result.accepted
    found = {(entry.name, entry.container) for entry in result.metadata_entries}
    assert ("meta.json", "outer.cbz") in {(n, Path(c).name) for n, c in found}
    assert any(name == "ComicInfo.xml" for name, _container in found)


def test_metadata_is_matched_case_insensitively(tmp_path: Path) -> None:
    archive = _write_zip(
        tmp_path / "a.cbz", {"001.png": _png(), "COMICINFO.XML": b"<ComicInfo/>"}
    )

    result = _inspect(archive)

    assert [entry.name for entry in result.metadata_entries] == ["COMICINFO.XML"]


def test_an_unreadable_metadata_entry_does_not_fail_the_import(tmp_path: Path, monkeypatch) -> None:
    """Metadata is an enhancement. Losing it must not cost the user a good book."""

    from joyread.core.archive import inspection as inspection_module
    from joyread.core.archive.errors import ArchiveReadError

    archive = _write_zip(tmp_path / "a.cbz", {"001.png": _png(), "meta.json": b"{}"})
    service = ArchiveImageService()
    original = service._read_entry

    def _fail_on_metadata(source, name, password, **kwargs):
        if name.endswith("meta.json"):
            raise ArchiveReadError("nope")
        return original(source, name, password, **kwargs)

    monkeypatch.setattr(service, "_read_entry", _fail_on_metadata)

    result = service.inspect_for_import(archive)

    assert result.accepted
    assert result.metadata_entries == ()


def test_an_oversized_metadata_entry_is_skipped_not_read(tmp_path: Path) -> None:
    from joyread.core.archive.inspection import METADATA_MAX_BYTES

    archive = _write_zip(
        tmp_path / "a.cbz",
        {"001.png": _png(), "meta.json": b"x" * (METADATA_MAX_BYTES + 1)},
    )

    result = _inspect(archive)

    assert result.accepted
    assert result.metadata_entries == ()


# ----------------------------------------------------------------------
# Read failures while descending
#
# ZIP flags any encrypted member at listing time, so those are caught before a
# read is attempted. 7Z and RAR can instead fail on the read of one member, and
# a member can be listable but have a corrupt payload. Both land in the
# descend path, so they are driven through the inspector's own seams rather
# than by constructing format-specific broken archives.
# ----------------------------------------------------------------------


def _inspector_over(entries, read):
    from joyread.core.archive.inspection import ArchiveImportInspector
    from joyread.core.archive.records import ArchiveContainerProbe, ArchiveEntry

    def probe(source):
        return ArchiveContainerProbe(
            entries=tuple(ArchiveEntry(name, size, None) for name, size in entries),
            is_encrypted=False,
        )

    return ArchiveImportInspector(probe, read)


def _run(inspector, *, suffix=".cbz"):
    from joyread.core.archive.limits import ArchiveOperationBudget
    from joyread.core.archive.records import ArchiveSource

    limits = ArchiveOpenLimits()
    return inspector.inspect(
        ArchiveSource(label="outer.cbz", suffix=suffix, path=Path("outer.cbz")),
        limits=limits,
        budget=ArchiveOperationBudget(limits.max_operation_bytes),
    )


def test_a_password_demanded_while_reading_a_child_rejects(tmp_path: Path) -> None:
    from joyread.core.archive.errors import ArchivePasswordRequired

    def read(source, name, password, **kwargs):
        raise ArchivePasswordRequired("locked", archive_path=name)

    result = _run(_inspector_over([("001.png", 10), ("inner.cb7", 10)], read))

    assert not result.accepted
    assert result.rejection is ImportRejection.ENCRYPTED_NESTED
    assert result.rejected_at == "outer.cbz::inner.cb7"


def test_a_child_whose_payload_cannot_be_extracted_rejects(tmp_path: Path) -> None:
    from joyread.core.archive.errors import ArchiveReadError

    def read(source, name, password, **kwargs):
        raise ArchiveReadError("bad crc")

    result = _run(_inspector_over([("001.png", 10), ("inner.cbz", 10)], read))

    assert not result.accepted
    assert result.rejection is ImportRejection.MALFORMED_CHILD


def test_a_missing_backend_for_a_child_rejects(tmp_path: Path) -> None:
    from joyread.core.archive.errors import ArchiveDependencyMissing

    def read(source, name, password, **kwargs):
        raise ArchiveDependencyMissing("no rar backend")

    result = _run(_inspector_over([("001.png", 10), ("inner.cbr", 10)], read))

    assert not result.accepted
    assert result.rejection is ImportRejection.DEPENDENCY_MISSING


def test_an_oversized_child_rejects_before_it_is_read(tmp_path: Path) -> None:
    """The size is declared in the listing, so the limit is enforced without
    materializing the payload it would have exceeded."""

    from joyread.core.archive.limits import ArchiveOperationBudget
    from joyread.core.archive.records import ArchiveSource

    reads: list[str] = []

    def read(source, name, password, **kwargs):
        reads.append(name)
        return b""

    inspector = _inspector_over([("001.png", 10), ("huge.cbz", 10_000)], read)
    limits = ArchiveOpenLimits(max_extracted_item_bytes=1_000)
    result = inspector.inspect(
        ArchiveSource(label="outer.cbz", suffix=".cbz", path=Path("outer.cbz")),
        limits=limits,
        budget=ArchiveOperationBudget(limits.max_operation_bytes),
    )

    assert not result.accepted
    assert result.rejection is ImportRejection.LIMIT_EXCEEDED
    assert reads == []


def test_a_page_in_a_too_deep_folder_rejects(tmp_path: Path) -> None:
    """``global_file_max_depth`` bounds folder depth, not just nesting.

    The scanner silently drops an over-deep page and reads on. For an import
    that dropped page is one the library would never have, so it rejects.
    """

    archive = _write_zip(
        tmp_path / "deep.cbz", {"001.png": _png(), "a/b/c/002.png": _png()}
    )

    result = _inspect(archive, limits=ArchiveOpenLimits(global_file_max_depth=1))

    assert not result.accepted
    assert result.rejection is ImportRejection.LIMIT_EXCEEDED
    assert "global_file_max_depth" in result.message


def test_a_nested_archive_in_a_too_deep_folder_rejects(tmp_path: Path) -> None:
    inner = _zip_bytes({"002.png": _png()})
    archive = _write_zip(
        tmp_path / "outer.cbz", {"001.png": _png(), "a/b/c/inner.cbz": inner}
    )

    result = _inspect(archive, limits=ArchiveOpenLimits(global_file_max_depth=2))

    assert not result.accepted
    assert result.rejection is ImportRejection.LIMIT_EXCEEDED


def test_folder_depth_within_the_limit_is_accepted(tmp_path: Path) -> None:
    """Guards the pair above against passing for the wrong reason."""

    archive = _write_zip(
        tmp_path / "deep.cbz", {"001.png": _png(), "a/b/c/002.png": _png()}
    )

    result = _inspect(archive, limits=ArchiveOpenLimits(global_file_max_depth=3))

    assert result.accepted
    assert result.image_count == 2


def test_a_too_deep_nested_archive_is_refused_before_it_is_materialized() -> None:
    """A page inside it would be caught anyway, one level further on.

    The guard earns its place by firing first: without it the child is read
    into memory and charged to the operation budget before anything notices it
    was always going to be refused.
    """

    from joyread.core.archive.limits import ArchiveOperationBudget
    from joyread.core.archive.records import ArchiveSource

    reads: list[str] = []

    def read(source, name, password, **kwargs):
        reads.append(name)
        return _zip_bytes({"002.png": _png()})

    inspector = _inspector_over([("001.png", 10), ("a/b/c/inner.cbz", 10)], read)
    limits = ArchiveOpenLimits(global_file_max_depth=2)
    result = inspector.inspect(
        ArchiveSource(label="outer.cbz", suffix=".cbz", path=Path("outer.cbz")),
        limits=limits,
        budget=ArchiveOperationBudget(limits.max_operation_bytes),
    )

    assert not result.accepted
    assert result.rejection is ImportRejection.LIMIT_EXCEEDED
    assert reads == []


# ----------------------------------------------------------------------
# Metadata caps
#
# Every cap here drops a sidecar and reads on. Metadata is an enhancement, so
# hitting a bound must never cost the user a book that is otherwise fine.
# ----------------------------------------------------------------------


def _collect(entries, read, *, limits=None):
    from joyread.core.archive.limits import ArchiveOperationBudget
    from joyread.core.archive.records import ArchiveSource

    limits = limits or ArchiveOpenLimits()
    return _inspector_over(entries, read).inspect(
        ArchiveSource(label="a.cbz", suffix=".cbz", path=Path("a.cbz")),
        limits=limits,
        budget=ArchiveOperationBudget(limits.max_operation_bytes),
    )


def test_an_undeclared_metadata_size_cannot_smuggle_a_huge_payload() -> None:
    """The declared size is attacker-controlled and 7Z/RAR often omit it.

    Trusting it alone let the entry be read under the archive-wide 1 GiB item
    limit and then held in the result for the rest of the import.
    """

    from joyread.core.archive.inspection import METADATA_MAX_BYTES

    oversized = b"x" * (METADATA_MAX_BYTES + 1)

    def read(source, name, password, **kwargs):
        return oversized if name.endswith("meta.json") else b""

    result = _collect(
        [("001.png", 10), ("meta.json", None)],  # size None: nothing declared
        read,
    )

    assert result.accepted
    assert result.metadata_entries == ()


def test_a_metadata_entry_that_under_declares_its_size_is_still_capped() -> None:
    """Same hole, reached by lying rather than by staying silent."""

    from joyread.core.archive.inspection import METADATA_MAX_BYTES

    oversized = b"x" * (METADATA_MAX_BYTES + 1)

    def read(source, name, password, **kwargs):
        return oversized if name.endswith("meta.json") else b""

    result = _collect([("001.png", 10), ("meta.json", 12)], read)

    assert result.accepted
    assert result.metadata_entries == ()


def test_the_read_is_bounded_before_the_payload_is_materialized() -> None:
    """The post-read length check is a backstop, not the primary defence.

    The entry is read under a metadata-sized item limit so a backend that
    honours limits never allocates the oversized payload at all.
    """

    from joyread.core.archive.inspection import METADATA_MAX_BYTES

    seen: list[int | None] = []

    def read(source, name, password, **kwargs):
        seen.append(kwargs["limits"].max_extracted_item_bytes)
        return b"{}"

    _collect([("001.png", 10), ("meta.json", 2)], read)

    assert seen == [METADATA_MAX_BYTES]


def test_a_backend_limit_error_on_metadata_does_not_fail_the_import() -> None:
    from joyread.core.archive.errors import ArchiveResourceLimitError

    def read(source, name, password, **kwargs):
        raise ArchiveResourceLimitError(
            "extracted_item_bytes", actual=99, maximum=1, subject=name
        )

    result = _collect([("001.png", 10), ("meta.json", 2)], read)

    assert result.accepted
    assert result.metadata_entries == ()


def test_many_small_sidecars_are_bounded_in_total() -> None:
    """A legal archive may hold any number of ``folder-N/meta.json`` files.

    Nested archive payloads are freed as the recursion unwinds; anything kept
    in the result stays resident for the whole import, so the collection needs
    a total bound and not just a per-entry one.
    """

    from joyread.core.archive.inspection import (
        METADATA_MAX_ENTRIES,
        METADATA_TOTAL_MAX_BYTES,
    )

    one_mib = b"y" * (1024 * 1024)

    def read(source, name, password, **kwargs):
        return one_mib if name.endswith("meta.json") else b""

    entries = [("001.png", 10)] + [
        (f"folder-{index}/meta.json", len(one_mib)) for index in range(500)
    ]

    result = _collect(entries, read)

    assert result.accepted
    retained = sum(len(entry.data) for entry in result.metadata_entries)
    assert retained <= METADATA_TOTAL_MAX_BYTES
    assert len(result.metadata_entries) <= METADATA_MAX_ENTRIES


def test_the_entry_count_is_bounded_even_for_tiny_sidecars() -> None:
    """Bytes alone would not stop 100k one-byte sidecars."""

    from joyread.core.archive.inspection import METADATA_MAX_ENTRIES

    def read(source, name, password, **kwargs):
        return b"{}"

    entries = [("001.png", 10)] + [
        (f"folder-{index}/meta.json", 2) for index in range(METADATA_MAX_ENTRIES * 4)
    ]

    result = _collect(entries, read)

    assert result.accepted
    assert len(result.metadata_entries) == METADATA_MAX_ENTRIES


def test_an_honestly_declared_oversize_sidecar_is_never_read() -> None:
    """The post-read backstop would catch it anyway.

    The declared-size check earns its place by firing first: when the listing
    is truthful there is no reason to ask the backend for a payload we already
    know we will discard.
    """

    from joyread.core.archive.inspection import METADATA_MAX_BYTES

    reads: list[str] = []

    def read(source, name, password, **kwargs):
        reads.append(name)
        return b"{}"

    result = _collect(
        [("001.png", 10), ("meta.json", METADATA_MAX_BYTES + 1)],
        read,
    )

    assert result.accepted
    assert reads == []


def test_the_total_cap_holds_when_a_sidecar_straddles_the_boundary() -> None:
    """The cap has to be arithmetic on the read, not a "still under it" test.

    Sidecars of uneven size land the running total *near* the ceiling rather
    than on it, and admitting one more whole item from there overshoots by
    almost its full size.
    """

    from joyread.core.archive.inspection import METADATA_TOTAL_MAX_BYTES

    half_mib = 512 * 1024
    one_mib = 1024 * 1024
    sizes = [half_mib] * 15 + [one_mib] * 4

    def read(source, name, password, **kwargs):
        limit = kwargs["limits"].max_extracted_item_bytes
        payload = b"y" * sizes[int(name.split("-")[1].split("/")[0])]
        if limit is not None and len(payload) > limit:
            from joyread.core.archive.errors import ArchiveResourceLimitError

            raise ArchiveResourceLimitError(
                "extracted_item_bytes", actual=len(payload), maximum=limit, subject=name
            )
        return payload

    entries = [("001.png", 10)] + [
        (f"folder-{index}/meta.json", size) for index, size in enumerate(sizes)
    ]

    result = _collect(entries, read)

    retained = sum(len(entry.data) for entry in result.metadata_entries)
    assert result.accepted
    assert retained <= METADATA_TOTAL_MAX_BYTES


def test_a_sidecars_directory_survives_into_the_result() -> None:
    """``chapter/ComicInfo.xml`` and a root ``ComicInfo.xml`` are different
    claims about the book, and the basename alone cannot tell them apart."""

    def read(source, name, password, **kwargs):
        return b"{}"

    result = _collect(
        [("001.png", 10), ("chapter-01/ComicInfo.xml", 2), ("ComicInfo.xml", 2)], read
    )

    found = {entry.path: entry.name for entry in result.metadata_entries}
    assert found == {
        "chapter-01/ComicInfo.xml": "ComicInfo.xml",
        "ComicInfo.xml": "ComicInfo.xml",
    }
