"""The contract that holds when the novel reader is not wired in.

These tests describe the app *without* EPUB support and must therefore keep
passing in an environment where ``joyread.novel`` and ``lxml`` are not
installed at all. Nothing here may import a novel module, directly or
transitively -- the grep guard at the bottom is what stops that rule from
quietly eroding.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from joyread.app.app_context import create_app_context
from joyread.core.file_types import SUPPORTED_READER_EXTENSIONS
from joyread.core.models.book import Book
from joyread.core.services.import_service import BOOK_EXTENSIONS
from joyread.ui.views.main_window import MainWindow

from tests.support.epub_fixtures import write_tiny_epub


SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "joyread"


def _novel_book(source: Path) -> Book:
    now = datetime.now()
    return Book(
        uuid="novel-book",
        title="Test Novel",
        author=None,
        language_tag="en",
        language_name="English",
        book_type="Novel",
        file_format=source.suffix.lstrip(".").upper(),
        file_path=str(source),
        progress=0.0,
        cover_thumbnail_path=None,
        added_at=now,
        updated_at=now,
        last_read_at=None,
        is_favourite=False,
        original_file_name=source.name,
    )


def test_epub_is_neither_readable_nor_importable() -> None:
    """The gate's whole user-visible effect, stated once."""

    assert ".epub" not in SUPPORTED_READER_EXTENSIONS
    assert ".epub" not in BOOK_EXTENSIONS


def test_a_main_window_without_a_novel_provider_claims_no_epub(qtbot, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """No provider means no path is a novel source -- the routing predicate
    that replaced the old module-level ``NOVEL_FORMATS`` constant."""

    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    context = create_app_context()
    window = MainWindow(context, standalone_reader_launcher=lambda _request: None)
    qtbot.addWidget(window)

    assert not window._is_novel_source(Path("/tmp/book.epub"))  # noqa: SLF001
    assert not window._is_novel_source(Path("/tmp/book.EPUB"))  # noqa: SLF001
    assert not window._is_novel_source(Path("/tmp/book.cbz"))  # noqa: SLF001
    # Same suffix, opposite answer: with nothing able to open it, an EPUB is
    # "shelved" rather than "novel", which is what routes it to the dialog.
    assert window._is_shelved_epub(Path("/tmp/book.epub"))  # noqa: SLF001
    assert not window._is_shelved_epub(Path("/tmp/book.cbz"))  # noqa: SLF001

    window.close()
    context.close()


def test_main_window_blocks_existing_epub_book(qtbot, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    context = create_app_context()
    novel_path = write_tiny_epub(tmp_path / "story.epub")
    book = _novel_book(novel_path)

    launch_requests: list[object] = []
    window = MainWindow(context, standalone_reader_launcher=launch_requests.append)
    qtbot.addWidget(window)
    # Replace the shelf after MainWindow.__init__ has run load_books();
    # the routing decision in open_reader_for_book reads ``books`` live.
    context.shelf_viewmodel.books = [book]
    context.settings_store.update(individual_read_window=True)

    window.open_reader_for_book(book.uuid)
    assert launch_requests == []
    assert not window.dialog_overlay.isHidden()
    assert window.dialog_overlay.panel.title_text == "Read"

    window.close()
    context.close()


def test_main_window_blocks_epub_file_open(qtbot, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    context = create_app_context()
    novel_path = write_tiny_epub(tmp_path / "anything.epub")

    launch_requests: list[object] = []
    window = MainWindow(context, standalone_reader_launcher=launch_requests.append)
    qtbot.addWidget(window)

    window.open_reader_for_file(novel_path, import_mode=True)

    assert launch_requests == []
    assert not window.dialog_overlay.isHidden()
    assert window.dialog_overlay.panel.title_text == "Read"

    window.close()
    context.close()


def test_no_application_module_imports_the_novel_package() -> None:
    """The rule that keeps the feature removable.

    ``joyread.novel`` may be imported by exactly one place -- the composition
    root, behind the release gate. Any other importer would drag the feature
    (and ``lxml``) back into every install, which is the entanglement this
    whole split exists to undo. Checked as text because an import-time check
    could only catch what the test run happens to load.
    """

    # Import statements only -- prose references to the package in docstrings
    # are how the boundary gets explained, and must not trip the guard.
    import_pattern = re.compile(r"^\s*(?:from|import)\s+joyread\.novel\b", re.MULTILINE)
    allowed = {SOURCE_ROOT / "app" / "bootstrap.py"}
    offenders = []
    for path in SOURCE_ROOT.rglob("*.py"):
        if path in allowed or "novel" in path.parts:
            continue
        if import_pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(SOURCE_ROOT)))

    assert not offenders, (
        "these modules import joyread.novel outside the bootstrap gate: "
        f"{sorted(offenders)}"
    )
