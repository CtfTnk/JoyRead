"""The runtime's cache-identity and privacy-purge policy, against a real pool.

The pool-level mechanics (`mark_session_scoped`, purge on last release,
promotion carrying the mark) are covered in `test_archive_extraction_pool.py`.
These tests cover the layer above: whether the runtime asks the pool for the
right thing, for the right documents -- the wiring a pool-level test cannot
see failing.
"""

from __future__ import annotations

from pathlib import Path

from joyread.app.reader_document_runtime import ReaderDocumentRuntime
from joyread.core.archive import ArchiveOpenLimits
from joyread.core.services.archive_extraction_pool import ArchiveExtractionPool


class _FakeSessionService:
    """Captures the lease the runtime hands to the session layer."""

    def __init__(self) -> None:
        self.lease = None

    def open_document(self, path, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.lease = kwargs.get("cache_lease")
        return object()

    def load_pages(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return {}


def _open(
    tmp_path: Path,
    *,
    passwords: dict[str, str],
    purge: bool = True,
    cache_key: str = "file:book1",
) -> tuple[ArchiveExtractionPool, object]:
    pool = ArchiveExtractionPool(tmp_path / "pool", 1 << 20)
    service = _FakeSessionService()
    runtime = ReaderDocumentRuntime(
        service,  # type: ignore[arg-type]
        document_cache_key=cache_key,
        archive_extraction_cache=pool,
        purge_encrypted_cache_on_close=purge,
    )
    source = tmp_path / "book.cbz"
    source.write_bytes(b"fake")
    runtime.open_document(
        source, passwords=passwords, skipped_archives=set(), limits=ArchiveOpenLimits()
    )
    assert service.lease is not None, "an encrypted document must get a pool lease"
    return pool, service.lease


def test_the_top_level_password_marks_the_document(tmp_path: Path) -> None:
    source_key = str(tmp_path / "book.cbz")
    pool, lease = _open(tmp_path, passwords={source_key: "pw"})
    lease.put("pages/00000000", b"decrypted page")

    lease.close()

    assert pool.current_bytes == 0, (
        "a managed encrypted book with the switch on must purge on close"
    )


def test_a_nested_password_alone_does_not_mark_the_document(tmp_path: Path) -> None:
    """A plain container with one protected extra inside is not an encrypted
    document. Marking it would purge the whole (mostly ordinary) cache on
    every close -- and the nested pages never reach the pool anyway, so there
    is nothing sensitive being protected by doing so."""

    pool = ArchiveExtractionPool(tmp_path / "pool", 1 << 20)
    service = _FakeSessionService()
    runtime = ReaderDocumentRuntime(
        service,  # type: ignore[arg-type]
        document_cache_key="file:book1",
        archive_extraction_cache=pool,
        purge_encrypted_cache_on_close=True,
    )
    source = tmp_path / "book.7z"  # expensive by format, so a lease exists
    source.write_bytes(b"fake")
    runtime.open_document(
        source,
        passwords={"book.7z::nested.cbz": "pw"},
        skipped_archives=set(),
        limits=ArchiveOpenLimits(),
    )
    lease = service.lease
    assert lease is not None
    lease.put("pages/00000000", b"ordinary page")

    lease.close()

    assert pool.current_bytes > 0, (
        "a nested-archive password must not cost the outer document its cache"
    )


def test_the_switch_off_leaves_an_encrypted_book_cached(tmp_path: Path) -> None:
    source_key = str(tmp_path / "book.cbz")
    pool, lease = _open(tmp_path, passwords={source_key: "pw"}, purge=False)
    lease.put("pages/00000000", b"decrypted page")

    lease.close()

    assert pool.current_bytes > 0
