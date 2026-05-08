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


def test_7z_batch_reads_and_reuses_disk_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive_path = tmp_path / "sample.cb7"
    with py7zr.SevenZipFile(archive_path, "w") as archive:
        archive.writestr(_png_bytes((40, 20)), "001.png")
        archive.writestr(_png_bytes((60, 20)), "002.png")

    service = ArchiveImageService(page_cache_dir=tmp_path / "archive_pages")
    session = service.open(archive_path)
    original = service._read_7z_entries
    calls: list[tuple[str, ...]] = []

    def counted_read(source, entries):  # noqa: ANN001
        calls.append(tuple(name for name, _password in entries))
        return original(source, entries)

    monkeypatch.setattr(service, "_read_7z_entries", counted_read)

    pages = session.get_pages((0, 1))
    assert [page.dimensions if page is not None else None for page in pages] == [(40, 20), (60, 20)]
    assert calls == [("001.png", "002.png")]

    second_session = service.open(archive_path)
    assert second_session.get_page(0) is not None
    assert calls == [("001.png", "002.png")]


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


def test_archive_validation_reports_listed_but_undecodable_first_page(tmp_path: Path) -> None:
    archive_path = tmp_path / "bad-image.cbz"
    _write_zip(archive_path, {"001.png": b"not an image"})

    result = ArchiveImageService().validate_archive(archive_path)

    assert result.is_valid is False
    assert result.code == ArchiveValidationCode.READ_FAILED
    assert result.error_type == "ArchiveReadError"


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

    def fake_run(command, stdout, stderr, check=False):  # noqa: ANN001
        assert command[:2] == ["/usr/bin/bsdtar", "-xOf"]
        return archive_service.subprocess.CompletedProcess(command, 0, stdout=page_bytes, stderr=b"")

    monkeypatch.setattr(archive_service, "rarfile", FakeRarModule())
    monkeypatch.setattr(archive_service.shutil, "which", fake_which)
    monkeypatch.setattr(archive_service.subprocess, "run", fake_run)

    session = ArchiveImageService().open(archive_path)
    page = session.get_page(0)

    assert page is not None
    assert page.dimensions == (32, 16)
