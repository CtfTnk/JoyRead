from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image
from PySide6.QtGui import QPainter, QPdfWriter

from joyread.core.archive import ArchiveImageService
from joyread.core.reader import ReaderSessionService
from joyread.core.reader.pdf_session import PdfImageService


def _write_pdf(path: Path, pages: int = 1) -> None:
    writer = QPdfWriter(str(path))
    painter = QPainter(writer)
    for index in range(pages):
        if index:
            writer.newPage()
        painter.drawText(40, 80, f"JoyRead PDF page {index + 1}")
    painter.end()


def test_pdf_image_service_opens_counts_and_renders_pages(tmp_path: Path, qtbot) -> None:  # noqa: ARG001
    pdf_path = tmp_path / "sample.pdf"
    _write_pdf(pdf_path, pages=2)

    session = PdfImageService().open(pdf_path)
    first_page = session.get_page(0)

    assert session.page_count == 2
    assert session.get_dimensions(0) is not None
    assert first_page is not None
    assert first_page.page_index == 0
    with Image.open(BytesIO(first_page.image_bytes)) as image:
        assert image.width > 0
        assert image.height > 0


def test_reader_session_service_dispatches_pdf_documents(tmp_path: Path, qtbot) -> None:  # noqa: ARG001
    pdf_path = tmp_path / "sample.pdf"
    _write_pdf(pdf_path)
    service = ReaderSessionService(ArchiveImageService())

    session = service.open_document(pdf_path)
    pages = service.load_pages(session, (0,))

    assert session.page_count == 1
    assert list(pages) == [0]
