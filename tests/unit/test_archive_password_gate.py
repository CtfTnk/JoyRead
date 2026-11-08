"""Archive passwords are gated off: the Reader skips encrypted archives unasked.

``ARCHIVE_PASSWORD_ACCESS_ENABLED`` closes the Reader's password flow. The
archive core expresses it as ``ArchivePasswordPolicy.SKIP``: no password is
requested, an encrypted archive nested in an ordinary one is left out, and a
book whose pages are all encrypted fails with ``ArchiveEncryptionUnsupported``.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pyzipper
import pytest
from PIL import Image

from joyread.core.archive import (
    ArchiveEmptyError,
    ArchiveEncryptionUnsupported,
    ArchiveImageService,
    ArchivePasswordPolicy,
    ArchivePasswordRequired,
)
from joyread.core.file_types import ARCHIVE_PASSWORD_ACCESS_ENABLED
from joyread.core.reader.session_service import ReaderSessionService
from joyread.core.services.archive_cache_lease import ArchiveCacheLease, ArchiveCacheScope
from joyread.core.services.archive_extraction_pool import ArchiveExtractionPool

PASSWORD = "pw-secret"


def _png(width: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, 30), "#336699").save(buffer, format="PNG")
    return buffer.getvalue()


def _encrypted_zip_bytes(pages: int = 3) -> bytes:
    buffer = BytesIO()
    with pyzipper.AESZipFile(buffer, "w", compression=8, encryption=pyzipper.WZ_AES) as archive:
        archive.setpassword(PASSWORD.encode())
        for index in range(pages):
            archive.writestr(f"secret-{index:03d}.png", _png(40 + index))
    return buffer.getvalue()


def _encrypted_cbz(tmp_path: Path) -> Path:
    path = tmp_path / "secret.cbz"
    path.write_bytes(_encrypted_zip_bytes())
    return path


def _plain_with_encrypted_inside(tmp_path: Path) -> Path:
    path = tmp_path / "mixed.cbz"
    with ZipFile(path, "w") as archive:
        archive.writestr("001.png", _png(50))
        archive.writestr("002.png", _png(51))
        archive.writestr("extra/locked.zip", _encrypted_zip_bytes())
    return path


def _refusing_provider(_request):  # noqa: ANN001, ANN202
    raise AssertionError("SKIP must never ask for a password")


def test_the_gate_ships_closed() -> None:
    assert ARCHIVE_PASSWORD_ACCESS_ENABLED is False
    assert ReaderSessionService(ArchiveImageService()).archive_passwords_enabled is False


def test_skip_refuses_an_encrypted_book_without_asking(tmp_path: Path) -> None:
    with pytest.raises(ArchiveEncryptionUnsupported) as raised:
        ArchiveImageService().open(
            _encrypted_cbz(tmp_path),
            password_provider=_refusing_provider,
            password_policy=ArchivePasswordPolicy.SKIP,
        )

    # Still an empty-archive failure for callers that only know that type.
    assert isinstance(raised.value, ArchiveEmptyError)
    assert "secret-000" not in str(raised.value)


def test_skip_leaves_out_an_encrypted_archive_inside_an_ordinary_one(tmp_path: Path) -> None:
    session = ArchiveImageService().open(
        _plain_with_encrypted_inside(tmp_path),
        password_provider=_refusing_provider,
        password_policy=ArchivePasswordPolicy.SKIP,
    )
    try:
        assert session.page_count == 2
        assert [Image.open(BytesIO(session.get_image(index))).size for index in range(2)] == [(50, 30), (51, 30)]
    finally:
        session.close()


def test_skip_writes_nothing_from_an_encrypted_book_to_the_pool(tmp_path: Path) -> None:
    pool = ArchiveExtractionPool(tmp_path / "pool", 64 * 1024 * 1024)
    lease = ArchiveCacheLease(pool, "external:sha256:aa", ArchiveCacheScope.PERSISTENT)
    try:
        with pytest.raises(ArchiveEncryptionUnsupported):
            ArchiveImageService().open(
                _encrypted_cbz(tmp_path),
                password_policy=ArchivePasswordPolicy.SKIP,
                cache_lease=lease,
            )
    finally:
        lease.close()
    assert pool.current_bytes == 0


def test_forbid_still_fails_the_whole_open(tmp_path: Path) -> None:
    """Import conversion's contract is unchanged: meeting an encrypted source
    there is a broken invariant, not something to leave out quietly."""

    with pytest.raises(ArchivePasswordRequired):
        ArchiveImageService().open(
            _plain_with_encrypted_inside(tmp_path),
            password_policy=ArchivePasswordPolicy.FORBID,
        )


def test_the_closed_reader_ignores_a_password_it_was_given(tmp_path: Path) -> None:
    path = _encrypted_cbz(tmp_path)
    reader = ReaderSessionService(ArchiveImageService())

    with pytest.raises(ArchiveEncryptionUnsupported):
        reader.open_document(path, passwords={str(path): PASSWORD})


def test_the_closed_reader_warms_nothing_for_an_encrypted_book(tmp_path: Path) -> None:
    pool = ArchiveExtractionPool(tmp_path / "pool", 64 * 1024 * 1024)
    lease = ArchiveCacheLease(pool, "external:sha256:bb", ArchiveCacheScope.PERSISTENT)
    reader = ReaderSessionService(ArchiveImageService())
    try:
        with pytest.raises(ArchiveEncryptionUnsupported):
            reader.warm_disk_cache(_encrypted_cbz(tmp_path), password=PASSWORD, cache_lease=lease)
    finally:
        lease.close()
    assert pool.current_bytes == 0


def test_the_reopened_gate_restores_the_password_flow(tmp_path: Path) -> None:
    path = _encrypted_cbz(tmp_path)
    reader = ReaderSessionService(ArchiveImageService(), archive_passwords_enabled=True)

    with pytest.raises(ArchivePasswordRequired):
        reader.open_document(path)
    session = reader.open_document(path, passwords={str(path): PASSWORD})
    try:
        assert session.page_count == 3
    finally:
        session.close()
