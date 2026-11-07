"""A real Library with a Hidden Space vault, for service-level tests.

Everything is the production class except scrypt's cost, which uses the
smallest value the slot format accepts so tests stay fast.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import zipfile

from joyread.core.archive import ArchiveImageService
from joyread.core.archive.formats.jrl_backend import JrlArchiveBackend
from joyread.core.file_types import SEALED_EXTENSION
from joyread.core.models.import_policy import CanonicalImportPolicy
from joyread.core.repositories.sqlite_book_repository import SqliteBookRepository
from joyread.core.repositories.sqlite_sealing_repository import SqliteSealingRepository
from joyread.core.repositories.sqlite_vault_repository import SqliteVaultRepository
from joyread.core.services.archive_extraction_pool import ArchiveExtractionPool
from joyread.core.services.hash_service import HashService
from joyread.core.services.import_service import ImportService
from joyread.core.services.library_maintenance_service import (
    LibraryMaintenanceCoordinator,
    LibraryMaintenanceService,
)
from joyread.core.services.sealed_cover_store import SealedCoverStore
from joyread.core.services.sealing_service import SealingService
from joyread.core.services.vault_service import VaultService
from joyread.core.vault import ScryptCost, VaultKeyring
from joyread.infrastructure.database import DatabaseInterpreter, DatabasePriority, apply_migrations
from joyread.infrastructure.filesystem.path_service import PathService
from tests.support.sealed_books import png_page

FAST_SCRYPT = ScryptCost(n=1 << 14, r=8, p=1)
PASSPHRASE = "1234"
_EPOCH = (1980, 1, 1, 0, 0, 0)


@dataclass
class VaultLibrary:
    paths: PathService
    database: DatabaseInterpreter
    pool: ArchiveExtractionPool
    archive_service: ArchiveImageService
    hash_service: HashService
    coordinator: LibraryMaintenanceCoordinator
    importer: ImportService
    books: SqliteBookRepository
    keyring: VaultKeyring
    vault: VaultService
    vault_repository: SqliteVaultRepository
    sealing_repository: SqliteSealingRepository
    cover_store: SealedCoverStore
    maintenance: LibraryMaintenanceService
    sealing: SealingService
    invalidated: list[str]

    def create_vault(self, passphrase: str = PASSPHRASE) -> None:
        draft = self.vault.prepare(passphrase, passphrase)
        self.vault.create(draft, draft.recovery_code.formatted())

    def import_file(self, path: Path) -> str:
        result = self.importer.import_files([path])
        item = result.items[0]
        assert item.status == "imported", item
        assert item.book_id is not None
        return item.book_id

    def file_row(self, book_id: str) -> dict:
        def read(connection):  # noqa: ANN001, ANN202
            row = connection.execute(
                """
                SELECT books.is_hidden, books.cover_path, book_files.*
                FROM books JOIN book_files ON book_files.file_id = books.file_id
                WHERE books.book_id = ?
                """,
                (book_id,),
            ).fetchone()
            return dict(row) if row is not None else None

        return self.database.execute(read, DatabasePriority.HIGH)

    def stored_path(self, book_id: str) -> Path:
        return self.paths.resolver.to_storage_absolute(self.file_row(book_id)["storage_path"])

    def read_pages(self, book_id: str) -> list[bytes]:
        session = self.archive_service.open(self.stored_path(book_id))
        try:
            pages = session.read_pages(range(session.page_count))
            return [page.image_bytes for page in pages if page is not None]
        finally:
            session.close()

    def close(self) -> None:
        self.sealing.close()
        self.cover_store.close()
        self.database.close()


def build_vault_library(
    root: Path,
    *,
    canonical_import_policy: CanonicalImportPolicy = CanonicalImportPolicy.EXPENSIVE_AND_NESTED,
    pdf_service=None,  # noqa: ANN001
) -> VaultLibrary:
    paths = PathService(storage_root=root / "library", support_root=root / "support")
    paths.ensure_directories()
    database = DatabaseInterpreter(paths.paths.database / "joyread.sqlite3")
    database.execute(apply_migrations, DatabasePriority.CRITICAL)
    keyring = VaultKeyring()
    pool = ArchiveExtractionPool(paths.paths.cache / ".archive_zip_bundles", max_bytes=32 * 1024 * 1024)
    archive_service = ArchiveImageService(
        extraction_pool=pool,
        extra_backends={SEALED_EXTENSION: JrlArchiveBackend(keyring)},
    )
    hash_service = HashService()
    coordinator = LibraryMaintenanceCoordinator()
    importer = ImportService(
        paths,
        database,
        archive_service,
        hash_service,
        maintenance_coordinator=coordinator,
        canonical_import_policy=canonical_import_policy,
        pdf_service=pdf_service,
    )
    books = SqliteBookRepository(
        database,
        resolver=paths.resolver,
        managed_books_root=paths.paths.books,
        thumbnails_root=paths.paths.thumbnails,
    )
    invalidated: list[str] = []
    maintenance = LibraryMaintenanceService(
        paths,
        database,
        hash_service,
        archive_service,
        extraction_cache=pool,
        invalidate_file_cache=invalidated.append,
        coordinator=coordinator,
        pdf_service=pdf_service,
    )
    vault_repository = SqliteVaultRepository(database)
    vault = VaultService(vault_repository, keyring, scrypt_cost=FAST_SCRYPT)
    sealing_repository = SqliteSealingRepository(database)
    cover_store = SealedCoverStore(paths, keyring)
    sealing = SealingService(
        paths,
        vault,
        sealing_repository,
        archive_service,
        hash_service,
        cover_store,
        coordinator=coordinator,
        invalidate_artifacts=maintenance.invalidate_artifacts,
    )
    return VaultLibrary(
        paths=paths,
        database=database,
        pool=pool,
        archive_service=archive_service,
        hash_service=hash_service,
        coordinator=coordinator,
        importer=importer,
        books=books,
        keyring=keyring,
        vault=vault,
        vault_repository=vault_repository,
        sealing_repository=sealing_repository,
        cover_store=cover_store,
        maintenance=maintenance,
        sealing=sealing,
        invalidated=invalidated,
    )


def write_cbz(path: Path, pages: list[bytes], *, deflate: bool = True) -> Path:
    """A user's own CBZ: deflated, real timestamps -- not JoyRead's canonical layout."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED) as archive:
        for index, page in enumerate(pages):
            archive.writestr(f"page-{index + 1:02d}.png", page)
    return path


def sample_pages(count: int = 3) -> list[bytes]:
    return [png_page(index) for index in range(count)]


def png_bytes(size: tuple[int, int] = (20, 28)) -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", size, "teal").save(buffer, format="PNG")
    return buffer.getvalue()
