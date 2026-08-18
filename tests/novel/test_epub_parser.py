"""Unit tests for the MIT-safe EPUB parser."""

from __future__ import annotations

from pathlib import Path

import pytest

# Before any joyread.novel import: the parser imports lxml at module level, so
# without the joyread[epub] extra the import below raises rather than skips.
# This belongs in the module rather than the directory conftest -- a conftest
# that raises Skipped makes pytest exit 1 when this directory is collected on
# its own, instead of skipping cleanly.
pytest.importorskip("lxml", reason="joyread[epub] extra not installed")

from joyread.novel.core.epub import (  # noqa: E402
    InvalidEpubError,
    ZipFileAssetReader,
    flatten_toc,
    open_epub,
)
from joyread.novel.core.epub_session import open_epub_session  # noqa: E402

from tests.support.epub_fixtures import write_tiny_epub  # noqa: E402


# Real-world fixtures live under ``test_set/`` and are not shipped with
# the repository. CI skips these gracefully.
_TEST_SET = Path(__file__).resolve().parents[2] / "test_set"
_KONOSUBA = _TEST_SET / (
    "Konosuba God’s Blessing on This Wonderful World, Vol. 1 Oh My Useless Goddess "
    "(Natsume Akatsuki) (z-library.sk, 1lib.sk, z-lib.sk).epub"
)
_OKAASAN = _TEST_SET / "通常攻撃が全体攻撃で二回攻撃のお母さんは好きですか？ (井中 だちま) (Z-Library).epub"


def test_open_epub_parses_metadata_and_spine(tmp_path: Path) -> None:
    source = write_tiny_epub(
        tmp_path / "novel.epub",
        title="Parser Test",
        author="Author A",
        language="en",
        chapter_titles=("Prologue", "Chapter 1", "Epilogue"),
    )
    book, reader = open_epub(source)
    try:
        assert book.metadata.title == "Parser Test"
        assert book.metadata.creators == ("Author A",)
        assert book.metadata.language == "en"
        assert book.metadata.primary_writing_mode is None
        assert len(book.spine) == 3
        assert len(book.toc) == 3
        # All TOC entries should resolve to a spine index (none -1).
        assert {item.spine_index for item in flatten_toc(book.toc)} == {0, 1, 2}
    finally:
        reader.close()


def test_open_epub_propagates_writing_mode(tmp_path: Path) -> None:
    source = write_tiny_epub(
        tmp_path / "vertical.epub",
        primary_writing_mode="vertical-rl",
    )
    book, reader = open_epub(source)
    try:
        assert book.metadata.primary_writing_mode == "vertical-rl"
    finally:
        reader.close()


def test_invalid_epub_raises(tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.epub"
    bogus.write_bytes(b"not a zip")
    with pytest.raises(Exception):
        open_epub(bogus)


def test_session_loads_chapter_html(tmp_path: Path) -> None:
    source = write_tiny_epub(tmp_path / "novel.epub", chapter_titles=("Intro",))
    session = open_epub_session(source)
    try:
        chapter = session.load_chapter(0)
        assert "Intro" in chapter.html
        assert chapter.spine_index == 0
        assert chapter.language == "en"
    finally:
        session.close()


def test_zipfile_asset_reader_exists_and_reads(tmp_path: Path) -> None:
    source = write_tiny_epub(tmp_path / "novel.epub")
    reader = ZipFileAssetReader(source)
    try:
        assert reader.exists("OEBPS/content.opf")
        assert not reader.exists("OEBPS/nonexistent.xhtml")
        data = reader.read("OEBPS/content.opf")
        assert b"<package" in data
    finally:
        reader.close()


@pytest.mark.skipif(not _KONOSUBA.exists(), reason="Konosuba test fixture missing")
def test_real_konosuba_epub_parses() -> None:
    book, reader = open_epub(_KONOSUBA)
    try:
        assert book.metadata.title.startswith("Konosuba")
        assert book.metadata.language == "en"
        assert len(book.spine) > 0
        # NCX-derived TOC should populate at least the prologue + chapters.
        assert len(book.toc) >= 5
    finally:
        reader.close()


@pytest.mark.skipif(not _OKAASAN.exists(), reason="Okaasan test fixture missing")
def test_real_okaasan_epub_parses() -> None:
    book, reader = open_epub(_OKAASAN)
    try:
        assert book.metadata.language == "ja"
        assert book.metadata.primary_writing_mode == "vertical-rl"
        # TOC labels should be Japanese without mojibake.
        toc_labels = "".join(item.label for item in flatten_toc(book.toc))
        assert any(0x3040 <= ord(c) <= 0x30FF for c in toc_labels), (
            "expected hiragana/katakana in Japanese TOC labels"
        )
    finally:
        reader.close()
