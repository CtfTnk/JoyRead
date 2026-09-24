"""The startup corpus must be stable and readable on every target platform."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from zipfile import ZipFile

from PySide6.QtPdf import QPdfDocument


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_generator():
    spec = importlib.util.spec_from_file_location("prepare_startup_fixture", REPO_ROOT / "scripts" / "prepare_startup_fixture.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_cbz_and_pdf_are_byte_stable_and_readable(tmp_path: Path) -> None:
    generator = _load_generator()
    cbz_a = tmp_path / "a.cbz"
    cbz_b = tmp_path / "b.cbz"
    pdf_a = tmp_path / "a.pdf"
    pdf_b = tmp_path / "b.pdf"
    generator._write_cbz(cbz_a, 2, (24, 32), 7)
    generator._write_cbz(cbz_b, 2, (24, 32), 7)
    generator._write_pdf(pdf_a, 2, (24, 32))
    generator._write_pdf(pdf_b, 2, (24, 32))

    assert cbz_a.read_bytes() == cbz_b.read_bytes()
    assert pdf_a.read_bytes() == pdf_b.read_bytes()
    with ZipFile(cbz_a) as archive:
        assert archive.namelist() == ["001.png", "002.png"]
        assert archive.read("001.png").startswith(b"\x89PNG")
    pdf = QPdfDocument()
    assert pdf.load(str(pdf_a)) == QPdfDocument.Error.None_
    assert pdf.pageCount() == 2
    pdf.close()
