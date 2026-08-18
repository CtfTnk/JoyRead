"""Tiny EPUB factories for unit tests.

Builds a minimal valid EPUB 3 archive in memory: ``mimetype``,
``META-INF/container.xml``, ``OEBPS/content.opf``, ``OEBPS/nav.xhtml``,
plus N chapter XHTML files. Enough for the parser + viewmodel +
content widget to exercise their happy paths without depending on
the user's local ``test_set/`` files.
"""

from __future__ import annotations

import zipfile
from pathlib import Path


def write_tiny_epub(
    target: Path,
    *,
    title: str = "Tiny Test Novel",
    author: str = "Test Author",
    language: str = "en",
    chapter_titles: tuple[str, ...] = ("Prologue", "Chapter 1", "Chapter 2", "Epilogue"),
    chapter_body_paragraphs: int = 8,
    primary_writing_mode: str | None = None,
) -> Path:
    """Write a minimal EPUB to ``target`` and return the path."""

    target.parent.mkdir(parents=True, exist_ok=True)
    chapter_filenames = [f"chapter_{index:03d}.xhtml" for index in range(len(chapter_titles))]

    with zipfile.ZipFile(target, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        # The EPUB spec requires the ``mimetype`` entry to be the FIRST
        # file in the archive and stored without compression.
        zf.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        zf.writestr("META-INF/container.xml", _container_xml())
        zf.writestr(
            "OEBPS/content.opf",
            _opf_xml(
                title=title,
                author=author,
                language=language,
                chapter_filenames=chapter_filenames,
                primary_writing_mode=primary_writing_mode,
            ),
        )
        zf.writestr(
            "OEBPS/nav.xhtml",
            _nav_xhtml(
                chapter_titles=chapter_titles,
                chapter_filenames=chapter_filenames,
                language=language,
            ),
        )
        for filename, chapter_title in zip(chapter_filenames, chapter_titles, strict=True):
            zf.writestr(
                f"OEBPS/{filename}",
                _chapter_xhtml(
                    chapter_title=chapter_title,
                    paragraph_count=chapter_body_paragraphs,
                    language=language,
                ),
            )
    return target


def _container_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">\n'
        "  <rootfiles>\n"
        '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n"
        "</container>\n"
    )


def _opf_xml(
    *,
    title: str,
    author: str,
    language: str,
    chapter_filenames: list[str],
    primary_writing_mode: str | None,
) -> str:
    manifest_items: list[str] = []
    spine_items: list[str] = []
    for index, filename in enumerate(chapter_filenames):
        item_id = f"chapter_{index:03d}"
        manifest_items.append(
            f'<item id="{item_id}" href="{filename}" media-type="application/xhtml+xml"/>'
        )
        spine_items.append(f'<itemref idref="{item_id}"/>')
    manifest_items.append(
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
    )
    mode_meta = (
        f'<meta name="primary-writing-mode" content="{primary_writing_mode}"/>'
        if primary_writing_mode
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="bookid" xml:lang="en">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f"    <dc:title>{_escape(title)}</dc:title>\n"
        f"    <dc:creator>{_escape(author)}</dc:creator>\n"
        f"    <dc:language>{_escape(language)}</dc:language>\n"
        '    <dc:identifier id="bookid">urn:uuid:tiny-test-novel</dc:identifier>\n'
        '    <meta property="dcterms:modified">2025-01-01T00:00:00Z</meta>\n'
        f"    {mode_meta}\n"
        "  </metadata>\n"
        "  <manifest>\n    "
        + "\n    ".join(manifest_items)
        + "\n  </manifest>\n  <spine>\n    "
        + "\n    ".join(spine_items)
        + "\n  </spine>\n</package>\n"
    )


def _nav_xhtml(
    *,
    chapter_titles: tuple[str, ...],
    chapter_filenames: list[str],
    language: str,
) -> str:
    list_items = "\n      ".join(
        f'<li><a href="{filename}">{_escape(title)}</a></li>'
        for title, filename in zip(chapter_titles, chapter_filenames, strict=True)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{_escape(language)}">\n'
        "<head><title>TOC</title></head>\n"
        "<body>\n"
        '  <nav epub:type="toc">\n'
        "    <h1>Contents</h1>\n"
        "    <ol>\n      "
        + list_items
        + "\n    </ol>\n  </nav>\n</body>\n</html>\n"
    )


def _chapter_xhtml(*, chapter_title: str, paragraph_count: int, language: str) -> str:
    paragraphs = "\n  ".join(
        f"<p>This is paragraph {index + 1} of {_escape(chapter_title)}. "
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
        "Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. "
        "Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris "
        "nisi ut aliquip ex ea commodo consequat.</p>"
        for index in range(paragraph_count)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{_escape(language)}">\n'
        f"<head><title>{_escape(chapter_title)}</title></head>\n"
        "<body>\n"
        f"  <h1>{_escape(chapter_title)}</h1>\n  "
        + paragraphs
        + "\n</body>\n</html>\n"
    )


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


__all__ = ["write_tiny_epub"]
