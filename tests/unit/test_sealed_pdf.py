"""Sealed PDFs: QtPdf reads them through a decrypting device, never from disk."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter, QPdfWriter

from joyread.app.reader_page_pipeline import ReaderPageRequest
from joyread.core.archive import ArchiveImageService
from joyread.core.reader import ReaderSessionService
from joyread.core.reader.pdf import PdfOpenError, PdfVaultLocked
from joyread.core.vault import (
    ContentType,
    SealedFile,
    VaultIntegrityError,
    VaultKeyring,
    VaultLockedError,
    VaultPrivateKey,
)
from joyread.infrastructure.pdf_document_thread import pdf_thread
from joyread.infrastructure.pdf_image_service import PdfImageService
from tests.support.sealed_books import seal_file


def _write_pdf(path: Path, pages: int = 3) -> bytes:
    image = QImage(600, 800, QImage.Format.Format_RGB32)
    writer = QPdfWriter(str(path))
    painter = QPainter(writer)
    for index in range(pages):
        if index:
            writer.newPage()
        image.fill(0x204060 * (index + 1))
        painter.drawImage(QRectF(0, 0, writer.width(), writer.height()), image)
        painter.drawText(60, 120, f"Sealed page {index + 1}")
    painter.end()
    return path.read_bytes()


@pytest.fixture
def key() -> VaultPrivateKey:
    return VaultPrivateKey.generate()


@pytest.fixture
def keyring(key: VaultPrivateKey) -> VaultKeyring:
    ring = VaultKeyring()
    ring.unlock(key)
    return ring


@pytest.fixture
def plain_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "plain.pdf"
    _write_pdf(path)
    return path


@pytest.fixture
def sealed_pdf(tmp_path: Path, plain_pdf: Path, key: VaultPrivateKey) -> Path:
    # Real chunk size: QtPdf's reads are large, and this is what ships.
    return seal_file(tmp_path / "sealed.jrl", plain_pdf.read_bytes(), key, ContentType.PDF, chunk_log2=16)


def _reader(keyring: VaultKeyring | None) -> ReaderSessionService:
    return ReaderSessionService(ArchiveImageService(), PdfImageService(), vault_keyring=keyring)


def _request(page: int) -> ReaderPageRequest:
    return ReaderPageRequest(page, 300, 400, 1.0, 0)


def test_a_sealed_pdf_renders_exactly_like_the_plain_one(
    qtbot, plain_pdf: Path, sealed_pdf: Path, keyring: VaultKeyring  # noqa: ANN001, ARG001
) -> None:
    reader = _reader(keyring)
    plain = reader.open_document(plain_pdf)
    sealed = reader.open_document(sealed_pdf)
    try:
        assert sealed.page_count == plain.page_count == 3
        for page in range(3):
            assert sealed.get_dimensions(page) == plain.get_dimensions(page)
            assert sealed.prepare_page(_request(page)).frame == plain.prepare_page(_request(page)).frame
    finally:
        sealed.close()
        plain.close()


def test_a_locked_vault_refuses_before_any_pdf_work(qtbot, sealed_pdf: Path) -> None:  # noqa: ANN001, ARG001
    with pytest.raises(VaultLockedError):
        _reader(VaultKeyring()).open_document(sealed_pdf)


def test_locking_stops_rendering_an_open_sealed_pdf(
    qtbot, sealed_pdf: Path, keyring: VaultKeyring  # noqa: ANN001, ARG001
) -> None:
    session = _reader(keyring).open_document(sealed_pdf)
    try:
        session.prepare_page(_request(0))
        keyring.lock()
        # PDFium already holds the whole document, so the session itself has
        # to refuse; a revoked file alone would not stop this render.
        with pytest.raises(PdfVaultLocked) as raised:
            session.prepare_page(_request(1))
        assert isinstance(raised.value, VaultLockedError)
    finally:
        session.close()


def test_a_damaged_sealed_pdf_says_damaged_not_password_protected(
    qtbot, tmp_path: Path, plain_pdf: Path, key: VaultPrivateKey, keyring: VaultKeyring  # noqa: ANN001, ARG001
) -> None:
    # Small chunks, so the damage sits in the PDF's own bytes and not in the
    # chunk holding the trailer: the device, not the open, has to notice it.
    damaged = seal_file(tmp_path / "damaged.jrl", plain_pdf.read_bytes(), key, ContentType.PDF)
    raw = bytearray(damaged.read_bytes())
    raw[200] ^= 0x01  # Chunk 0: the PDF header, which every load reads.
    damaged.write_bytes(bytes(raw))

    # PDFium would report this block as a password problem.
    with pytest.raises(PdfOpenError, match="damaged"):
        _reader(keyring).open_document(damaged)


def test_damage_to_the_trailer_fails_the_open_itself(
    qtbot, tmp_path: Path, sealed_pdf: Path, keyring: VaultKeyring  # noqa: ANN001, ARG001
) -> None:
    raw = bytearray(sealed_pdf.read_bytes())
    raw[-20] ^= 0x01  # The final chunk's tag, which holds the trailer.
    damaged = tmp_path / "trailer.jrl"
    damaged.write_bytes(bytes(raw))

    with pytest.raises(VaultIntegrityError):
        _reader(keyring).open_document(damaged)


def test_closing_the_session_closes_the_sealed_file(
    qtbot, sealed_pdf: Path, key: VaultPrivateKey  # noqa: ANN001, ARG001
) -> None:
    sealed = SealedFile.open(sealed_pdf, key)
    session = PdfImageService().open_sealed(sealed, source_path=sealed_pdf)
    session.prepare_page(_request(0))

    session.close()
    pdf_thread().call(lambda: None)  # Disposal is queued on the PDF thread.

    with pytest.raises(ValueError, match="closed"):
        sealed.read(0, 16)


def test_a_sealed_archive_handed_to_the_pdf_engine_is_refused_and_closed(
    qtbot, tmp_path: Path, key: VaultPrivateKey  # noqa: ANN001, ARG001
) -> None:
    path = seal_file(tmp_path / "book.jrl", b"PK\x03\x04 not a pdf", key, ContentType.CBZ)
    sealed = SealedFile.open(path, key)

    with pytest.raises(PdfOpenError):
        PdfImageService().open_sealed(sealed, source_path=path)
    with pytest.raises(ValueError, match="closed"):
        sealed.read(0, 4)
