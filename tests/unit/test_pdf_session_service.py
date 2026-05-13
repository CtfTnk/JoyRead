from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image
from PIL import ImageDraw
from PySide6.QtGui import QPainter, QPdfWriter

from joyread.core.archive import ArchiveImageService
from joyread.core.reader import ReaderSessionService
from joyread.core.reader import pdf_session as pdf_module
from joyread.core.reader.pdf_session import PdfImageService, _normalize_rendered_pdf_png


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


def test_pdf_image_service_preserves_page_box_by_default(
    tmp_path: Path,
    qtbot,  # noqa: ANN001, ARG001
    monkeypatch,
) -> None:
    pdf_path = tmp_path / "sample.pdf"
    _write_pdf(pdf_path)

    def fail_if_called(_payload: bytes) -> tuple[bytes, tuple[int, int]]:
        raise AssertionError("PDF margin normalization should be opt-in.")

    monkeypatch.setattr(pdf_module, "_normalize_rendered_pdf_png", fail_if_called)

    session = PdfImageService().open(pdf_path)
    page = session.get_page(0)

    assert page is not None
    assert page.dimensions == session.get_dimensions(0)


def test_pdf_image_service_can_opt_into_margin_normalization(
    tmp_path: Path,
    qtbot,  # noqa: ANN001, ARG001
    monkeypatch,
) -> None:
    pdf_path = tmp_path / "sample.pdf"
    _write_pdf(pdf_path)
    calls: list[bytes] = []

    def normalize(payload: bytes) -> tuple[bytes, tuple[int, int]]:
        calls.append(payload)
        return payload, (123, 456)

    monkeypatch.setattr(pdf_module, "_normalize_rendered_pdf_png", normalize)

    session = PdfImageService(normalize_margins=True).open(pdf_path)
    page = session.get_page(0)

    assert calls
    assert page is not None
    assert page.dimensions == (123, 456)


def test_reader_session_service_dispatches_pdf_documents(tmp_path: Path, qtbot) -> None:  # noqa: ARG001
    pdf_path = tmp_path / "sample.pdf"
    _write_pdf(pdf_path)
    service = ReaderSessionService(ArchiveImageService())

    session = service.open_document(pdf_path)
    pages = service.load_pages(session, (0,))

    assert session.page_count == 1
    assert list(pages) == [0]


def test_pdf_margin_normalization_crops_asymmetric_white_margins() -> None:
    source = Image.new("RGBA", (400, 600), "white")
    draw = ImageDraw.Draw(source)
    draw.rectangle((110, 50, 378, 558), fill="black")
    payload = _png_bytes(source)

    normalized, dimensions = _normalize_rendered_pdf_png(payload)

    assert dimensions[0] < 400
    assert dimensions[1] < 600
    with Image.open(BytesIO(normalized)) as image:
        assert image.size == dimensions


def test_pdf_margin_normalization_keeps_full_bleed_pages() -> None:
    source = Image.new("RGBA", (400, 600), "black")
    payload = _png_bytes(source)

    normalized, dimensions = _normalize_rendered_pdf_png(payload)

    assert dimensions == (400, 600)
    assert normalized == payload


def test_pdf_margin_normalization_avoids_tiny_content_overcrop() -> None:
    source = Image.new("RGBA", (400, 600), "white")
    draw = ImageDraw.Draw(source)
    draw.rectangle((180, 280, 220, 310), fill="black")
    payload = _png_bytes(source)

    normalized, dimensions = _normalize_rendered_pdf_png(payload)

    assert dimensions == (400, 600)
    assert normalized == payload


def _png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
