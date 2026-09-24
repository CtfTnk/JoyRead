#!/usr/bin/env python3
"""Generate a reproducible, disposable startup benchmark corpus and Library.

Run with JoyRead's repository conda interpreter. The 50-book profile is created
through the real ImportService, then copied by the benchmark before every run.
No existing user Library is read or changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import sys
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo
import zlib


REPO_ROOT = Path(__file__).resolve().parents[1]
PAGE_COUNT = 20
BOOK_COUNT = 50
READER_SIZE = (900, 1200)
LIBRARY_SIZE = (240, 360)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def _rgb_rows(width: int, height: int, seed: int) -> bytes:
    rows = bytearray()
    for y in range(height):
        color = ((seed * 37 + y // 24) % 256, (seed * 59 + y // 32) % 256, (seed * 83 + y // 40) % 256)
        rows.extend(bytes(color) * width)
    return bytes(rows)


def _png(width: int, height: int, seed: int) -> bytes:
    rgb = _rgb_rows(width, height, seed)
    stride = width * 3
    raw = b"".join(b"\0" + rgb[offset : offset + stride] for offset in range(0, len(rgb), stride))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, level=6))
        + _chunk(b"IEND", b"")
    )


def _write_cbz(path: Path, count: int, size: tuple[int, int], seed: int) -> None:
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for index in range(count):
            entry = ZipInfo(f"{index + 1:03d}.png", date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, _png(*size, seed + index))


def _write_pdf(path: Path, count: int, size: tuple[int, int]) -> None:
    """Write a deterministic PDF with one full-page RGB image per page."""

    objects: list[bytes] = [b"", b""]
    page_ids: list[int] = []
    for index in range(count):
        page_id = len(objects) + 1
        content_id = page_id + 1
        image_id = page_id + 2
        page_ids.append(page_id)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Im0 {image_id} 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        content = b"q 612 0 0 792 0 0 cm /Im0 Do Q\n"
        objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream")
        compressed = zlib.compress(_rgb_rows(*size, index + 1), level=6)
        objects.append(
            f"<< /Type /XObject /Subtype /Image /Width {size[0]} /Height {size[1]} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length {len(compressed)} >>\nstream\n".encode()
            + compressed
            + b"\nendstream"
        )
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = b" ".join(f"{page_id} 0 R".encode() for page_id in page_ids)
    objects[1] = b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(count).encode() + b" >>"

    with path.open("wb") as stream:
        stream.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for number, body in enumerate(objects, start=1):
            offsets.append(stream.tell())
            stream.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
        xref = stream.tell()
        stream.write(f"xref\n0 {len(offsets)}\n".encode())
        stream.write(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            stream.write(f"{offset:010d} 00000 n \n".encode())
        stream.write(
            b"trailer\n<< /Size "
            + str(len(offsets)).encode()
            + b" /Root 1 0 R >>\nstartxref\n"
            + str(xref).encode()
            + b"\n%%EOF\n"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare(destination: Path) -> dict[str, object]:
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"Fixture destination must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    samples = destination / "samples"
    sources = destination / "library_sources"
    profile = destination / "profile_template"
    for directory in (samples, sources, profile):
        directory.mkdir()

    cbz = samples / "reader-20-pages.cbz"
    pdf = samples / "reader-20-pages.pdf"
    _write_cbz(cbz, PAGE_COUNT, READER_SIZE, 1)
    _write_pdf(pdf, PAGE_COUNT, READER_SIZE)
    books: list[Path] = []
    for index in range(BOOK_COUNT):
        path = sources / f"book-{index + 1:03d}.cbz"
        _write_cbz(path, 1, LIBRARY_SIZE, index + 101)
        books.append(path)

    os.environ["JOYREAD_RUNTIME_DIR"] = str(profile)
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from PySide6.QtWidgets import QApplication
    from joyread.app.app_context import create_app_context

    app = QApplication.instance() or QApplication([])
    context = create_app_context()
    try:
        result = context.import_service.import_files(books)
        if result.imported_count != BOOK_COUNT or result.failed_count or result.duplicate_count:
            raise RuntimeError(f"Fixture import failed: {result}")
    finally:
        context.close()
        app.processEvents()

    cache = profile / "JoyRead-Library" / "Cache"
    shutil.rmtree(cache, ignore_errors=True)
    cache.mkdir(parents=True, exist_ok=True)
    logs = profile / ".joyread_support" / "Logs"
    shutil.rmtree(logs, ignore_errors=True)
    logs.mkdir(parents=True, exist_ok=True)
    files = {str(path.relative_to(destination)): _sha256(path) for path in (cbz, pdf, *books)}
    manifest: dict[str, object] = {
        "version": 1,
        "page_count": PAGE_COUNT,
        "book_count": BOOK_COUNT,
        "reader_size": list(READER_SIZE),
        "library_size": list(LIBRARY_SIZE),
        "files": files,
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    manifest = prepare(args.destination.expanduser().resolve())
    print(json.dumps({key: value for key, value in manifest.items() if key != "files"}, indent=2))
    print(f"Fixture ready: {args.destination.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
