"""ZIP/CBZ listing and bounded entry reads."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from joyread.core.archive.errors import (
    ArchiveCorruptError,
    ArchiveDependencyMissing,
    ArchiveOpenError,
    ArchivePasswordRejected,
    ArchiveReadError,
)
from joyread.core.archive.formats.common import looks_like_password_error, read_stream_bounded
from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget
from joyread.core.archive.records import ArchiveEntry, ArchiveSource
from joyread.core.archive.scanner import ArchiveScanContext


class ZipArchiveBackend:
    """ZIP implementation isolated from service orchestration and caching."""

    def __init__(
        self,
        zipper_getter: Callable[[], object | None],
        bad_file_errors_getter: Callable[[], tuple[type[BaseException], ...]],
        request_password: Callable[..., str],
    ) -> None:
        self._zipper_getter = zipper_getter
        self._bad_file_errors_getter = bad_file_errors_getter
        self._request_password = request_password

    def list_entries(self, source: ArchiveSource, context: ArchiveScanContext) -> list[ArchiveEntry]:
        zipper = self._zipper_getter()
        if zipper is None:
            raise ArchiveDependencyMissing("pyzipper is required for ZIP/CBZ archives.")
        bad_file_errors = self._bad_file_errors_getter()

        try:
            with zipper.AESZipFile(source.open_arg(), "r") as archive:
                infos = archive.infolist()
                encrypted = [info for info in infos if not info.is_dir() and info.flag_bits & 0x1]
                password = None
                if encrypted:
                    password = self._request_password(
                        source,
                        context,
                        reason="zip archive is encrypted",
                    )
                    self._verify_password(
                        source,
                        encrypted[0].filename,
                        password,
                        limits=context.limits,
                        budget=context.budget,
                    )
                return [
                    ArchiveEntry(info.filename, getattr(info, "file_size", None), password)
                    for info in infos
                    if not info.is_dir()
                ]
        except bad_file_errors as exc:
            raise ArchiveCorruptError(f"Corrupt ZIP archive: {source.display_name}") from exc
        except OSError as exc:
            raise ArchiveOpenError(f"Could not open ZIP archive: {source.display_name}") from exc

    def read_entries(
        self,
        source: ArchiveSource,
        entries: Sequence[tuple[str, str | None]],
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> dict[str, bytes]:
        zipper = self._zipper_getter()
        if zipper is None:
            raise ArchiveDependencyMissing("pyzipper is required for ZIP/CBZ archives.")
        bad_file_errors = self._bad_file_errors_getter()
        try:
            payloads: dict[str, bytes] = {}
            with zipper.AESZipFile(source.open_arg(), "r") as archive:
                for name, password in entries:
                    pwd = password.encode("utf-8") if password is not None else None
                    with archive.open(name, "r", pwd=pwd) as stream:
                        payloads[name] = read_stream_bounded(
                            stream,
                            name,
                            max_item_bytes=limits.max_extracted_item_bytes,
                            budget=budget,
                        )
            return payloads
        except RuntimeError as exc:
            if looks_like_password_error(exc):
                raise ArchivePasswordRejected(
                    f"Password rejected for archive: {source.display_name}",
                    archive_path=source.display_name,
                ) from exc
            raise ArchiveReadError(f"Could not read ZIP entry: {entries[0][0]}") from exc
        except (*bad_file_errors, KeyError) as exc:
            raise ArchiveReadError(f"Could not read ZIP entry: {entries[0][0]}") from exc

    def _verify_password(
        self,
        source: ArchiveSource,
        name: str,
        password: str | None,
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> None:
        payload = self.read_entries(source, ((name, password),), limits=limits, budget=budget).get(name)
        if payload is None:
            raise ArchiveReadError(f"ZIP entry was not extracted: {name}")
