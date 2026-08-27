"""ZIP/CBZ listing and bounded entry reads."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from joyread.core.archive.backends import ExtractionBackendResolver
from joyread.core.archive.errors import (
    ArchiveCorruptError,
    ArchiveDependencyMissing,
    ArchiveOpenError,
    ArchivePasswordRejected,
    ArchiveReadError,
)
from joyread.core.archive.formats.common import (
    looks_like_password_error,
    read_stream_bounded,
    run_archive_stdout_command,
)
from joyread.core.archive.formats.seven_zip_command import (
    EXTRACT_STALL_SECONDS,
    background_thread_limit,
    extract_members_to_directory,
)
from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget
from joyread.core.archive.records import ArchiveContainerProbe, ArchiveEntry, ArchiveListing, ArchiveSource
from joyread.core.archive.scanner import ArchiveScanContext


# WinZip AES stores its parameters in an extra field with this header id. Its
# absence on an encrypted entry means the legacy ZipCrypto cipher.
_AES_EXTRA_HEADER_ID = 0x9901
_ENCRYPTED_FLAG = 0x1


def uses_zipcrypto(info: object) -> bool:
    """Whether this entry is encrypted with ZipCrypto rather than AES.

    The distinction is worth a central-directory walk because the two are not
    remotely comparable in cost. AES is handed to a C backend and decrypts at
    ~166 MB/s; ZipCrypto goes through ``zipfile._ZipDecrypter``, a per-byte
    Python loop holding the GIL, measured at ~2.6 MB/s. A single 2 MB page
    costs ~12 ms one way and ~1 s the other.
    """

    if not getattr(info, "flag_bits", 0) & _ENCRYPTED_FLAG:
        return False
    extra = getattr(info, "extra", b"") or b""
    offset = 0
    while offset + 4 <= len(extra):
        header_id = int.from_bytes(extra[offset : offset + 2], "little")
        if header_id == _AES_EXTRA_HEADER_ID:
            return False
        offset += 4 + int.from_bytes(extra[offset + 2 : offset + 4], "little")
    return True


class ZipArchiveBackend:
    """ZIP implementation isolated from service orchestration and caching."""

    def __init__(
        self,
        zipper_getter: Callable[[], object | None],
        bad_file_errors_getter: Callable[[], tuple[type[BaseException], ...]],
        request_password: Callable[..., str],
        backend_resolver: ExtractionBackendResolver | None = None,
    ) -> None:
        self._zipper_getter = zipper_getter
        self._bad_file_errors_getter = bad_file_errors_getter
        self._request_password = request_password
        self._backend_resolver = backend_resolver or ExtractionBackendResolver()

    def probe_entries(self, source: ArchiveSource) -> ArchiveContainerProbe:
        """List ZIP metadata without reading members or prompting for a key."""

        zipper = self._zipper_getter()
        if zipper is None:
            raise ArchiveDependencyMissing("pyzipper is required for ZIP/CBZ archives.")
        bad_file_errors = self._bad_file_errors_getter()
        try:
            with zipper.AESZipFile(source.open_arg(), "r") as archive:
                infos = archive.infolist()
                return ArchiveContainerProbe(
                    entries=tuple(
                        ArchiveEntry(info.filename, getattr(info, "file_size", None), None)
                        for info in infos
                        if not info.is_dir()
                    ),
                    is_encrypted=any(
                        not info.is_dir() and bool(info.flag_bits & 0x1)
                        for info in infos
                    ),
                )
        except bad_file_errors as exc:
            raise ArchiveCorruptError(f"Corrupt ZIP archive: {source.display_name}") from exc
        except OSError as exc:
            raise ArchiveOpenError(f"Could not open ZIP archive: {source.display_name}") from exc

    def list_entries(self, source: ArchiveSource, context: ArchiveScanContext) -> ArchiveListing:
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
                return ArchiveListing(
                    tuple(
                        ArchiveEntry(info.filename, getattr(info, "file_size", None), password)
                        for info in infos
                        if not info.is_dir()
                    )
                )
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
                helper: str | None = None
                helper_resolved = False
                for name, password in entries:
                    if password is not None and uses_zipcrypto(archive.getinfo(name)):
                        if not helper_resolved:
                            helper = self._zipcrypto_helper(source)
                            helper_resolved = True
                        if helper is not None:
                            payloads[name] = self._read_via_seven_zip(
                                helper,
                                source,
                                name,
                                password,
                                limits=limits,
                                budget=budget,
                            )
                            continue
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

    def _zipcrypto_helper(self, source: ArchiveSource) -> str | None:
        """The 7-Zip executable to decrypt ZipCrypto entries with, if usable.

        Needs a file on disk: the helper takes a path, not a stream. Nested
        archives only qualify because the scanner spills them (see
        ``ArchiveScanner._materialize_nested_source``); before that they were
        bytes in memory and every nested ZipCrypto page paid the Python loop.
        """

        if source.path is None:
            return None
        backend = self._backend_resolver.seven_zip()
        return backend.executable if backend is not None else None

    def _read_via_seven_zip(
        self,
        executable: str,
        source: ArchiveSource,
        name: str,
        password: str,
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> bytes:
        """Decrypt one ZipCrypto entry out of process, measured ~70x faster.

        Same argument discipline as the RAR backend: entry names come from
        archive metadata and are attacker controlled, so "-spd" stops a name
        being read as a wildcard and "--" stops it being read as a switch.
        "-scsUTF-8" keeps CJK names intact.
        """

        command = [executable, "x", "-so", "-y", "-spd", "-scsUTF-8"]
        # Known project limitation, shared with 7z and RAR: 7-Zip accepts a
        # password only as a command-line argument, so it is readable through
        # `ps` by any process running as this user for the length of the
        # extraction. AES entries never reach here and keep the in-process
        # guarantee -- they are already as fast as this path.
        command.append(f"-p{password}")
        command.extend(["--", str(source.path), name])
        try:
            return run_archive_stdout_command(
                command,
                name,
                password=password,
                max_item_bytes=limits.max_extracted_item_bytes,
                budget=budget,
                timeout_seconds=limits.external_command_timeout_seconds,
            )
        except ArchivePasswordRejected as exc:
            raise ArchivePasswordRejected(
                f"Password rejected for ZIP archive: {source.display_name}",
                archive_path=source.display_name,
            ) from exc

    def supports_bulk_extraction(self, source: ArchiveSource) -> bool:
        """Whether this source *can* be converted in one executable pass.

        Capability only, mirroring the 7Z backend. Whether conversion is
        *worth doing* is the session's cache-plan decision: a plain zip is
        DIRECT (cheap random access) and never converts; an encrypted one is
        expensive and does. Answering the policy question here required
        re-opening the archive and re-parsing the central directory the scan
        had just finished parsing, on every open.

        The reason encrypted zips convert at all is CPython, not the
        container: `zipfile._ZipDecrypter` is a pure-Python per-byte loop that
        measures 2.5 MB/s and holds the GIL, so it does not parallelise --
        four decrypting threads take four times as long as one, while starving
        the UI thread. `7zz` does the same work in another process, measured
        at 1.05 s against ~40 s for the same 100 MB archive.
        """

        if source.path is None:
            return False
        return self._backend_resolver.seven_zip() is not None

    def extract_members(
        self,
        source: ArchiveSource,
        members: Sequence[str],
        destination: Path,
        password: str | None,
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
        max_output_bytes: int | None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        """Extract exactly ``members`` into ``destination`` in one pass.

        Mirrors the 7Z backend: only the scanner's confirmed page members are
        requested, and the names travel in a listfile rather than argv so they
        can never be read as switches.
        """

        if source.path is None:
            raise ArchiveDependencyMissing("Bulk extraction requires a filesystem archive path.")
        backend = self._backend_resolver.seven_zip()
        if backend is None:
            raise ArchiveDependencyMissing("7zz/7z is not installed.")
        if not members:
            return

        extract_members_to_directory(
            backend.executable,
            source.path,
            members,
            destination,
            password,
            budget=budget,
            max_output_bytes=max_output_bytes,
            timeout_seconds=None,
            thread_limit=background_thread_limit(),
            stall_seconds=EXTRACT_STALL_SECONDS,
            is_cancelled=is_cancelled,
        )

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
