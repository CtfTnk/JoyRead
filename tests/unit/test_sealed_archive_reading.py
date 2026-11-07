"""Reading sealed Hidden Space books through the archive engine.

The promise under test is that a sealed book reads like any other archive
while its plaintext never reaches the disk: no extraction-pool entry, no
spilled nested archive, no conversion workspace, whatever the caller asks for.
"""

from __future__ import annotations

from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from joyread.app.archive_warmup_coordinator import ArchiveWarmupCoordinator
from joyread.app.reader_runtime import create_archive_image_service, create_reader_session_service
from joyread.core.archive import ArchiveImageService
from joyread.core.archive.errors import (
    ArchiveCorruptError,
    ArchiveOpenError,
    ArchiveUnsupportedFormat,
    ArchiveVaultLocked,
)
from joyread.core.archive.formats.jrl_backend import JrlArchiveBackend, SealedCbzIndex
from joyread.core.archive.inspection import ImportRejection
from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget
from joyread.core.archive.models import ArchiveAccessMode, ArchiveCachePolicy, ArchiveValidationCode
from joyread.core.archive.records import ArchiveSource, PageRecord
from joyread.core.archive.registry import ArchiveBackendRegistry
from joyread.core.archive.session import ArchiveImageSession
from joyread.core.reader import ReaderSessionService
from joyread.core.services.archive_cache_lease import ArchiveCacheLease, ArchiveCacheScope
from joyread.core.services.archive_extraction_pool import ArchiveExtractionPool
from joyread.core.vault import ContentType, VaultKeyring, VaultLockedError, VaultPrivateKey
from joyread.infrastructure.filesystem.path_service import PathService
from tests.support.sealed_books import canonical_cbz, png_page, seal_file

PAGES = [png_page(index) for index in range(5)]
SIDECAR = b"<ComicInfo><Title>Private</Title></ComicInfo>" * 30


@pytest.fixture
def key() -> VaultPrivateKey:
    return VaultPrivateKey.generate()


@pytest.fixture
def keyring(key: VaultPrivateKey) -> VaultKeyring:
    ring = VaultKeyring()
    ring.unlock(key)
    return ring


@pytest.fixture
def book(tmp_path: Path, key: VaultPrivateKey) -> Path:
    return seal_file(
        tmp_path / "Books" / "ab" / "ab12.jrl",
        canonical_cbz(PAGES, sidecar=SIDECAR),
        key,
        ContentType.CBZ,
    )


def _service(tmp_path: Path, keyring: VaultKeyring) -> ArchiveImageService:
    temp_root = tmp_path / "session-temp"
    temp_root.mkdir(exist_ok=True)
    return ArchiveImageService(
        extraction_pool=ArchiveExtractionPool(tmp_path / "pool", max_bytes=1 << 30),
        session_temp_root=temp_root,
        extra_backends={".jrl": JrlArchiveBackend(keyring)},
    )


def _pool_entries(pool: Path) -> list[Path]:
    # The pool stamps its own schema marker when it is created; that is the
    # pool's bookkeeping, not page data.
    if not pool.exists():
        return []
    return [
        path
        for path in pool.rglob("*")
        if path.is_file() and path.name != ".joyread-archive-cache-schema"
    ]


def _nothing_written(tmp_path: Path) -> bool:
    return not _pool_entries(tmp_path / "pool") and not any((tmp_path / "session-temp").iterdir())


# -- registry -----------------------------------------------------------------


def test_the_registry_refuses_a_second_backend_for_one_suffix() -> None:
    registry = ArchiveBackendRegistry()
    registry.register((".cbz",), object())  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        registry.register((".CBZ",), object())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        registry.register(("jrl",), object())  # type: ignore[arg-type]


def test_only_an_explicit_true_makes_a_backend_confidential() -> None:
    registry = ArchiveBackendRegistry()
    registry.register((".a",), SimpleNamespace(confidential="yes"))  # type: ignore[arg-type]
    registry.register((".b",), SimpleNamespace(confidential=True))  # type: ignore[arg-type]

    assert not registry.is_confidential(".a")
    assert registry.is_confidential(".B")
    assert not registry.is_confidential(".unknown")


# -- reading ------------------------------------------------------------------


def test_a_sealed_book_reads_its_pages_byte_for_byte(
    tmp_path: Path, book: Path, keyring: VaultKeyring
) -> None:
    session = _service(tmp_path, keyring).open(book)
    try:
        assert session.page_count == len(PAGES)
        assert [page.image_bytes for page in session.read_pages(range(len(PAGES)))] == PAGES
        assert session.cache_plan.policy is ArchiveCachePolicy.DIRECT
        assert session.access_mode is ArchiveAccessMode.DIRECT
        assert session.allows_parallel_page_preparation(0)
    finally:
        session.close()


def test_a_deflated_sidecar_inflates_and_checks_its_crc(book: Path, keyring: VaultKeyring) -> None:
    index = SealedCbzIndex.open(keyring, book)
    try:
        data = index.read_member(
            "ComicInfo.xml",
            limits=ArchiveOpenLimits(),
            budget=ArchiveOperationBudget(None),
        )
    finally:
        index.close()
    assert data == SIDECAR


def test_two_threads_read_one_sealed_book_at_once(
    tmp_path: Path, book: Path, keyring: VaultKeyring
) -> None:
    session = _service(tmp_path, keyring).open(book)
    results: dict[int, list[bytes]] = {}
    barrier = threading.Barrier(2)

    def read(worker: int) -> None:
        barrier.wait()
        order = range(len(PAGES)) if worker == 0 else reversed(range(len(PAGES)))
        results[worker] = [session.read_pages((index,))[0].image_bytes for index in order]

    threads = [threading.Thread(target=read, args=(worker,)) for worker in (0, 1)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert results[0] == PAGES
        assert results[1] == list(reversed(PAGES))
    finally:
        session.close()


def test_closing_the_session_closes_the_sealed_reader(
    tmp_path: Path, book: Path, keyring: VaultKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[SealedCbzIndex] = []
    real_close = SealedCbzIndex.close

    def recording_close(self: SealedCbzIndex) -> None:
        closed.append(self)
        real_close(self)

    monkeypatch.setattr(SealedCbzIndex, "close", recording_close)
    session = _service(tmp_path, keyring).open(book)
    session.read_pages((0,))

    session.close()

    assert len(closed) == 1


# -- nothing reaches the disk -----------------------------------------------


def test_asking_for_the_persistent_cache_writes_nothing(
    tmp_path: Path, book: Path, keyring: VaultKeyring
) -> None:
    service = _service(tmp_path, keyring)

    session = service.open(book, document_cache_key="file:sealed", allow_persistent_cache=True)
    try:
        session.read_pages(range(len(PAGES)))
        assert session.cache_plan.policy is ArchiveCachePolicy.DIRECT
    finally:
        session.close()

    assert _nothing_written(tmp_path)


def test_a_cache_lease_offered_for_a_sealed_book_is_released_unused(
    tmp_path: Path, book: Path, keyring: VaultKeyring
) -> None:
    pool = ArchiveExtractionPool(tmp_path / "pool", max_bytes=1 << 30)
    service = ArchiveImageService(
        extraction_pool=pool,
        session_temp_root=tmp_path / "session-temp",
        extra_backends={".jrl": JrlArchiveBackend(keyring)},
    )
    (tmp_path / "session-temp").mkdir()
    lease = ArchiveCacheLease(pool, "file:sealed", ArchiveCacheScope.PERSISTENT)

    session = service.open(book, cache_lease=lease)
    try:
        session.read_pages(range(len(PAGES)))
    finally:
        session.close()

    assert lease.is_closed
    assert _nothing_written(tmp_path)


def test_a_session_built_by_hand_still_refuses_the_pool(tmp_path: Path) -> None:
    pool = ArchiveExtractionPool(tmp_path / "pool", max_bytes=1 << 30)
    lease = ArchiveCacheLease(pool, "file:hand-built", ArchiveCacheScope.PERSISTENT)
    source = ArchiveSource(label="x.jrl", suffix=".7z", path=tmp_path / "x.7z", confidential=True)
    record = PageRecord(display_path="x/001.png", source=source, name="001.png", password=None)

    session = ArchiveImageSession(
        [record],
        lambda _source, entries, _budget: {name: PAGES[0] for name, _password in entries},
        cache_lease=lease,
        bulk_extract=lambda *args, **kwargs: None,
    )
    try:
        assert lease.is_closed
        assert session.read_pages((0,))[0].image_bytes == PAGES[0]
        assert session.cache_plan.policy is not ArchiveCachePolicy.BULK_CONVERT
    finally:
        session.close()
    assert not _pool_entries(tmp_path / "pool")


def test_a_nested_archive_inside_a_sealed_book_is_skipped_not_spilled(
    tmp_path: Path, key: VaultPrivateKey, keyring: VaultKeyring
) -> None:
    nested = canonical_cbz([png_page(90)])
    book = seal_file(
        tmp_path / "nested.jrl", canonical_cbz(PAGES[:2], nested=nested), key, ContentType.CBZ
    )

    session = _service(tmp_path, keyring).open(book)
    try:
        assert session.page_count == 2
    finally:
        session.close()
    assert _nothing_written(tmp_path)


def test_sealed_books_are_never_probed_inspected_or_converted(
    tmp_path: Path, book: Path, keyring: VaultKeyring
) -> None:
    service = _service(tmp_path, keyring)

    assert service.probe_archive(book).code is ArchiveValidationCode.UNSUPPORTED_FORMAT
    inspection = service.inspect_for_import(book)
    assert not inspection.accepted
    assert inspection.rejection is ImportRejection.MALFORMED_ROOT
    with pytest.raises(ArchiveUnsupportedFormat):
        service.convert_to_canonical(book, tmp_path / "out.cbz")
    assert not (tmp_path / "out.cbz").exists()


# -- failures -------------------------------------------------------------------


def test_a_locked_vault_is_reported_as_locked(tmp_path: Path, book: Path) -> None:
    service = _service(tmp_path, VaultKeyring())

    with pytest.raises(ArchiveVaultLocked) as raised:
        service.open(book)
    assert isinstance(raised.value, VaultLockedError)


def test_locking_stops_an_open_session(tmp_path: Path, book: Path, keyring: VaultKeyring) -> None:
    session = _service(tmp_path, keyring).open(book)
    try:
        session.read_pages((0,))
        keyring.lock()
        with pytest.raises(ArchiveVaultLocked):
            session.read_pages((1,))
    finally:
        session.close()


def test_damage_fails_only_the_pages_it_touches(
    tmp_path: Path, key: VaultPrivateKey, keyring: VaultKeyring
) -> None:
    content = canonical_cbz(PAGES)
    book = seal_file(tmp_path / "damaged.jrl", content, key, ContentType.CBZ)
    # Flip one ciphertext byte inside the last page's data.
    last_page_offset = content.rindex(PAGES[-1])
    chunk = (last_page_offset + 200) // 1024
    raw = bytearray(book.read_bytes())
    raw[104 + chunk * (1024 + 16) + 5] ^= 0x01
    book.write_bytes(bytes(raw))

    session = _service(tmp_path, keyring).open(book)
    try:
        assert session.read_pages((0,))[0].image_bytes == PAGES[0]
        with pytest.raises(ArchiveCorruptError):
            session.read_pages((len(PAGES) - 1,))
    finally:
        session.close()


def test_a_book_sealed_to_another_vault_does_not_open(
    tmp_path: Path, keyring: VaultKeyring
) -> None:
    stranger = seal_file(
        tmp_path / "stranger.jrl", canonical_cbz(PAGES), VaultPrivateKey.generate(), ContentType.CBZ
    )

    with pytest.raises(ArchiveOpenError):
        _service(tmp_path, keyring).open(stranger)


def test_a_sealed_pdf_is_not_an_archive(tmp_path: Path, key: VaultPrivateKey, keyring: VaultKeyring) -> None:
    pdf = seal_file(tmp_path / "doc.jrl", b"%PDF-1.4 not really", key, ContentType.PDF)

    with pytest.raises(ArchiveUnsupportedFormat):
        _service(tmp_path, keyring).open(pdf)


# -- reader dispatch, runtime wiring and guards --------------------------------


def test_the_reader_opens_a_sealed_cbz_through_the_runtime_factories(
    tmp_path: Path, book: Path, keyring: VaultKeyring
) -> None:
    paths = PathService(base_dir=tmp_path / "library")
    pool = ArchiveExtractionPool(tmp_path / "pool", max_bytes=1 << 30)
    archive = create_archive_image_service(paths, pool, keyring)
    reader = create_reader_session_service(archive, None, None, keyring)  # type: ignore[arg-type]
    lease = ArchiveCacheLease(pool, "file:sealed", ArchiveCacheScope.PERSISTENT)

    session = reader.open_document(book, cache_lease=lease, allow_persistent_cache=False)
    try:
        assert lease.is_closed
        assert [page.image_bytes for page in session.read_pages(range(len(PAGES)))] == PAGES
    finally:
        session.close()


def test_a_reader_without_a_keyring_reports_hidden_space_locked(tmp_path: Path, book: Path) -> None:
    reader = ReaderSessionService(ArchiveImageService())

    with pytest.raises(VaultLockedError):
        reader.open_document(book)


def test_the_warmup_coordinator_never_queues_a_sealed_book(book: Path) -> None:
    submitted: list[object] = []
    tasks = SimpleNamespace(submit=lambda *args, **kwargs: submitted.append(args))
    coordinator = ArchiveWarmupCoordinator(ReaderSessionService(ArchiveImageService()), tasks)  # type: ignore[arg-type]

    coordinator.acquire(
        book,
        "client",
        on_ready=lambda: None,
        document_cache_key="file:sealed",
        allow_persistent_cache=True,
    )

    assert submitted == []
    assert coordinator.pending_library_tasks(book.parent) == 0
