from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import subprocess
from threading import Event, Lock, Thread
import time
from zipfile import ZIP_DEFLATED, ZipFile

import py7zr
import pyzipper
import pytest
from PIL import Image

from joyread.core.archive import (
    ArchiveAccessMode,
    ArchiveCorruptError,
    ArchiveDependencyMissing,
    ArchiveEmptyError,
    ArchiveImageService,
    ArchiveImageSession,
    ArchivePasswordRejected,
    ArchivePasswordRequired,
    ArchivePasswordPolicy,
    ArchivePasswordResponse,
    ArchiveReadError,
    ArchiveOpenLimits,
    ArchiveResourceLimitError,
    ArchiveUnsupportedFormat,
    ArchiveValidationCode,
    ExtractionBackendResolver,
)
from joyread.core.archive.backends import SEVEN_ZIP_ENV_VAR
from joyread.core.archive.formats import common as archive_common
from joyread.core.archive.formats.seven_zip_backend import _BudgetedBytesFactory
from joyread.core.archive.limits import ArchiveOperationBudget
from joyread.core.archive.records import ArchiveSource, PageRecord
from joyread.core.archive.scanner import SCANNER_SCHEMA_VERSION
from joyread.core.services.archive_extraction_pool import ArchiveExtractionPool


def _png_bytes(size: tuple[int, int], color: str = "#ffffff") -> bytes:
    image = Image.new("RGB", size, color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _write_zip(path: Path, entries: dict[str, bytes]) -> None:
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class _FakePopen:
    """Small Popen stand-in for bounded external-extractor tests."""

    def __init__(
        self,
        *,
        stdout_target: object,
        stderr_target: object,
        stdout_data: bytes = b"",
        stderr_data: bytes = b"",
        returncode: int = 0,
    ) -> None:
        self.stdout = BytesIO(stdout_data) if stdout_target == subprocess.PIPE else None
        self.stderr = BytesIO(stderr_data) if stderr_target == subprocess.PIPE else None
        self._returncode = returncode
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self._returncode

    def kill(self) -> None:
        self.killed = True


class _TrackingLease:
    document_cache_key = "file:two-phase-close"
    # The session asks its lease for the shared budget when it decides the
    # document's cache policy.
    cache_max_bytes = 1 << 30

    def __init__(self) -> None:
        self.is_closed = False
        self.close_count = 0
        self.put_count = 0

    def get(self, _entry_name: str) -> bytes | None:
        return None

    def get_many(self, _entry_names: tuple[str, ...]) -> dict[str, bytes]:
        return {}

    def put_many(self, payloads: dict[str, bytes]) -> None:
        if self.is_closed:
            raise AssertionError("cache write happened after lease close")
        self.put_count += len(payloads)

    def is_complete(self, _page_count: int, _signature: str) -> bool:
        return False

    def close(self) -> None:
        self.close_count += 1
        self.is_closed = True


def _encrypted_cbz_bytes(password: str = "secret", image_size: tuple[int, int] = (32, 16)) -> bytes:
    buffer = BytesIO()
    with pyzipper.AESZipFile(
        buffer,
        "w",
        compression=ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as archive:
        archive.setpassword(password.encode("utf-8"))
        archive.writestr("001.png", _png_bytes(image_size))
    return buffer.getvalue()


def _write_encrypted_cbz(path: Path, password: str = "secret", image_size: tuple[int, int] = (32, 16)) -> None:
    path.write_bytes(_encrypted_cbz_bytes(password, image_size))


def test_cbz_discovers_images_and_naturally_sorts_each_directory(tmp_path: Path) -> None:
    archive_path = tmp_path / "sample.cbz"
    _write_zip(
        archive_path,
        {
            "10.png": _png_bytes((30, 10)),
            "2.png": _png_bytes((20, 10)),
            "notes.txt": b"ignored",
            "chapter/a10.png": _png_bytes((110, 10)),
            "chapter/a2.png": _png_bytes((102, 10)),
            "chapter/a1.png": _png_bytes((101, 10)),
            "deep/a/b/c/d/e/f/001.png": _png_bytes((201, 10)),
            "../unsafe.png": _png_bytes((255, 10)),
        },
    )

    resolver = ExtractionBackendResolver(tmp_path / "empty-extractors")
    session = ArchiveImageService(backend_resolver=resolver).open(archive_path)

    assert session.page_count == 6
    assert [session.get_dimensions(index) for index in session.index_range] == [
        (20, 10),
        (30, 10),
        (101, 10),
        (102, 10),
        (110, 10),
        (201, 10),
    ]



def test_session_bounds_ranged_reads_dimensions_and_navigation(tmp_path: Path) -> None:
    archive_path = tmp_path / "bounds.cbz"
    _write_zip(
        archive_path,
        {
            "001.png": _png_bytes((20, 10)),
            "002.png": _png_bytes((30, 10)),
        },
    )

    resolver = ExtractionBackendResolver(tmp_path / "empty-extractors")
    session = ArchiveImageService(backend_resolver=resolver).open(archive_path)

    assert session.is_not_empty()
    assert list(session.index_range) == [0, 1]
    assert session.get_image(-1) is None
    assert session.get_image(session.page_count) is None
    assert session.get_images(-1, 4)[0] is None
    assert session.get_images(-1, 4)[-1] is None
    assert session.get_aspect_ratio(0) == (2.0, 1.0)
    assert session.get_horizontal_aspect_ratio([0, 1]) == (5.0, 1.0)
    assert session.current() == session.get_image(0)
    assert session.next() == session.get_image(1)
    assert session.current_index == 1
    assert session.next() is None
    assert session.previous() == session.get_image(0)
    assert session.seek(99) is False
    assert session.seek(1) is True


def test_session_returns_dimensions_without_mutating_page_records(tmp_path: Path) -> None:
    archive_path = tmp_path / "memory.cbz"
    _write_zip(
        archive_path,
        {
            "001.png": _png_bytes((20, 10)),
            "002.png": _png_bytes((30, 10)),
        },
    )

    resolver = ExtractionBackendResolver(tmp_path / "empty-extractors")
    session = ArchiveImageService(backend_resolver=resolver).open(archive_path)
    pages = session.get_pages((0, 1))

    assert [page.dimensions if page is not None else None for page in pages] == [(20, 10), (30, 10)]
    assert all(not hasattr(record, "dimensions") for record in session._pages)
    assert all(not hasattr(record, "_page") for record in session._pages)


def test_session_close_defers_lease_release_until_registered_read_finishes(tmp_path: Path) -> None:
    source = ArchiveSource(
        label="slow.7z",
        suffix=".7z",
        path=tmp_path / "slow.7z",
    )
    record = PageRecord("001.png", source, "001.png", None, size=128)
    started = Event()
    release = Event()
    lease = _TrackingLease()
    payload = _png_bytes((20, 10))

    def read_entries(_source, entries, _budget):  # noqa: ANN001, ANN202
        started.set()
        assert release.wait(timeout=2)
        return {name: payload for name, _password in entries}

    session = ArchiveImageSession(
        (record,),
        read_entries,
        cache_lease=lease,  # type: ignore[arg-type]
    )
    results: list[object] = []
    worker = Thread(target=lambda: results.extend(session.read_pages((0,))))
    worker.start()
    assert started.wait(timeout=2)

    session.close()

    assert lease.close_count == 0
    assert session.read_pages((0,)) == [None]
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert results[0] is not None
    assert lease.put_count == 1
    assert lease.close_count == 1
    session.close()
    assert lease.close_count == 1


def test_session_close_finalizes_after_a_registered_read_failure(tmp_path: Path) -> None:
    source = ArchiveSource(label="broken.7z", suffix=".7z", path=tmp_path / "broken.7z")
    record = PageRecord("001.png", source, "001.png", None, size=128)
    started = Event()
    release = Event()
    lease = _TrackingLease()

    def fail_read(_source, _entries, _budget):  # noqa: ANN001, ANN202
        started.set()
        assert release.wait(timeout=2)
        raise ArchiveReadError("simulated failure")

    session = ArchiveImageSession(
        (record,),
        fail_read,
        cache_lease=lease,  # type: ignore[arg-type]
    )
    failures: list[Exception] = []

    def run() -> None:
        try:
            session.read_pages((0,))
        except Exception as exc:  # noqa: BLE001 - assertion helper captures worker failure.
            failures.append(exc)

    worker = Thread(target=run)
    worker.start()
    assert started.wait(timeout=2)
    session.close()
    release.set()
    worker.join(timeout=2)

    assert isinstance(failures[0], ArchiveReadError)
    assert lease.close_count == 1


def test_nested_cbz_pages_follow_root_pages_and_create_contents(tmp_path: Path) -> None:
    nested_buffer = BytesIO()
    with ZipFile(nested_buffer, "w", compression=ZIP_DEFLATED) as nested:
        nested.writestr("001.png", _png_bytes((40, 10)))

    archive_path = tmp_path / "nested.cbz"
    _write_zip(
        archive_path,
        {
            "001.png": _png_bytes((20, 10)),
            "nested.cbz": nested_buffer.getvalue(),
        },
    )

    session = ArchiveImageService().open(archive_path)

    assert session.page_count == 2
    assert [session.get_dimensions(index) for index in session.index_range] == [(20, 10), (40, 10)]
    assert [(item.label, item.page_index, item.depth) for item in session.contents] == [("nested", 1, 0)]


def test_nested_archives_sort_as_folder_nodes_and_expose_internal_contents(tmp_path: Path) -> None:
    archive_path = tmp_path / "nested-order.cbz"
    _write_zip(
        archive_path,
        {
            "Volume10.cbz": _zip_bytes({"Wrapper/Chapter2/1.png": _png_bytes((102, 10))}),
            "Volume2.cbz": _zip_bytes({"Wrapper/Chapter1/1.png": _png_bytes((21, 10))}),
        },
    )

    session = ArchiveImageService().open(archive_path)

    assert [session.get_dimensions(index) for index in session.index_range] == [(21, 10), (102, 10)]
    assert [(item.label, item.page_index, item.depth) for item in session.contents] == [
        ("Volume2", 0, 0),
        ("Chapter1", 0, 1),
        ("Volume10", 1, 0),
        ("Chapter2", 1, 1),
    ]


def test_nested_archive_label_keeps_extension_when_it_collides_with_folder(tmp_path: Path) -> None:
    archive_path = tmp_path / "collision.cbz"
    _write_zip(
        archive_path,
        {
            "Bonus/1.png": _png_bytes((10, 10)),
            "Bonus.cbz": _zip_bytes({"1.png": _png_bytes((20, 10))}),
        },
    )

    session = ArchiveImageService().open(archive_path)

    assert [(item.label, item.page_index, item.depth) for item in session.contents] == [
        ("Bonus", 0, 0),
        ("Bonus.cbz", 1, 0),
    ]


def test_unreadable_nested_archive_is_skipped_without_a_contents_entry(tmp_path: Path) -> None:
    archive_path = tmp_path / "unreadable-nested.cbz"
    _write_zip(
        archive_path,
        {
            "1.png": _png_bytes((10, 10)),
            "broken.cbz": b"not a zip archive",
        },
    )

    session = ArchiveImageService().open(archive_path)

    assert session.page_count == 1
    assert session.contents == ()


def test_nested_and_global_depth_limits_apply_independently(tmp_path: Path) -> None:
    level_three = _zip_bytes({"3.png": _png_bytes((30, 10))})
    level_two = _zip_bytes({"2.png": _png_bytes((20, 10)), "level3.cbz": level_three})
    level_one = _zip_bytes(
        {
            "Wrapper/1.png": _png_bytes((10, 10)),
            "Wrapper/level2.cbz": level_two,
        }
    )
    archive_path = tmp_path / "depths.cbz"
    _write_zip(
        archive_path,
        {
            "0.png": _png_bytes((1, 10)),
            "level1.cbz": level_one,
        },
    )

    nested_limited = ArchiveImageService().open(
        archive_path,
        max_nested_depth=1,
        global_file_max_depth=-1,
    )
    global_limited = ArchiveImageService().open(
        archive_path,
        max_nested_depth=-1,
        global_file_max_depth=1,
    )
    unlimited = ArchiveImageService().open(
        archive_path,
        max_nested_depth=-1,
        global_file_max_depth=-1,
    )

    assert [nested_limited.get_dimensions(index) for index in nested_limited.index_range] == [(1, 10), (10, 10)]
    assert [global_limited.get_dimensions(index) for index in global_limited.index_range] == [(1, 10)]
    assert [unlimited.get_dimensions(index) for index in unlimited.index_range] == [
        (1, 10),
        (10, 10),
        (20, 10),
        (30, 10),
    ]


def test_legacy_max_depth_alias_targets_nested_archives_and_rejects_conflicts(tmp_path: Path) -> None:
    archive_path = tmp_path / "alias.cbz"
    _write_zip(
        archive_path,
        {"nested.cbz": _zip_bytes({"1.png": _png_bytes((10, 10))})},
    )

    session = ArchiveImageService().open(archive_path, max_depth=1)

    assert session.page_count == 1
    with pytest.raises(ValueError, match="must match"):
        ArchiveImageService().open(archive_path, max_depth=1, max_nested_depth=2)


def test_explicit_archive_limits_reject_conflicting_legacy_depth_parameters(tmp_path: Path) -> None:
    archive_path = tmp_path / "limits-alias.cbz"
    _write_zip(archive_path, {"nested.cbz": _zip_bytes({"1.png": _png_bytes((10, 10))})})
    limits = ArchiveOpenLimits(
        nested_archive_max_depth=1,
        global_file_max_depth=100,
        max_source_bytes=None,
        max_extracted_item_bytes=None,
        max_operation_bytes=None,
        max_image_pixels=None,
        external_command_timeout_seconds=None,
    )

    assert ArchiveImageService().open(archive_path, limits=limits).page_count == 1
    with pytest.raises(ValueError, match="must match"):
        ArchiveImageService().open(archive_path, limits=limits, max_nested_depth=2)


def test_archive_open_limits_use_none_not_negative_values_for_unlimited() -> None:
    with pytest.raises(ValueError, match="max_source_bytes"):
        ArchiveOpenLimits(max_source_bytes=-1)
    with pytest.raises(ValueError, match="nested_archive_max_depth"):
        ArchiveOpenLimits(nested_archive_max_depth=-1)

    unlimited = ArchiveOpenLimits(
        nested_archive_max_depth=None,
        max_source_bytes=None,
        max_extracted_item_bytes=None,
        max_operation_bytes=None,
        max_image_pixels=None,
        external_command_timeout_seconds=None,
    )

    assert unlimited.nested_archive_max_depth is None
    assert unlimited.max_source_bytes is None


def test_rarfile_configuration_lock_is_shared_by_archive_service_instances() -> None:
    assert ArchiveImageService()._rar_lock is ArchiveImageService()._rar_lock


def test_archive_source_size_limit_rejects_before_scanning_and_validates_cleanly(tmp_path: Path) -> None:
    archive_path = tmp_path / "too-large.cbz"
    _write_zip(archive_path, {"1.png": _png_bytes((10, 10))})
    limits = ArchiveOpenLimits(max_source_bytes=1)

    with pytest.raises(ArchiveResourceLimitError) as error:
        ArchiveImageService().open(archive_path, limits=limits)

    assert error.value.limit == "source_bytes"
    validation = ArchiveImageService().probe_archive(archive_path, limits=limits)
    assert validation.code == ArchiveValidationCode.RESOURCE_LIMIT_EXCEEDED
    assert validation.error_type == ArchiveResourceLimitError.__name__


def test_archive_declared_item_limit_rejects_before_page_materialization(tmp_path: Path) -> None:
    archive_path = tmp_path / "large-entry.cbz"
    _write_zip(archive_path, {"1.png": _png_bytes((40, 20))})
    limits = ArchiveOpenLimits(
        max_source_bytes=None,
        max_extracted_item_bytes=1,
        max_operation_bytes=None,
        max_image_pixels=None,
        external_command_timeout_seconds=None,
    )

    with pytest.raises(ArchiveResourceLimitError) as error:
        ArchiveImageService().open(archive_path, limits=limits)

    assert error.value.limit == "extracted_item_bytes"


def test_archive_page_read_enforces_cumulative_operation_budget(tmp_path: Path) -> None:
    archive_path = tmp_path / "operation-budget.cbz"
    _write_zip(
        archive_path,
        {
            "1.png": _png_bytes((40, 20)),
            "2.png": _png_bytes((40, 20)),
        },
    )
    limits = ArchiveOpenLimits(
        max_source_bytes=None,
        max_extracted_item_bytes=None,
        max_operation_bytes=1,
        max_image_pixels=None,
        external_command_timeout_seconds=None,
    )
    session = ArchiveImageService().open(archive_path, limits=limits)

    with pytest.raises(ArchiveResourceLimitError) as error:
        session.get_pages((0, 1))

    assert error.value.limit == "operation_bytes"


def test_archive_image_pixels_are_checked_after_cache_hits(tmp_path: Path) -> None:
    archive_path = tmp_path / "pixel-limit.cb7"
    with py7zr.SevenZipFile(archive_path, "w") as archive:
        archive.writestr(_png_bytes((40, 20)), "1.png")
    service = ArchiveImageService(page_cache_dir=tmp_path / "archive-pages")

    assert service.open(archive_path).get_page(0) is not None
    strict_limits = ArchiveOpenLimits(
        max_source_bytes=None,
        max_extracted_item_bytes=None,
        max_operation_bytes=None,
        max_image_pixels=1,
        external_command_timeout_seconds=None,
    )
    strict_session = service.open(archive_path, limits=strict_limits)

    with pytest.raises(ArchiveResourceLimitError) as error:
        strict_session.get_page(0)

    assert error.value.limit == "image_pixels"


def test_extraction_cache_signature_includes_scanner_schema_and_limits(tmp_path: Path) -> None:
    archive_path = tmp_path / "signature.cbz"
    _write_zip(archive_path, {"1.png": _png_bytes((10, 10))})
    service = ArchiveImageService(page_cache_dir=tmp_path / "archive-pages")

    default_session = service.open(archive_path)
    relaxed_session = service.open(
        archive_path,
        limits=ArchiveOpenLimits(max_operation_bytes=None),
    )

    assert f"scanner-v{SCANNER_SCHEMA_VERSION}" in default_session._cache_signature
    assert default_session._cache_signature != relaxed_session._cache_signature


def test_archive_global_file_depth_counts_normal_folders_from_archive_root(tmp_path: Path) -> None:
    archive_path = tmp_path / "depth.cbz"
    _write_zip(
        archive_path,
        {
            "001.png": _png_bytes((10, 10)),
            "chapter/002.png": _png_bytes((20, 10)),
            "chapter/deep/003.png": _png_bytes((30, 10)),
        },
    )

    shallow_session = ArchiveImageService().open(archive_path, global_file_max_depth=1)
    deep_session = ArchiveImageService().open(archive_path, global_file_max_depth=2)

    assert [shallow_session.get_dimensions(index) for index in shallow_session.index_range] == [(10, 10), (20, 10)]
    assert [deep_session.get_dimensions(index) for index in deep_session.index_range] == [
        (10, 10),
        (20, 10),
        (30, 10),
    ]


def test_archive_global_depth_1000_uses_iterative_contents_flattening(tmp_path: Path) -> None:
    archive_path = tmp_path / "deep-tree.cbz"
    depth_1000 = "/".join(["d"] * 1000)
    depth_1001 = f"{depth_1000}/d"
    _write_zip(
        archive_path,
        {
            "0.png": _png_bytes((1, 10)),
            f"{depth_1000}/1.png": _png_bytes((10, 10)),
            f"{depth_1001}/2.png": _png_bytes((20, 10)),
        },
    )

    session = ArchiveImageService().open(archive_path, global_file_max_depth=1000)

    assert session.page_count == 2
    assert len(session.contents) == 1000
    assert session.contents[-1].page_index == 1
    assert session.contents[-1].depth == 999


def test_archive_single_root_folder_is_transparent_but_counts_toward_global_depth(tmp_path: Path) -> None:
    archive_path = tmp_path / "wrapped.cbz"
    _write_zip(
        archive_path,
        {
            "Book/001.png": _png_bytes((10, 10)),
            "Book/chapter/002.png": _png_bytes((20, 10)),
            "Book/chapter/deep/003.png": _png_bytes((30, 10)),
        },
    )

    session = ArchiveImageService().open(archive_path)
    shallow_session = ArchiveImageService().open(archive_path, global_file_max_depth=1)

    assert [session.get_dimensions(index) for index in session.index_range] == [(10, 10), (20, 10), (30, 10)]
    assert [shallow_session.get_dimensions(index) for index in shallow_session.index_range] == [(10, 10)]
    assert [(item.label, item.page_index, item.depth) for item in session.contents] == [
        ("chapter", 1, 0),
        ("deep", 2, 1),
    ]


def test_archive_skips_macos_metadata_entries(tmp_path: Path) -> None:
    archive_path = tmp_path / "metadata.cbz"
    _write_zip(
        archive_path,
        {
            "__MACOSX/._001.png": b"not an image",
            "._002.png": b"not an image",
            ".DS_Store": b"ignored",
            "001.png": _png_bytes((10, 10)),
        },
    )

    session = ArchiveImageService().open(archive_path)

    assert session.page_count == 1
    assert session.get_dimensions(0) == (10, 10)


def test_archive_natural_sort_handles_mixed_chinese_and_numeric_names(tmp_path: Path) -> None:
    archive_path = tmp_path / "10 十级中坦战车娘.zip"
    _write_zip(
        archive_path,
        {
            "10 十级中坦战车娘/0封面2.jpg": _png_bytes((20, 10)),
            "10 十级中坦战车娘/0封面1.jpg": _png_bytes((10, 10)),
            "10 十级中坦战车娘/STB-1.jpg": _png_bytes((30, 10)),
        },
    )

    session = ArchiveImageService().open(archive_path)

    assert [session.get_dimensions(index) for index in session.index_range] == [(10, 10), (20, 10), (30, 10)]


def test_archive_dfs_outputs_direct_pages_before_naturally_sorted_child_folders(tmp_path: Path) -> None:
    archive_path = tmp_path / "chapters.cbz"
    _write_zip(
        archive_path,
        {
            "Chapter10/1.png": _png_bytes((101, 10)),
            "Chapter2/Sub10/1.png": _png_bytes((210, 10)),
            "Chapter2/10.png": _png_bytes((2010, 10)),
            "Chapter2/2.png": _png_bytes((202, 10)),
            "Chapter1/1.png": _png_bytes((11, 10)),
            "0.png": _png_bytes((1, 10)),
        },
    )

    session = ArchiveImageService().open(archive_path)

    assert [session.get_dimensions(index) for index in session.index_range] == [
        (1, 10),
        (11, 10),
        (202, 10),
        (2010, 10),
        (210, 10),
        (101, 10),
    ]
    assert [(item.label, item.page_index, item.depth) for item in session.contents] == [
        ("Chapter1", 1, 0),
        ("Chapter2", 2, 0),
        ("Sub10", 4, 1),
        ("Chapter10", 5, 0),
    ]


def test_archive_natural_sort_is_deterministic_for_case_and_leading_zero(tmp_path: Path) -> None:
    archive_path = tmp_path / "natural.cbz"
    _write_zip(
        archive_path,
        {
            "chapter/1.png": _png_bytes((1, 10)),
            "chapter/01.png": _png_bytes((10, 10)),
            "chapter/001.png": _png_bytes((100, 10)),
            "ChapterA/1.png": _png_bytes((20, 10)),
            "chaptera/1.png": _png_bytes((30, 10)),
        },
    )

    session = ArchiveImageService().open(archive_path)

    assert [session.get_dimensions(index) for index in session.index_range] == [
        (100, 10),
        (10, 10),
        (1, 10),
        (20, 10),
        (30, 10),
    ]


def test_7z_archive_reads_images(tmp_path: Path) -> None:
    archive_path = tmp_path / "sample.cb7"
    with py7zr.SevenZipFile(archive_path, "w") as archive:
        archive.writestr(_png_bytes((60, 20)), "002.png")
        archive.writestr(_png_bytes((40, 20)), "001.png")
        archive.writestr(b"ignored", "notes.txt")

    session = ArchiveImageService().open(archive_path)

    assert session.page_count == 2
    assert [session.get_dimensions(index) for index in session.index_range] == [(40, 20), (60, 20)]


def test_7z_writer_enforces_operation_budget_before_unbounded_buffering(tmp_path: Path) -> None:
    archive_path = tmp_path / "budget.cb7"
    with py7zr.SevenZipFile(archive_path, "w") as archive:
        archive.writestr(_png_bytes((40, 20)), "001.png")
    limits = ArchiveOpenLimits(
        max_source_bytes=None,
        max_extracted_item_bytes=None,
        max_operation_bytes=1,
        max_image_pixels=None,
        external_command_timeout_seconds=None,
    )
    session = ArchiveImageService().open(archive_path, limits=limits)

    with pytest.raises(ArchiveResourceLimitError) as error:
        session.get_page(0)

    assert error.value.limit == "operation_bytes"


def test_7z_batch_reads_and_reuses_disk_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive_path = tmp_path / "sample.cb7"
    with py7zr.SevenZipFile(archive_path, "w") as archive:
        archive.writestr(_png_bytes((40, 20)), "001.png")
        archive.writestr(_png_bytes((60, 20)), "002.png")

    service = ArchiveImageService(page_cache_dir=tmp_path / "archive_pages")
    cache_key = "file:managed-sample"
    session = service.open(
        archive_path,
        document_cache_key=cache_key,
        allow_persistent_cache=True,
    )
    original = service._seven_zip_backend.read_entries
    calls: list[tuple[str, ...]] = []

    def counted_read(source, entries, **kwargs):  # noqa: ANN001
        calls.append(tuple(name for name, _password in entries))
        return original(source, entries, **kwargs)

    monkeypatch.setattr(service._seven_zip_backend, "read_entries", counted_read)

    pages = session.get_pages((0, 1))
    assert [page.dimensions if page is not None else None for page in pages] == [(40, 20), (60, 20)]
    assert calls == [("001.png", "002.png")]
    assert service._page_cache.get(cache_key, "001.png") is None
    assert service._page_cache.get(cache_key, session._cache_page_key(0)) is not None

    second_session = service.open(
        archive_path,
        document_cache_key=cache_key,
        allow_persistent_cache=True,
    )
    assert second_session.get_dimensions(0) == (40, 20)
    assert second_session.get_page(0) is not None
    assert calls == [("001.png", "002.png")]


def test_extraction_cache_pages_are_scoped_to_the_limit_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive_path = tmp_path / "policy.cb7"
    with py7zr.SevenZipFile(archive_path, "w") as archive:
        archive.writestr(_png_bytes((40, 20)), "001.png")
    service = ArchiveImageService(page_cache_dir=tmp_path / "archive-pages")
    default_session = service.open(archive_path)
    assert default_session.get_page(0) is not None

    original = service._seven_zip_backend.read_entries
    calls: list[tuple[str, ...]] = []

    def counted_read(source, entries, **kwargs):  # noqa: ANN001
        calls.append(tuple(name for name, _password in entries))
        return original(source, entries, **kwargs)

    monkeypatch.setattr(service._seven_zip_backend, "read_entries", counted_read)
    changed_policy = service.open(archive_path, limits=ArchiveOpenLimits(max_operation_bytes=None))

    assert default_session._cache_page_key(0) != changed_policy._cache_page_key(0)
    assert changed_policy.get_page(0) is not None
    assert calls == [("001.png",)]


def test_external_file_extraction_output_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_dir = tmp_path / "unar-output"
    output_dir.mkdir()
    (output_dir / "extra.bin").write_bytes(b"too much")

    monkeypatch.setattr(
        archive_common.subprocess,
        "Popen",
        lambda _command, stdout=None, stderr=None: _FakePopen(
            stdout_target=stdout,
            stderr_target=stderr,
        ),
    )

    with pytest.raises(ArchiveResourceLimitError) as error:
        archive_common.run_archive_file_command(
            ["unar"],
            "page.png",
            password=None,
            timeout_seconds=None,
            output_directory=output_dir,
            max_output_bytes=1,
            budget=ArchiveOperationBudget(1024),
        )

    assert error.value.limit == "extracted_item_bytes"


def test_7z_buffer_peak_tracking_is_opt_in() -> None:
    limits = ArchiveOpenLimits()
    untracked = _BudgetedBytesFactory(limits, ArchiveOperationBudget(None))
    untracked.create("untracked.png").write(b"page")

    tracked = _BudgetedBytesFactory(
        limits,
        ArchiveOperationBudget(None),
        track_buffered_bytes=True,
    )
    tracked.create("tracked.png").write(b"page")

    assert untracked.peak_buffered_bytes == 0
    assert tracked.peak_buffered_bytes == 4


def test_7z_thumbnail_access_switches_from_cold_batch_to_ready_single_page(tmp_path: Path) -> None:
    archive_path = tmp_path / "sample.cb7"
    with py7zr.SevenZipFile(archive_path, "w") as archive:
        archive.writestr(_png_bytes((40, 20)), "001.png")
        archive.writestr(_png_bytes((60, 20)), "002.png")
    pool = ArchiveExtractionPool(tmp_path / "archive_pages", max_bytes=1 << 20)
    session = ArchiveImageService(extraction_pool=pool).open(
        archive_path,
        document_cache_key="file:managed-sample",
        allow_persistent_cache=True,
    )

    assert session.access_mode == ArchiveAccessMode.EXPENSIVE_COLD
    assert session.requires_sequential_warmup is True
    assert session.thumbnail_batch_size(0) == 8

    assert all(page is not None for page in session.get_pages((0, 1)))
    assert session.mark_thumbnail_cache_ready()
    assert session.access_mode == ArchiveAccessMode.EXPENSIVE_READY
    assert session.requires_sequential_warmup is False
    assert session.thumbnail_batch_size(0) == 1

    pool.clear()

    assert session.access_mode == ArchiveAccessMode.EXPENSIVE_COLD


def test_non_solid_7z_uses_single_page_random_access_without_warmup(tmp_path: Path) -> None:
    archive_path = tmp_path / "single-page.cb7"
    # A one-member 7z has one independent block and py7zr reports it as
    # non-solid, so JoyRead can serve it on demand without a whole-book pass.
    with py7zr.SevenZipFile(archive_path, "w") as archive:
        archive.writestr(_png_bytes((40, 20)), "001.png")
    pool = ArchiveExtractionPool(tmp_path / "archive_pages", max_bytes=1 << 20)
    session = ArchiveImageService(extraction_pool=pool).open(
        archive_path,
        document_cache_key="file:non-solid",
        allow_persistent_cache=True,
    )

    assert session.access_mode == ArchiveAccessMode.EXPENSIVE_COLD
    assert session.requires_sequential_warmup is False
    assert session.thumbnail_batch_size(0) == 1


def test_encrypted_zip_uses_password_provider(tmp_path: Path) -> None:
    archive_path = tmp_path / "encrypted.cbz"
    _write_encrypted_cbz(archive_path)

    resolver = ExtractionBackendResolver(tmp_path / "empty-extractors")
    session = ArchiveImageService(backend_resolver=resolver).open(
        archive_path,
        password_provider=lambda _request: "secret",
    )

    assert session.page_count == 1
    assert session.get_dimensions(0) == (32, 16)


def test_encrypted_zip_accepts_unicode_password(tmp_path: Path) -> None:
    archive_path = tmp_path / "encrypted-unicode.cbz"
    with pyzipper.AESZipFile(
        archive_path,
        "w",
        compression=ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as archive:
        archive.setpassword("秘密".encode("utf-8"))
        archive.writestr("001.png", _png_bytes((32, 16)))

    session = ArchiveImageService().open(archive_path, password_provider=lambda _request: "秘密")

    assert session.page_count == 1
    assert session.get_dimensions(0) == (32, 16)


def test_encrypted_zip_without_password_is_controlled(tmp_path: Path) -> None:
    archive_path = tmp_path / "encrypted.cbz"
    _write_encrypted_cbz(archive_path)

    with pytest.raises(ArchivePasswordRequired):
        ArchiveImageService().open(archive_path)

    with pytest.raises(ArchivePasswordRejected):
        ArchiveImageService().open(archive_path, password_provider=lambda _request: "wrong")


def test_probe_reports_encrypted_archive_without_prompting(tmp_path: Path) -> None:
    """Encryption is an answer the probe returns, never something it asks about.

    The probe has no password parameters at all now, so "did not prompt" is a
    property of the signature rather than of the implementation.
    """

    archive_path = tmp_path / "encrypted.cbz"
    _write_encrypted_cbz(archive_path)

    result = ArchiveImageService().probe_archive(archive_path)

    assert result.is_valid is False
    assert result.code == ArchiveValidationCode.PASSWORD_REQUIRED
    assert result.error_type == "ArchivePasswordRequired"
    assert "Password-protected archive" in result.message


def test_probe_does_not_recurse_into_nested_encrypted_archives(tmp_path: Path) -> None:
    archive_path = tmp_path / "outer.cbz"
    _write_zip(
        archive_path,
        {
            "001.png": _png_bytes((20, 10)),
            "nested.cbz": _encrypted_cbz_bytes("secret", (32, 16)),
        },
    )

    # The shallow probe cannot see the nested encryption -- that is exactly
    # why importing needs ``inspect_for_import`` instead.
    probe = ArchiveImageService().probe_archive(archive_path)
    requests = []
    session = ArchiveImageService().open(
        archive_path,
        password_provider=lambda request: requests.append(request.archive_path) or "secret",
    )

    assert probe.code == ArchiveValidationCode.OK
    assert probe.has_direct_images is True
    assert probe.has_nested_archive_candidates is True
    assert requests == ["outer.cbz::nested.cbz"]
    assert [session.get_dimensions(index) for index in session.index_range] == [(20, 10), (32, 16)]


def test_nested_encrypted_archive_can_be_skipped_while_outer_images_remain(tmp_path: Path) -> None:
    archive_path = tmp_path / "outer.cbz"
    _write_zip(
        archive_path,
        {
            "001.png": _png_bytes((20, 10)),
            "nested.cbz": _encrypted_cbz_bytes("secret", (32, 16)),
        },
    )
    requests = []

    session = ArchiveImageService().open(
        archive_path,
        password_provider=lambda request: requests.append(request.archive_path) or ArchivePasswordResponse(skip=True),
    )

    assert requests == ["outer.cbz::nested.cbz"]
    assert session.page_count == 1
    assert session.get_dimensions(0) == (20, 10)


def test_skipping_only_top_level_encrypted_archive_reports_no_readable_images(tmp_path: Path) -> None:
    archive_path = tmp_path / "encrypted.cbz"
    _write_encrypted_cbz(archive_path)

    with pytest.raises(ArchiveEmptyError, match="No readable images. Encrypted archives were skipped."):
        ArchiveImageService().open(
            archive_path,
            password_provider=lambda _request: ArchivePasswordResponse(skip=True),
        )


def test_multiple_nested_encrypted_archives_prompt_in_order_and_skip_independently(tmp_path: Path) -> None:
    archive_path = tmp_path / "outer.cbz"
    _write_zip(
        archive_path,
        {
            "001.png": _png_bytes((20, 10)),
            "nested-a.cbz": _encrypted_cbz_bytes("secret-a", (32, 16)),
            "nested-b.cbz": _encrypted_cbz_bytes("secret-b", (48, 16)),
        },
    )
    requests = []

    session = ArchiveImageService().open(
        archive_path,
        password_provider=lambda request: (
            requests.append(request.archive_path)
            or (
                ArchivePasswordResponse(skip=True)
                if request.archive_path.endswith("nested-a.cbz")
                else "secret-b"
            )
        ),
    )

    assert requests == ["outer.cbz::nested-a.cbz", "outer.cbz::nested-b.cbz"]
    assert [session.get_dimensions(index) for index in session.index_range] == [(20, 10), (48, 16)]
    assert [(item.label, item.page_index, item.depth) for item in session.contents] == [
        ("nested-b", 1, 0),
    ]


def test_wrong_nested_archive_password_reports_nested_archive_path(tmp_path: Path) -> None:
    archive_path = tmp_path / "outer.cbz"
    _write_zip(archive_path, {"nested.cbz": _encrypted_cbz_bytes("secret", (32, 16))})

    with pytest.raises(ArchivePasswordRejected) as exc_info:
        ArchiveImageService().open(archive_path, password_provider=lambda _request: "wrong")

    assert exc_info.value.archive_path == "outer.cbz::nested.cbz"


def test_wrong_nested_rar_password_reprompts_before_page_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from joyread.core.archive import service as archive_service

    archive_path = tmp_path / "outer.cbz"
    _write_zip(archive_path, {"nested.cbr": b"fake-rar"})

    class FakeInfo:
        filename = "001.jpg"
        file_size = 128

        def isdir(self) -> bool:
            return False

    class FakeRarFile:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):  # noqa: ANN001
            return self

        def __exit__(self, *_args) -> None:  # noqa: ANN001
            return None

        def needs_password(self) -> bool:
            return True

        def infolist(self):
            return [FakeInfo()]

        def read(self, *_args, **_kwargs) -> bytes:
            raise FakeRarModule.BadRarFile("wrong password")

    class FakeRarModule:
        class RarCannotExec(Exception):
            pass

        class NeedFirstVolume(Exception):
            pass

        class BadRarFile(Exception):
            pass

        class PasswordRequired(Exception):
            pass

        class RarWrongPassword(Exception):
            pass

        RarFile = FakeRarFile

        def tool_setup(self) -> None:
            return None

    monkeypatch.setattr(archive_service, "rarfile", FakeRarModule())

    with pytest.raises(ArchivePasswordRejected) as exc_info:
        ArchiveImageService().open(archive_path, password_provider=lambda _request: "wrong")

    assert exc_info.value.archive_path == "outer.cbz::nested.cbr"


def test_empty_corrupt_and_unsupported_archives_are_controlled(tmp_path: Path) -> None:
    empty_path = tmp_path / "empty.cbz"
    _write_zip(empty_path, {"notes.txt": b"no images"})
    corrupt_path = tmp_path / "corrupt.cbz"
    corrupt_path.write_bytes(b"not a zip")
    unsupported_path = tmp_path / "sample.tar"
    unsupported_path.write_bytes(b"not supported")

    with pytest.raises(ArchiveEmptyError):
        ArchiveImageService().open(empty_path)
    with pytest.raises(ArchiveCorruptError):
        ArchiveImageService().open(corrupt_path)
    with pytest.raises(ArchiveUnsupportedFormat):
        ArchiveImageService().open(unsupported_path)


def test_archive_probe_returns_structured_container_feedback(tmp_path: Path) -> None:
    service = ArchiveImageService()
    archive_path = tmp_path / "valid.cbz"
    _write_zip(archive_path, {"001.png": _png_bytes((20, 10)), "notes.txt": b"ignored"})
    missing_path = tmp_path / "missing.cbz"
    directory_path = tmp_path / "folder.cbz"
    directory_path.mkdir()
    unsupported_path = tmp_path / "sample.tar"
    unsupported_path.write_bytes(b"not supported")
    empty_path = tmp_path / "empty.cbz"
    _write_zip(empty_path, {"notes.txt": b"no images"})
    corrupt_path = tmp_path / "corrupt.cbz"
    corrupt_path.write_bytes(b"not a zip")

    valid = service.probe_archive(archive_path)
    assert valid.is_valid is True
    assert valid.code == ArchiveValidationCode.OK
    assert valid.archive_format == "CBZ"
    assert valid.has_direct_images is True
    assert valid.has_nested_archive_candidates is False
    assert not hasattr(valid, "page_count")
    assert not hasattr(valid, "file_size")
    assert not hasattr(valid, "mtime_ns")

    missing = service.probe_archive(missing_path)
    assert missing.is_valid is False
    assert missing.code == ArchiveValidationCode.MISSING
    assert "does not exist" in missing.message

    not_file = service.probe_archive(directory_path)
    assert not_file.code == ArchiveValidationCode.NOT_FILE
    assert not_file.error_type == "ArchiveOpenError"

    unsupported = service.probe_archive(unsupported_path)
    assert unsupported.code == ArchiveValidationCode.UNSUPPORTED_FORMAT
    assert unsupported.error_type == "ArchiveUnsupportedFormat"

    empty = service.probe_archive(empty_path)
    assert empty.code == ArchiveValidationCode.EMPTY
    assert empty.error_type == "ArchiveEmptyError"

    corrupt = service.probe_archive(corrupt_path)
    assert corrupt.code == ArchiveValidationCode.CORRUPT
    assert corrupt.error_type == "ArchiveCorruptError"


def test_probe_leaves_undecodable_image_errors_for_page_reads(tmp_path: Path) -> None:
    archive_path = tmp_path / "bad-image.cbz"
    _write_zip(archive_path, {"001.png": b"not an image"})

    service = ArchiveImageService()
    result = service.probe_archive(archive_path)

    assert result.is_valid is True
    assert result.code == ArchiveValidationCode.OK
    with pytest.raises(ArchiveReadError):
        service.open(archive_path).get_page(0)


def test_probe_reports_encryption_and_cannot_be_given_a_password(tmp_path: Path) -> None:
    """The probe answers "is this encrypted", it never tries to get past it.

    Previously it accepted a ``password_provider`` and documented that it
    ignored it. A parameter that cannot affect the result is a trap, so the
    guarantee is now in the signature: there is nothing to pass.
    """

    archive_path = tmp_path / "encrypted.cbz"
    with pyzipper.AESZipFile(
        archive_path,
        "w",
        compression=ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as archive:
        archive.setpassword(b"secret")
        archive.writestr("001.png", _png_bytes((32, 16)))

    service = ArchiveImageService()

    required = service.probe_archive(archive_path)
    assert required.is_valid is False
    assert required.code == ArchiveValidationCode.PASSWORD_REQUIRED
    assert required.error_type == "ArchivePasswordRequired"

    with pytest.raises(TypeError):
        service.probe_archive(archive_path, password_provider=lambda _request: "secret")

    # open() is the access path, and it still takes one.
    assert service.open(archive_path, password_provider=lambda _request: "secret").page_count == 1


def test_external_archive_sessions_do_not_share_persistent_extraction_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "external.cb7"
    with py7zr.SevenZipFile(archive_path, "w") as archive:
        archive.writestr(_png_bytes((40, 20)), "001.png")

    service = ArchiveImageService(page_cache_dir=tmp_path / "archive-pages")
    original = service._seven_zip_backend.read_entries
    calls: list[tuple[str, ...]] = []

    def counted_read(source, entries, **kwargs):  # noqa: ANN001
        calls.append(tuple(name for name, _password in entries))
        return original(source, entries, **kwargs)

    monkeypatch.setattr(service._seven_zip_backend, "read_entries", counted_read)

    assert service.open(archive_path).get_page(0) is not None
    assert service.open(archive_path).get_page(0) is not None

    assert calls == [("001.png",), ("001.png",)]


def test_rar_missing_backend_is_controlled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from joyread.core.archive import service as archive_service

    class MissingRarBackend:
        class RarCannotExec(Exception):
            pass

        class NeedFirstVolume(Exception):
            pass

        class BadRarFile(Exception):
            pass

        class RarFile:
            def __init__(self, *_args, **_kwargs) -> None:
                raise MissingRarBackend.RarCannotExec("missing backend")

        def tool_setup(self) -> None:
            raise self.RarCannotExec("missing backend")

    archive_path = tmp_path / "sample.cbr"
    archive_path.write_bytes(b"not read because backend check happens first")
    monkeypatch.setattr(archive_service, "rarfile", MissingRarBackend())

    with pytest.raises(ArchiveDependencyMissing):
        ArchiveImageService().open(archive_path)

    result = ArchiveImageService().probe_archive(archive_path)
    assert result.code == ArchiveValidationCode.DEPENDENCY_MISSING
    assert result.error_type == "ArchiveDependencyMissing"
    assert "Tried:" in result.message


def test_extraction_backend_resolver_prefers_bundled_7zip(tmp_path: Path) -> None:
    resolver = ExtractionBackendResolver(tmp_path)
    bundled, _description = resolver._seven_zip_candidates()[0]
    bundled.parent.mkdir(parents=True)
    bundled.write_text("#!/bin/sh\n", encoding="utf-8")
    bundled.chmod(0o755)

    backend = resolver.seven_zip()

    assert backend is not None
    assert backend.executable == str(bundled)
    assert backend.source.startswith("bundled:")


def test_extraction_backend_resolver_uses_env_before_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = tmp_path / "custom-7zz"
    override.write_text("#!/bin/sh\n", encoding="utf-8")
    override.chmod(0o755)
    monkeypatch.setenv(SEVEN_ZIP_ENV_VAR, str(override))

    resolver = ExtractionBackendResolver(tmp_path)
    backend = resolver.seven_zip()

    assert backend is not None
    assert backend.executable == str(override)


def test_rar_read_falls_back_to_external_bsdtar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from joyread.core.archive import service as archive_service

    archive_path = tmp_path / "sample.cbr"
    archive_path.write_bytes(b"fake-rar")
    page_bytes = _png_bytes((32, 16))

    class FakeInfo:
        filename = "001.jpg"
        file_size = len(page_bytes)

        def isdir(self) -> bool:
            return False

    class FakeRarFile:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):  # noqa: ANN001
            return self

        def __exit__(self, *_args) -> None:  # noqa: ANN001
            return None

        def needs_password(self) -> bool:
            return False

        def infolist(self):
            return [FakeInfo()]

        def read(self, *_args, **_kwargs) -> bytes:
            raise FakeRarModule.BadRarFile("rarfile backend failed")

    class FakeRarModule:
        class RarCannotExec(Exception):
            pass

        class NeedFirstVolume(Exception):
            pass

        class BadRarFile(Exception):
            pass

        class PasswordRequired(Exception):
            pass

        class RarWrongPassword(Exception):
            pass

        RarFile = FakeRarFile

        def tool_setup(self) -> None:
            return None

    def fake_which(name: str) -> str | None:
        return "/usr/bin/bsdtar" if name == "bsdtar" else None

    def fake_popen(command, stdout=None, stderr=None):  # noqa: ANN001
        assert command[:2] == ["/usr/bin/bsdtar", "-xOf"]
        return _FakePopen(
            stdout_target=stdout,
            stderr_target=stderr,
            stdout_data=page_bytes,
        )

    monkeypatch.setattr(archive_service, "rarfile", FakeRarModule())
    from joyread.core.archive import backends

    monkeypatch.setattr(backends.shutil, "which", fake_which)
    monkeypatch.setattr(archive_common.subprocess, "Popen", fake_popen)

    resolver = ExtractionBackendResolver(tmp_path / "empty-extractors")
    session = ArchiveImageService(backend_resolver=resolver).open(archive_path)
    page = session.get_page(0)

    assert page is not None
    assert page.dimensions == (32, 16)


def test_encrypted_rar_read_prefers_7zip_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from joyread.core.archive import backends
    from joyread.core.archive import service as archive_service

    archive_path = tmp_path / "encrypted.cbr"
    archive_path.write_bytes(b"fake-rar")
    page_bytes = _png_bytes((40, 20))

    class FakeInfo:
        filename = "001.jpg"
        file_size = len(page_bytes)

        def isdir(self) -> bool:
            return False

    class FakeRarFile:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):  # noqa: ANN001
            return self

        def __exit__(self, *_args) -> None:  # noqa: ANN001
            return None

        def needs_password(self) -> bool:
            return True

        def infolist(self):
            return [FakeInfo()]

        def read(self, *_args, **_kwargs) -> bytes:
            raise FakeRarModule.BadRarFile("rarfile backend failed")

    class FakeRarModule:
        class RarCannotExec(Exception):
            pass

        class NeedFirstVolume(Exception):
            pass

        class BadRarFile(Exception):
            pass

        class PasswordRequired(Exception):
            pass

        class RarWrongPassword(Exception):
            pass

        RarFile = FakeRarFile
        SEVENZIP_TOOL = "7z"
        SEVENZIP2_TOOL = "7z"
        UNAR_TOOL = "unar"
        BSDTAR_TOOL = "bsdtar"

        def tool_setup(self) -> None:
            return None

    def fake_which(name: str) -> str | None:
        return { "7zz": "/opt/joyread/7zz", "bsdtar": "/usr/bin/bsdtar" }.get(name)

    def fake_popen(command, stdout=None, stderr=None):  # noqa: ANN001
        assert command[:4] == ["/opt/joyread/7zz", "x", "-so", "-y"]
        assert "-psecret" in command
        assert "/usr/bin/bsdtar" not in command
        return _FakePopen(
            stdout_target=stdout,
            stderr_target=stderr,
            stdout_data=page_bytes,
        )

    monkeypatch.setattr(archive_service, "rarfile", FakeRarModule())
    monkeypatch.setattr(backends.shutil, "which", fake_which)
    monkeypatch.setattr(archive_common.subprocess, "Popen", fake_popen)

    resolver = ExtractionBackendResolver(tmp_path / "empty-extractors")
    session = ArchiveImageService(backend_resolver=resolver).open(
        archive_path,
        password_provider=lambda _request: "secret",
    )
    page = session.get_page(0)

    assert page is not None
    assert page.dimensions == (40, 20)


def test_encrypted_rar_page_read_rejects_wrong_7zip_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from joyread.core.archive import backends
    from joyread.core.archive import service as archive_service

    archive_path = tmp_path / "encrypted.cbr"
    archive_path.write_bytes(b"fake-rar")

    class FakeInfo:
        filename = "001.jpg"
        file_size = 128

        def isdir(self) -> bool:
            return False

    class FakeRarFile:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):  # noqa: ANN001
            return self

        def __exit__(self, *_args) -> None:  # noqa: ANN001
            return None

        def needs_password(self) -> bool:
            return True

        def infolist(self):
            return [FakeInfo()]

        def read(self, *_args, **_kwargs) -> bytes:
            raise FakeRarModule.BadRarFile("rarfile backend failed")

    class FakeRarModule:
        class RarCannotExec(Exception):
            pass

        class NeedFirstVolume(Exception):
            pass

        class BadRarFile(Exception):
            pass

        class PasswordRequired(Exception):
            pass

        class RarWrongPassword(Exception):
            pass

        RarFile = FakeRarFile

        def tool_setup(self) -> None:
            return None

    def fake_which(name: str) -> str | None:
        return "/opt/joyread/7zz" if name == "7zz" else None

    def fake_popen(command, stdout=None, stderr=None):  # noqa: ANN001
        assert command[:4] == ["/opt/joyread/7zz", "x", "-so", "-y"]
        assert "-pwrong" in command
        return _FakePopen(
            stdout_target=stdout,
            stderr_target=stderr,
            stderr_data=b"ERROR: Data Error in encrypted file. Wrong password?",
            returncode=2,
        )

    monkeypatch.setattr(archive_service, "rarfile", FakeRarModule())
    monkeypatch.setattr(backends.shutil, "which", fake_which)
    monkeypatch.setattr(archive_common.subprocess, "Popen", fake_popen)

    resolver = ExtractionBackendResolver(tmp_path / "empty-extractors")
    with pytest.raises(ArchivePasswordRejected):
        ArchiveImageService(backend_resolver=resolver).open(
            archive_path,
            password_provider=lambda _request: "wrong",
        )


@pytest.mark.parametrize(
    ("suffix", "password", "expected_concurrency"),
    ((".zip", None, 2), (".zip", "secret", 1), (".rar", None, 1)),
)
def test_archive_session_enforces_backend_read_concurrency(
    suffix: str,
    password: str | None,
    expected_concurrency: int,
) -> None:
    source = ArchiveSource("sample", suffix, data=b"container")
    pages = (
        PageRecord("sample/001.png", source, "001.png", password),
        PageRecord("sample/002.png", source, "002.png", password),
    )
    active = 0
    maximum_active = 0
    counter_lock = Lock()

    def read_entries(_source, entries, _budget):  # noqa: ANN001
        nonlocal active, maximum_active
        with counter_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.04)
        with counter_lock:
            active -= 1
        return {name: _png_bytes((20, 30)) for name, _entry_password in entries}

    session = ArchiveImageSession(pages, read_entries)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(session.get_page, (0, 1)))

    assert all(page is not None for page in results)
    assert maximum_active == expected_concurrency
