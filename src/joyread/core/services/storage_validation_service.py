"""Decides whether a JoyRead storage root is usable.

A library is "usable" when its SQLite database opens, passes an integrity
check, and carries a schema this build of JoyRead understands (current or
older-but-migratable). This is the single gate behind *Move*, *Select existing
library*, first-run reuse, and the lightweight daily-startup check.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
import logging
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from joyread.infrastructure.database import LATEST_SCHEMA_VERSION, apply_migrations
from joyread.infrastructure.database.sqlite_connection import open_sqlite_connection
from joyread.infrastructure.filesystem.path_service import StoragePathResolver, WritableLocation

# Imported at module level so a rename or removal produces an ImportError at
# startup rather than a misleading SMOKE_TEST_FAILED at validation time.
from joyread.core.repositories.sqlite_book_repository import _list_books as _repo_list_books


logger = logging.getLogger(__name__)


DATABASE_FILENAME = "joyread.sqlite3"

# Storage-root-internal directories a usable library must contain or be able to
# create. Config/Logs live in the external support root and are not part of a
# library, so they are deliberately excluded here.
REQUIRED_SUBDIRECTORIES: tuple[str, ...] = (
    WritableLocation.BOOKS,
    WritableLocation.DATABASE,
    WritableLocation.THUMBNAILS,
    WritableLocation.CACHE,
    WritableLocation.PLUGINS,
    WritableLocation.BACKUPS,
)

# Tables/columns that must exist after migrations for the app to function.
_REQUIRED_TABLES: tuple[str, ...] = (
    "schema_migrations",
    "book_files",
    "books",
    "progress",
    "collections",
    "reader_settings",
)
_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "book_files": ("storage_path",),
    "books": ("file_id", "cover_path"),
}


class StorageValidationCode(StrEnum):
    OK = "ok"
    NOT_READABLE = "not_readable"
    NOT_WRITABLE = "not_writable"
    DATABASE_MISSING = "database_missing"
    DATABASE_UNOPENABLE = "database_unopenable"
    INTEGRITY_FAILED = "integrity_failed"
    SCHEMA_MISSING = "schema_missing"
    SCHEMA_INCOMPLETE = "schema_incomplete"
    SCHEMA_TOO_NEW = "schema_too_new"
    SMOKE_TEST_FAILED = "smoke_test_failed"


@dataclass(frozen=True)
class StorageValidationResult:
    ok: bool
    code: StorageValidationCode
    message: str

    @classmethod
    def success(cls) -> StorageValidationResult:
        return cls(True, StorageValidationCode.OK, "")

    @classmethod
    def failure(cls, code: StorageValidationCode, message: str) -> StorageValidationResult:
        return cls(False, code, message)


class StorageValidationService:
    """Validates storage roots for the storage management flows."""

    def database_path(self, storage_root: Path) -> Path:
        return Path(storage_root) / WritableLocation.DATABASE / DATABASE_FILENAME

    def validate_full(self, storage_root: Path) -> StorageValidationResult:
        """Complete check used by Move (staging), Select, and first-run reuse.

        Confirms the root is readable/writable, opens the database, runs an
        integrity check, upgrades an older schema via ``apply_migrations``,
        verifies the key tables/columns, and runs a repository smoke query.
        Only once all of that passes are the managed subdirectories created.

        Rejecting a folder leaves it exactly as it was found. Creating the
        managed layout up front meant that pointing Select at any empty folder
        littered it with ``Books``/``Database``/``Thumbnails``/... on the way to
        telling the user it was not a library. Writability is still checked, but
        with a probe file that is always removed.
        """

        root = Path(storage_root)
        logger.debug("validate_full storage_root=%s", root)

        readable = self._check_readable(root)
        if readable is not None:
            return readable

        writable = self._check_writable(root)
        if writable is not None:
            return writable

        # Before the database is opened, because opening it applies migrations
        # and that rewrites the candidate. A root holding a *file* named
        # `Thumbnails` is rejected either way, but discovering it afterwards
        # meant a rejected Select had already upgraded a 0-byte database
        # through every migration -- a change to a library the user was only
        # asking about.
        conflict = self._check_layout_conflicts(root)
        if conflict is not None:
            return conflict

        database = self.database_path(root)
        if not database.is_file():
            return StorageValidationResult.failure(
                StorageValidationCode.DATABASE_MISSING,
                f"No JoyRead database found at {database}.",
            )

        try:
            connection = open_sqlite_connection(database)
        except sqlite3.Error as exc:
            return StorageValidationResult.failure(
                StorageValidationCode.DATABASE_UNOPENABLE,
                f"Could not open the JoyRead database: {exc}",
            )
        try:
            result = self._validate_open_database(connection, root)
        finally:
            connection.close()
        if not result.ok:
            return result
        # Accepted, so the library may now be completed in place. A valid
        # library from an older layout can legitimately be missing a directory.
        return self.ensure_managed_layout(root) or result

    def validate_lightweight(self, storage_root: Path) -> StorageValidationResult:
        """Cheap daily-startup check: root readable, database opens read-only,
        schema present and not newer than this build supports."""

        root = Path(storage_root)
        logger.debug("validate_lightweight storage_root=%s", root)

        readable = self._check_readable(root)
        if readable is not None:
            return readable

        database = self.database_path(root)
        if not database.is_file():
            return StorageValidationResult.failure(
                StorageValidationCode.DATABASE_MISSING,
                f"No JoyRead database found at {database}.",
            )

        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            return StorageValidationResult.failure(
                StorageValidationCode.DATABASE_UNOPENABLE,
                f"Could not open the JoyRead database: {exc}",
            )
        try:
            applied = self._max_applied_version(connection)
            if applied is None:
                return StorageValidationResult.failure(
                    StorageValidationCode.SCHEMA_MISSING,
                    "Database is missing the schema_migrations table.",
                )
            too_new = self._check_not_too_new(applied)
            if too_new is not None:
                return too_new
        except sqlite3.Error as exc:
            return StorageValidationResult.failure(
                StorageValidationCode.DATABASE_UNOPENABLE,
                f"Could not read the JoyRead database: {exc}",
            )
        finally:
            connection.close()
        return StorageValidationResult.success()

    # -- internal helpers ---------------------------------------------------

    def _check_readable(self, root: Path) -> StorageValidationResult | None:
        if not root.is_dir() or not os.access(root, os.R_OK):
            return StorageValidationResult.failure(
                StorageValidationCode.NOT_READABLE,
                f"Storage location is not a readable directory: {root}",
            )
        return None

    def _check_writable(self, root: Path) -> StorageValidationResult | None:
        """Prove the root is writable without leaving anything behind."""

        probe = root / f".joyread-write-test-{uuid4().hex}"
        try:
            probe.write_bytes(b"")
        except OSError as exc:
            return StorageValidationResult.failure(
                StorageValidationCode.NOT_WRITABLE,
                f"Storage location is not writable: {exc}",
            )
        finally:
            # Unconditional: a probe that was created must not survive a later
            # rejection, and one that was never created makes this a no-op.
            with suppress(OSError):
                probe.unlink()
        return None

    def _check_layout_conflicts(self, root: Path) -> StorageValidationResult | None:
        """Reject a root where a managed directory name is taken by a file.

        Pure inspection -- it creates nothing -- so it is safe to run before
        the database is opened, which is the point: the answer is known before
        anything about the candidate has been changed.
        """

        for name in REQUIRED_SUBDIRECTORIES:
            path = root / name
            try:
                if path.exists() and not path.is_dir():
                    return StorageValidationResult.failure(
                        StorageValidationCode.NOT_WRITABLE,
                        f"Storage location cannot be used: {name} is not a directory.",
                    )
            except OSError as exc:
                return StorageValidationResult.failure(
                    StorageValidationCode.NOT_WRITABLE,
                    f"Storage location is not usable: {exc}",
                )
        return None

    def ensure_managed_layout(self, root: Path) -> StorageValidationResult | None:
        """Create the managed subdirectories of an accepted library.

        Separate from validation on purpose: this writes, so it runs only once
        the root has been confirmed to be a JoyRead library.
        """

        try:
            for name in REQUIRED_SUBDIRECTORIES:
                (root / name).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return StorageValidationResult.failure(
                StorageValidationCode.NOT_WRITABLE,
                f"Storage location is not writable: {exc}",
            )
        return None

    def _validate_open_database(
        self,
        connection: sqlite3.Connection,
        root: Path,
    ) -> StorageValidationResult:
        try:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
        except sqlite3.Error as exc:
            return StorageValidationResult.failure(
                StorageValidationCode.INTEGRITY_FAILED,
                f"Database integrity check could not run: {exc}",
            )
        if integrity is None or str(integrity[0]).lower() != "ok":
            return StorageValidationResult.failure(
                StorageValidationCode.INTEGRITY_FAILED,
                f"Database failed PRAGMA quick_check: {integrity[0] if integrity else 'no result'}",
            )

        # Refuse to touch a database written by a newer build before migrating,
        # since migrations only move forward.
        applied = self._max_applied_version(connection)
        if applied is not None:
            too_new = self._check_not_too_new(applied)
            if too_new is not None:
                return too_new

        try:
            apply_migrations(connection)
        except sqlite3.Error as exc:
            return StorageValidationResult.failure(
                StorageValidationCode.SCHEMA_INCOMPLETE,
                f"Database migrations could not be applied: {exc}",
            )

        missing = self._missing_schema(connection)
        if missing is not None:
            return StorageValidationResult.failure(
                StorageValidationCode.SCHEMA_INCOMPLETE,
                missing,
            )

        smoke = self._smoke_test(connection, root)
        if smoke is not None:
            return smoke

        return StorageValidationResult.success()

    def _max_applied_version(self, connection: sqlite3.Connection) -> int | None:
        try:
            row = connection.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
        except sqlite3.Error:
            return None
        if row is None or row[0] is None:
            return None
        return int(row[0])

    def _check_not_too_new(self, applied: int) -> StorageValidationResult | None:
        if applied > LATEST_SCHEMA_VERSION:
            return StorageValidationResult.failure(
                StorageValidationCode.SCHEMA_TOO_NEW,
                (
                    f"Library was created by a newer version of JoyRead "
                    f"(schema v{applied} > supported v{LATEST_SCHEMA_VERSION})."
                ),
            )
        return None

    def _missing_schema(self, connection: sqlite3.Connection) -> str | None:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for table in _REQUIRED_TABLES:
            if table not in tables:
                return f"Database is missing the required table '{table}'."
        for table, columns in _REQUIRED_COLUMNS.items():
            existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for column in columns:
                if column not in existing:
                    return f"Database table '{table}' is missing the column '{column}'."
        return None

    def _smoke_test(
        self,
        connection: sqlite3.Connection,
        root: Path,
    ) -> StorageValidationResult | None:
        # Mirror the real shelf load so a structurally-valid but query-incompatible
        # database is caught here rather than at first render.
        #
        # The resolver is passed explicitly. Managed paths are stored relative
        # to the storage root, so without one the repository falls back to
        # returning them raw -- warning on every row that it is handing back a
        # path the caller would resolve against the process working directory.
        # We know the root here, so mirroring the shelf means resolving the way
        # the shelf does rather than tripping that fallback.
        try:
            _repo_list_books(connection, resolver=StoragePathResolver(root))
        except Exception as exc:  # noqa: BLE001 - any failure means the library is unusable.
            return StorageValidationResult.failure(
                StorageValidationCode.SMOKE_TEST_FAILED,
                f"Repository smoke test failed: {exc}",
            )
        return None
