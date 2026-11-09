"""Hidden Space sealing: hiding encrypts, unhiding decrypts, and a crash anywhere recovers."""

from __future__ import annotations

import logging
from pathlib import Path
from time import monotonic, sleep

import pytest

from joyread.core.archive import ArchiveError
from joyread.core.archive.canonical import CanonicalWriteCancelled
from joyread.core.models.import_policy import CanonicalImportPolicy
from joyread.core.models.vault import VaultJobDirection, VaultJobPhase
from joyread.core.repositories.sqlite_sealing_repository import CANCELLED_ERROR_CODE, UnhideOutcome
from joyread.core.services import sealing_writers
from joyread.core.services.sealing_service import RESTORED_COVER_SUFFIX
from joyread.core.vault import VaultLockedError
from joyread.infrastructure.database import DatabasePriority
from tests.support.vault_library import (
    PASSPHRASE,
    VaultLibrary,
    build_vault_library,
    png_bytes,
    sample_pages,
    write_cbz,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class _Crash(BaseException):
    """Stands in for the process dying at one exact step."""


def _crash_once(monkeypatch: pytest.MonkeyPatch, target: object, name: str) -> None:
    real = getattr(target, name)
    fired = []

    def crash(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        if not fired:
            fired.append(name)
            raise _Crash(name)
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, crash)


@pytest.fixture
def library(tmp_path: Path):
    lib = build_vault_library(tmp_path)
    yield lib
    lib.close()


def _add_book(lib: VaultLibrary, tmp_path: Path, name: str = "Book.cbz", pages: int = 3) -> tuple[str, list[bytes]]:
    content = sample_pages(pages)
    book = lib.import_file(write_cbz(tmp_path / "incoming" / name, content))
    return book, lib.read_pages(book)


def _give_cover(lib: VaultLibrary, book: str) -> bytes:
    cover = png_bytes((200, 284))
    path = lib.paths.paths.thumbnails / "covers" / f"{book}-generated-200x284.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cover)
    return cover


def _book_files(lib: VaultLibrary) -> list[str]:
    root = lib.paths.paths.books
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def _seal(lib: VaultLibrary, book: str) -> None:
    lib.sealing.hide_books([book])
    lib.sealing.run_pending()
    assert lib.file_row(book)["storage_kind"] == "sealed"


# -- sealing ----------------------------------------------------------------


def test_hiding_encrypts_the_book_and_deletes_every_plaintext_copy(library: VaultLibrary, tmp_path: Path) -> None:
    book, pages = _add_book(library, tmp_path)
    cover = _give_cover(library, book)
    original = library.stored_path(book)
    library.create_vault()

    assert library.sealing.hide_books([book]) == (book,)
    hidden = library.file_row(book)
    assert hidden["is_hidden"] == 1 and hidden["storage_kind"] == "verbatim"
    assert library.sealing.run_pending() == 1

    row = library.file_row(book)
    sealed = library.stored_path(book)
    assert row["storage_kind"] == "sealed" and row["file_format"] == "CBZ"
    assert sealed.suffix == ".jrl" and sealed.parent.name == row["file_id"][:2]
    assert not original.exists()
    assert PNG_SIGNATURE not in sealed.read_bytes()
    assert library.read_pages(book) == pages
    assert library.sealing.jobs_by_book() == {}
    assert row["stored_hash"] == library.hash_service.compute(sealed, row["hash_algorithm"])
    # The cover went the same way: sealed under its fixed name, PNG gone.
    covers = library.paths.paths.thumbnails / "covers"
    assert not list(covers.glob(f"{book}-*.png"))
    assert row["cover_path"] == library.paths.resolver.to_storage_relative(library.cover_store.path_for(book))
    assert library.cover_store.read(book) == cover
    assert library.invalidated == [row["file_id"]]


def test_sealing_runs_while_the_vault_is_locked(library: VaultLibrary, tmp_path: Path) -> None:
    book, pages = _add_book(library, tmp_path)
    library.create_vault()
    library.vault.lock()

    _seal(library, book)

    with pytest.raises(VaultLockedError):
        library.read_pages(book)
    library.vault.unlock(PASSPHRASE)
    assert library.read_pages(book) == pages


def test_a_canonical_book_is_sealed_byte_for_byte_and_comes_back_identical(tmp_path: Path) -> None:
    library = build_vault_library(tmp_path, canonical_import_policy=CanonicalImportPolicy.ALWAYS)
    try:
        book, _pages = _add_book(library, tmp_path)
        before = library.file_row(book)
        assert before["storage_kind"] == "canonical"
        original = library.stored_path(book).read_bytes()
        library.create_vault()

        _seal(library, book)
        library.sealing.unhide_books([book], wait_seconds=0)
        library.sealing.run_pending()

        after = library.file_row(book)
        assert after["storage_kind"] == "canonical" and after["is_hidden"] == 0
        assert after["storage_path"] == before["storage_path"]
        assert after["stored_hash"] == before["stored_hash"]
        assert library.stored_path(book).read_bytes() == original
    finally:
        library.close()


def test_a_file_another_book_shares_is_left_alone(library: VaultLibrary, tmp_path: Path) -> None:
    book, _pages = _add_book(library, tmp_path)
    original = library.stored_path(book)
    def duplicate(connection):  # noqa: ANN001, ANN202
        # Two books on one file: what an audit merge of duplicates leaves.
        columns = [row["name"] for row in connection.execute("PRAGMA table_info(books)").fetchall()]
        selected = ", ".join("?" if column == "book_id" else column for column in columns)
        connection.execute(
            f"INSERT INTO books({', '.join(columns)}) SELECT {selected} FROM books WHERE book_id = ?",
            ("twin-book", book),
        )

    library.database.execute(duplicate, DatabasePriority.NORMAL)
    library.create_vault()

    library.sealing.hide_books([book])
    library.sealing.run_pending()

    job = library.sealing.jobs_by_book()[book]
    assert (job.phase, job.error_code) == (VaultJobPhase.FAILED, "shared_file")
    assert original.is_file() and library.file_row(book)["storage_kind"] == "verbatim"


def test_a_stored_file_that_changed_is_not_sealed_until_it_is_back(tmp_path: Path) -> None:
    library = build_vault_library(tmp_path, canonical_import_policy=CanonicalImportPolicy.ALWAYS)
    try:
        book, _pages = _add_book(library, tmp_path)
        stored = library.stored_path(book)
        original = stored.read_bytes()
        stored.write_bytes(original + b"tampered")
        library.create_vault()

        library.sealing.hide_books([book])
        library.sealing.run_pending()

        job = library.sealing.jobs_by_book()[book]
        assert (job.phase, job.error_code) == (VaultJobPhase.FAILED, "source_changed")
        assert stored.read_bytes() == original + b"tampered"
        assert _book_files(library) == [str(stored.relative_to(library.paths.paths.books))]

        # Retried on the next launch.
        stored.write_bytes(original)
        assert library.sealing.recover() == 1
        library.sealing.run_pending()
        assert library.file_row(book)["storage_kind"] == "sealed"
    finally:
        library.close()


def test_launch_seals_books_hidden_before_the_vault_existed(library: VaultLibrary, tmp_path: Path) -> None:
    book, pages = _add_book(library, tmp_path)
    library.books.set_book_hidden(book, True)  # Soft hiding, as before the vault.
    assert library.sealing.recover() == 0  # No vault yet: nothing to seal to.
    library.create_vault()

    assert library.sealing.recover() == 1
    library.sealing.run_pending()

    assert library.file_row(book)["storage_kind"] == "sealed"
    assert library.read_pages(book) == pages


def test_a_visible_book_with_a_sealed_file_is_hidden_until_it_is_decrypted(
    library: VaultLibrary, tmp_path: Path
) -> None:
    book, _pages = _add_book(library, tmp_path)
    library.create_vault()
    _seal(library, book)
    library.database.execute(
        lambda connection: connection.execute("UPDATE books SET is_hidden = 0 WHERE book_id = ?", (book,)),
        DatabasePriority.NORMAL,
    )

    library.sealing.recover()

    assert library.file_row(book)["is_hidden"] == 1
    assert library.sealing.jobs_by_book()[book].direction is VaultJobDirection.UNSEAL


def test_cancelling_mid_seal_leaves_the_job_queued_and_nothing_on_disk(
    library: VaultLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book, _pages = _add_book(library, tmp_path)
    before = _book_files(library)
    library.create_vault()

    def cancelled(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise CanonicalWriteCancelled()

    library.sealing.hide_books([book])
    with monkeypatch.context() as patch:
        patch.setattr(sealing_writers.SealedCanonicalWriter, "write", cancelled)
        library.sealing.run_pending()

    job = library.sealing.jobs_by_book()[book]
    assert (job.phase, job.partial_path) == (VaultJobPhase.QUEUED, None)
    assert _book_files(library) == before
    library.sealing.run_pending()
    assert library.file_row(book)["storage_kind"] == "sealed"


def test_a_conversion_error_fails_the_job_and_keeps_the_book(
    library: VaultLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book, pages = _add_book(library, tmp_path)
    library.create_vault()

    def broken(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise ArchiveError("unreadable entry")

    monkeypatch.setattr(library.archive_service, "convert_to_canonical", broken)
    library.sealing.hide_books([book])
    library.sealing.run_pending()

    job = library.sealing.jobs_by_book()[book]
    assert (job.phase, job.error_code) == (VaultJobPhase.FAILED, "conversion_failed")
    assert library.read_pages(book) == pages


# -- unsealing --------------------------------------------------------------


def test_unhiding_waits_for_the_key_then_restores_the_book_and_its_cover(
    library: VaultLibrary, tmp_path: Path
) -> None:
    book, pages = _add_book(library, tmp_path)
    cover = _give_cover(library, book)
    library.create_vault()
    _seal(library, book)
    sealed = library.stored_path(book)
    library.vault.lock()

    assert library.sealing.unhide_books([book], wait_seconds=0) == {book: UnhideOutcome.UNSEALING}
    assert library.sealing.run_pending() == 0
    assert library.file_row(book)["is_hidden"] == 1  # Still hidden: nothing decrypted yet.

    library.vault.unlock(PASSPHRASE)
    assert library.sealing.run_pending() == 1

    row = library.file_row(book)
    assert (row["is_hidden"], row["storage_kind"], row["file_format"]) == (0, "canonical", "CBZ")
    assert library.stored_path(book).suffix == ".cbz"
    assert library.read_pages(book) == pages
    assert not sealed.exists()
    assert not library.cover_store.path_for(book).exists()
    restored = library.paths.paths.thumbnails / "covers" / f"{book}{RESTORED_COVER_SUFFIX}"
    assert row["cover_path"] == library.paths.resolver.to_storage_relative(restored)
    assert restored.read_bytes() == cover


def test_unhiding_a_book_still_waiting_for_its_seal_just_shows_it(library: VaultLibrary, tmp_path: Path) -> None:
    book, _pages = _add_book(library, tmp_path)
    before = _book_files(library)
    library.create_vault()

    library.sealing.hide_books([book])
    outcome = library.sealing.unhide_books([book], wait_seconds=0)

    assert outcome == {book: UnhideOutcome.VISIBLE}
    assert library.file_row(book)["is_hidden"] == 0
    assert library.sealing.jobs_by_book() == {}
    assert library.sealing.run_pending() == 0
    assert _book_files(library) == before


def test_unhide_waits_while_a_seal_finishes_its_cleanup(
    library: VaultLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book, _pages = _add_book(library, tmp_path)
    library.create_vault()
    library.sealing.hide_books([book])
    _crash_once(monkeypatch, library.sealing, "_finish_seal")
    with pytest.raises(_Crash):
        library.sealing.run_pending()
    assert library.sealing.jobs_by_book()[book].phase is VaultJobPhase.COMMITTED

    assert library.sealing.unhide_books([book], wait_seconds=0) == {book: UnhideOutcome.BUSY}
    library.sealing.run_pending()
    assert library.sealing.unhide_books([book], wait_seconds=0) == {book: UnhideOutcome.UNSEALING}


def test_hiding_again_cancels_an_unhide_and_deletes_its_decrypted_copy(
    library: VaultLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book, pages = _add_book(library, tmp_path)
    library.create_vault()
    _seal(library, book)
    sealed_files = _book_files(library)
    library.sealing.unhide_books([book], wait_seconds=0)
    _crash_once(monkeypatch, library.sealing_repository, "commit_unseal")
    with pytest.raises(_Crash):
        library.sealing.run_pending()
    job = library.sealing.jobs_by_book()[book]
    decrypted = library.paths.resolver.to_storage_absolute(job.target_path)
    assert job.phase is VaultJobPhase.WRITTEN and decrypted.is_file()

    assert library.sealing.hide_books([book]) == ()

    job = library.sealing.jobs_by_book()[book]
    assert (job.phase, job.error_code) == (VaultJobPhase.FAILED, CANCELLED_ERROR_CODE)
    library.sealing.run_pending()
    assert not decrypted.exists()
    assert library.sealing.jobs_by_book() == {}
    assert _book_files(library) == sealed_files
    row = library.file_row(book)
    assert (row["is_hidden"], row["storage_kind"]) == (1, "sealed")
    assert library.read_pages(book) == pages


def test_a_pdf_round_trips_byte_for_byte(qtbot, tmp_path: Path) -> None:  # noqa: ANN001, ARG001
    from joyread.infrastructure.pdf_image_service import PdfImageService
    from tests.unit.test_sealed_pdf import _write_pdf

    library = build_vault_library(tmp_path, pdf_service=PdfImageService())
    try:
        source = tmp_path / "incoming" / "Doc.pdf"
        source.parent.mkdir(parents=True)
        original = _write_pdf(source)
        book = library.import_file(source)
        before = library.file_row(book)
        library.create_vault()

        _seal(library, book)
        assert library.file_row(book)["file_format"] == "PDF"
        assert not library.paths.resolver.to_storage_absolute(before["storage_path"]).exists()
        library.sealing.unhide_books([book], wait_seconds=0)
        library.sealing.run_pending()

        after = library.file_row(book)
        assert (after["storage_kind"], after["storage_path"]) == ("verbatim", before["storage_path"])
        assert after["stored_hash"] == before["stored_hash"] == before["source_hash"]
        assert library.stored_path(book).read_bytes() == original
    finally:
        library.close()


# -- crash recovery ---------------------------------------------------------


@pytest.mark.parametrize(
    ("owner", "step"),
    [
        ("repository", "mark_written"),
        ("repository", "commit_seal"),
        ("service", "_finish_seal"),
    ],
)
def test_a_seal_interrupted_at_any_step_recovers(
    library: VaultLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str, step: str
) -> None:
    book, pages = _add_book(library, tmp_path)
    library.create_vault()
    library.sealing.hide_books([book])
    _crash_once(monkeypatch, library.sealing_repository if owner == "repository" else library.sealing, step)

    with pytest.raises(_Crash):
        library.sealing.run_pending()
    # Whatever the row names now is a whole, readable book.
    assert library.read_pages(book) == pages

    library.sealing.run_pending()  # The next launch.

    assert library.file_row(book)["storage_kind"] == "sealed"
    assert library.sealing.jobs_by_book() == {}
    assert _book_files(library) == [library.file_row(book)["storage_path"].removeprefix("Books/")]
    assert library.read_pages(book) == pages


@pytest.mark.parametrize(
    ("owner", "step"),
    [
        ("repository", "mark_written"),
        ("repository", "commit_unseal"),
        ("service", "_finish_unseal"),
    ],
)
def test_an_unseal_interrupted_at_any_step_recovers(
    library: VaultLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str, step: str
) -> None:
    book, pages = _add_book(library, tmp_path)
    library.create_vault()
    _seal(library, book)
    library.sealing.unhide_books([book], wait_seconds=0)
    _crash_once(monkeypatch, library.sealing_repository if owner == "repository" else library.sealing, step)

    with pytest.raises(_Crash):
        library.sealing.run_pending()
    assert library.read_pages(book) == pages

    library.sealing.run_pending()

    row = library.file_row(book)
    assert (row["is_hidden"], row["storage_kind"]) == (0, "canonical")
    assert library.sealing.jobs_by_book() == {}
    assert _book_files(library) == [row["storage_path"].removeprefix("Books/")]
    assert library.read_pages(book) == pages


def test_recovery_restarts_a_written_seal_whose_file_was_damaged(
    library: VaultLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book, pages = _add_book(library, tmp_path)
    library.create_vault()
    library.sealing.hide_books([book])
    _crash_once(monkeypatch, library.sealing_repository, "commit_seal")
    with pytest.raises(_Crash):
        library.sealing.run_pending()
    target = library.paths.resolver.to_storage_absolute(library.sealing.jobs_by_book()[book].target_path)
    target.write_bytes(target.read_bytes()[:-10])

    library.sealing.run_pending()

    assert library.file_row(book)["storage_kind"] == "sealed"
    assert library.read_pages(book) == pages


def test_deleting_a_book_mid_unseal_leaves_no_decrypted_copy(
    library: VaultLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book, _pages = _add_book(library, tmp_path)
    _give_cover(library, book)
    library.create_vault()
    _seal(library, book)
    library.sealing.unhide_books([book], wait_seconds=0)
    _crash_once(monkeypatch, library.sealing_repository, "commit_unseal")
    with pytest.raises(_Crash):
        library.sealing.run_pending()
    assert library.sealing.jobs_by_book()[book].phase is VaultJobPhase.WRITTEN

    library.books.delete_book(book)

    assert _book_files(library) == []
    covers = library.paths.paths.thumbnails / "covers"
    assert not [path for path in covers.iterdir() if path.name.startswith(book)]


# -- the worker thread ------------------------------------------------------


def _eventually(predicate, timeout: float = 10.0) -> bool:  # noqa: ANN001
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return True
        sleep(0.02)
    return predicate()


def test_the_worker_seals_in_the_background_and_pauses_on_request(library: VaultLibrary, tmp_path: Path) -> None:
    first, _pages = _add_book(library, tmp_path, "First.cbz")
    second, _pages = _add_book(library, tmp_path, "Second.cbz")
    library.create_vault()
    changed: list[tuple[str, ...]] = []
    library.sealing.add_listener(changed.append)
    library.sealing.start()

    library.sealing.hide_books([first])
    assert _eventually(lambda: library.file_row(first)["storage_kind"] == "sealed")
    assert _eventually(lambda: library.sealing.jobs_by_book() == {})
    assert (first,) in changed

    library.sealing.pause()
    library.sealing.hide_books([second])
    sleep(0.3)
    assert library.file_row(second)["storage_kind"] == "verbatim"

    library.sealing.resume()
    assert _eventually(lambda: library.file_row(second)["storage_kind"] == "sealed")
    library.sealing.close()
    assert library.sealing.pending_task_count() == 0


def test_the_worker_waits_for_the_maintenance_gate(library: VaultLibrary, tmp_path: Path) -> None:
    book, _pages = _add_book(library, tmp_path)
    library.create_vault()
    library.sealing.hide_books([book])
    lease = library.coordinator.acquire("import")
    try:
        library.sealing.start()
        assert _eventually(lambda: library.sealing.pending_task_count() == 1)
        sleep(0.3)
        assert library.file_row(book)["storage_kind"] == "verbatim"
    finally:
        lease.release()
    assert _eventually(lambda: library.file_row(book)["storage_kind"] == "sealed")


def test_closing_stops_a_worker_waiting_for_the_gate(library: VaultLibrary, tmp_path: Path) -> None:
    book, _pages = _add_book(library, tmp_path)
    library.create_vault()
    library.sealing.hide_books([book])
    lease = library.coordinator.acquire("storage-move")
    try:
        library.sealing.start()
        assert _eventually(lambda: library.sealing.pending_task_count() == 1)
        library.sealing.close(timeout=5)
        assert library.sealing.pending_task_count() == 0
    finally:
        lease.release()
    assert library.sealing.jobs_by_book()[book].phase is VaultJobPhase.QUEUED


# -- privacy ----------------------------------------------------------------


def test_nothing_identifying_reaches_the_logs(
    library: VaultLibrary, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    title = "Zanzibar Moonlight Confession"
    passphrase = "violet-otter-42"
    book, _pages = _add_book(library, tmp_path, f"{title}.cbz")
    caplog.clear()
    caplog.set_level(logging.DEBUG)

    draft = library.vault.prepare(passphrase, passphrase)
    code = draft.recovery_code.formatted()
    library.vault.create(draft, code)
    _seal(library, book)
    library.vault.lock()
    library.vault.unlock(passphrase)
    library.read_pages(book)
    library.sealing.unhide_books([book], wait_seconds=0)
    library.sealing.run_pending()

    text = caplog.text
    assert caplog.records
    for secret in (title, "Zanzibar", passphrase, code, code.replace("-", "")):
        assert secret not in text


def test_an_unhide_stops_waiting_on_a_cleanup_that_keeps_failing(
    library: VaultLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book, _pages = _add_book(library, tmp_path)
    original = library.stored_path(book)
    library.create_vault()
    real_unlink = Path.unlink

    def held_open(self: Path, missing_ok: bool = False) -> None:
        if self == original:
            raise PermissionError(13, "The file is in use")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", held_open)
    library.sealing.hide_books([book])
    library.sealing.start()
    assert _eventually(lambda: library.sealing.jobs_by_book()[book].error_code == "file_in_use")

    started = monotonic()
    assert library.sealing.unhide_books([book], wait_seconds=20) == {book: UnhideOutcome.BUSY}
    assert monotonic() - started < 5

    monkeypatch.undo()
    library.sealing.wake()
    assert _eventually(lambda: library.sealing.jobs_by_book() == {})
    assert not original.exists()


def test_a_cover_that_cannot_be_sealed_does_not_stop_the_book(
    library: VaultLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book, pages = _add_book(library, tmp_path)
    _give_cover(library, book)
    library.create_vault()

    def full_disk(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(library.cover_store, "write", full_disk)
    _seal(library, book)

    row = library.file_row(book)
    assert row["cover_path"] is None
    assert not list((library.paths.paths.thumbnails / "covers").glob(f"{book}*"))
    assert library.read_pages(book) == pages
