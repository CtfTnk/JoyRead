from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import py7zr
import pyzipper
import pytest
from PIL import Image

from joyread.core.archive import (
    ArchiveCorruptError,
    ArchiveDependencyMissing,
    ArchiveEmptyError,
    ArchiveImageService,
    ArchivePasswordRejected,
    ArchivePasswordRequired,
    ArchiveUnsupportedFormat,
    ArchiveValidationCode,
)


def _png_bytes(size: tuple[int, int], color: str = "#ffffff") -> bytes:
    image = Image.new("RGB", size, color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _write_zip(path: Path, entries: dict[str, bytes]) -> None:
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


def test_cbz_discovers_images_sorts_groups_and_keeps_deep_paths(tmp_path: Path) -> None:
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

    session = ArchiveImageService().open(archive_path)

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

    session = ArchiveImageService().open(archive_path)

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


def test_nested_cbz_pages_are_appended_in_discovery_order(tmp_path: Path) -> None:
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


def test_7z_archive_reads_images(tmp_path: Path) -> None:
    archive_path = tmp_path / "sample.cb7"
    with py7zr.SevenZipFile(archive_path, "w") as archive:
        archive.writestr(_png_bytes((60, 20)), "002.png")
        archive.writestr(_png_bytes((40, 20)), "001.png")
        archive.writestr(b"ignored", "notes.txt")

    session = ArchiveImageService().open(archive_path)

    assert session.page_count == 2
    assert [session.get_dimensions(index) for index in session.index_range] == [(40, 20), (60, 20)]


def test_encrypted_zip_uses_password_provider(tmp_path: Path) -> None:
    archive_path = tmp_path / "encrypted.cbz"
    with pyzipper.AESZipFile(
        archive_path,
        "w",
        compression=ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as archive:
        archive.setpassword(b"secret")
        archive.writestr("001.png", _png_bytes((32, 16)))

    session = ArchiveImageService().open(archive_path, password_provider=lambda _request: "secret")

    assert session.page_count == 1
    assert session.get_dimensions(0) == (32, 16)


def test_encrypted_zip_without_password_is_controlled(tmp_path: Path) -> None:
    archive_path = tmp_path / "encrypted.cbz"
    with pyzipper.AESZipFile(
        archive_path,
        "w",
        compression=ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as archive:
        archive.setpassword(b"secret")
        archive.writestr("001.png", _png_bytes((32, 16)))

    with pytest.raises(ArchivePasswordRequired):
        ArchiveImageService().open(archive_path)

    with pytest.raises(ArchivePasswordRejected):
        ArchiveImageService().open(archive_path, password_provider=lambda _request: "wrong")


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


def test_archive_validation_returns_structured_success_and_failure_feedback(tmp_path: Path) -> None:
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

    valid = service.validate_archive(archive_path)
    assert valid.is_valid is True
    assert valid.code == ArchiveValidationCode.OK
    assert valid.page_count == 1
    assert valid.archive_format == "CBZ"
    assert valid.file_size == archive_path.stat().st_size
    assert valid.mtime_ns == archive_path.stat().st_mtime_ns

    missing = service.validate_archive(missing_path)
    assert missing.is_valid is False
    assert missing.code == ArchiveValidationCode.MISSING
    assert "does not exist" in missing.message

    not_file = service.validate_archive(directory_path)
    assert not_file.code == ArchiveValidationCode.NOT_FILE
    assert not_file.error_type == "ArchiveOpenError"

    unsupported = service.validate_archive(unsupported_path)
    assert unsupported.code == ArchiveValidationCode.UNSUPPORTED_FORMAT
    assert unsupported.error_type == "ArchiveUnsupportedFormat"

    empty = service.validate_archive(empty_path)
    assert empty.code == ArchiveValidationCode.EMPTY
    assert empty.error_type == "ArchiveEmptyError"

    corrupt = service.validate_archive(corrupt_path)
    assert corrupt.code == ArchiveValidationCode.CORRUPT
    assert corrupt.error_type == "ArchiveCorruptError"


def test_archive_validation_reports_password_feedback(tmp_path: Path) -> None:
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

    required = service.validate_archive(archive_path)
    assert required.is_valid is False
    assert required.code == ArchiveValidationCode.PASSWORD_REQUIRED
    assert required.error_type == "ArchivePasswordRequired"

    rejected = service.validate_archive(archive_path, password_provider=lambda _request: "wrong")
    assert rejected.code == ArchiveValidationCode.PASSWORD_REJECTED
    assert rejected.error_type == "ArchivePasswordRejected"

    accepted = service.validate_archive(archive_path, password_provider=lambda _request: "secret")
    assert accepted.is_valid is True
    assert accepted.code == ArchiveValidationCode.OK
    assert accepted.page_count == 1


def test_rar_missing_backend_is_controlled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from joyread.core.archive import service as archive_service

    class MissingRarBackend:
        class RarCannotExec(Exception):
            pass

        def tool_setup(self) -> None:
            raise self.RarCannotExec("missing backend")

    archive_path = tmp_path / "sample.cbr"
    archive_path.write_bytes(b"not read because backend check happens first")
    monkeypatch.setattr(archive_service, "rarfile", MissingRarBackend())

    with pytest.raises(ArchiveDependencyMissing):
        ArchiveImageService().open(archive_path)

    result = ArchiveImageService().validate_archive(archive_path)
    assert result.code == ArchiveValidationCode.DEPENDENCY_MISSING
    assert result.error_type == "ArchiveDependencyMissing"
