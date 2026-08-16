"""RAR/CBR reader with rarfile compatibility and managed external fallbacks."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import logging
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from threading import RLock

from joyread.core.archive.backends import ExtractionBackendResolver
from joyread.core.archive.errors import (
    ArchiveCorruptError,
    ArchiveDependencyMissing,
    ArchiveOpenError,
    ArchivePasswordRejected,
    ArchivePasswordRequired,
    ArchiveReadError,
    ArchiveUnsupportedFormat,
)
from joyread.core.archive.formats.common import (
    looks_like_password_error_text,
    read_file_bounded,
    read_stream_bounded,
    run_archive_file_command,
    run_archive_stdout_command,
)
from joyread.core.archive.formats.seven_zip_command import (
    EXTRACT_STALL_SECONDS,
    background_thread_limit,
    extract_members_to_directory,
    read_members_via_executable,
)
from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget, ensure_item_size
from joyread.core.archive.records import ArchiveContainerProbe, ArchiveEntry, ArchiveListing, ArchiveSource
from joyread.core.archive.scanner import ArchiveScanContext
from joyread.core.archive.tree import safe_entry_name
from joyread.core.diagnostics import reader_perf_enabled, reader_perf_event


logger = logging.getLogger(__name__)


def rar_requires_sequential_warmup(module: object, archive: object) -> bool:
    """Whether this RAR must be read forward rather than randomly.

    RAR records solidity per file: a member whose data continues the previous
    member's compression stream carries a solid bit, and the first member of a
    block never does. An archive is therefore solid when *any* member is
    marked, not when the first one is.

    RAR5 keeps the bit in ``file_compress_flags`` and RAR3 in ``flags``; the two
    fields have different layouts, so testing the wrong one would read an
    unrelated bit. When neither field is available the answer is unknown, and
    unknown is treated as sequential: reading a solid archive randomly is slow,
    while warming a non-solid one is merely unnecessary.
    """

    rar5_solid = getattr(module, "RAR5_COMPR_SOLID", 0x40)
    rar3_solid = getattr(module, "RAR_FILE_SOLID", 0x10)
    saw_flags = False
    try:
        infos = [info for info in archive.infolist() if not info.isdir()]
    except (AttributeError, OSError):
        return True
    for info in infos:
        rar5_flags = getattr(info, "file_compress_flags", None)
        if rar5_flags is not None:
            saw_flags = True
            if int(rar5_flags) & rar5_solid:
                return True
            continue
        rar3_flags = getattr(info, "flags", None)
        if rar3_flags is not None:
            saw_flags = True
            if int(rar3_flags) & rar3_solid:
                return True
    return not saw_flags


class RarArchiveBackend:
    """RAR implementation, including the compatibility fallback chain.

    ``rarfile`` can delegate to its own external tool and does not expose a
    cancellable process handle. The JoyRead-owned 7z, unar, and bsdtar paths
    below do expose one and therefore receive timeout and output budgets.
    """

    def __init__(
        self,
        module_getter: Callable[[], object | None],
        backend_resolver: ExtractionBackendResolver,
        lock: RLock,
        request_password: Callable[..., str],
    ) -> None:
        self._module_getter = module_getter
        self._backend_resolver = backend_resolver
        self._lock = lock
        self._request_password = request_password

    def probe_entries(self, source: ArchiveSource) -> ArchiveContainerProbe:
        """Read RAR headers only, without invoking password verification."""

        module = self._module_getter()
        if module is None or not hasattr(module, "RarFile"):
            raise ArchiveDependencyMissing("rarfile is required for RAR/CBR archives.")
        try:
            with self._lock:
                self._configure_rarfile_tools(module)
                with module.RarFile(source.open_arg(), "r") as archive:
                    encrypted = bool(archive.needs_password())
                    entries = () if encrypted else tuple(
                        ArchiveEntry(info.filename, getattr(info, "file_size", None), None)
                        for info in archive.infolist()
                        if not info.isdir()
                    )
                    return ArchiveContainerProbe(entries, is_encrypted=encrypted)
        except module.RarCannotExec as exc:
            raise ArchiveDependencyMissing(self._backend_resolver.missing_message()) from exc
        except module.NeedFirstVolume as exc:
            raise ArchiveUnsupportedFormat("Multi-volume RAR archives are not supported yet.") from exc
        except module.BadRarFile as exc:
            raise ArchiveCorruptError(f"Corrupt RAR archive: {source.display_name}") from exc
        except OSError as exc:
            raise ArchiveOpenError(f"Could not open RAR archive: {source.display_name}") from exc

    def list_entries(self, source: ArchiveSource, context: ArchiveScanContext) -> ArchiveListing:
        module = self._module_getter()
        if module is None or not hasattr(module, "RarFile"):
            raise ArchiveDependencyMissing("rarfile is required for RAR/CBR archives.")

        password = None
        try:
            # rarfile stores extractor paths in module globals. Keep both
            # configuration and the delegated operation under one lock.
            with self._lock:
                self._configure_rarfile_tools(module)
                with module.RarFile(source.open_arg(), "r") as archive:
                    if archive.needs_password():
                        password = self._request_password(
                            source,
                            context,
                            reason="rar archive is encrypted",
                        )
                        first_entry = next(
                            (info.filename for info in archive.infolist() if not info.isdir()),
                            None,
                        )
                        if first_entry is not None:
                            self._verify_password(
                                source,
                                first_entry,
                                password,
                                limits=context.limits,
                                budget=context.budget,
                            )
                    return ArchiveListing(
                        tuple(
                            ArchiveEntry(info.filename, getattr(info, "file_size", None), password)
                            for info in archive.infolist()
                            if not info.isdir()
                        ),
                        requires_sequential_warmup=rar_requires_sequential_warmup(
                            module, archive
                        ),
                    )
        except module.RarCannotExec as exc:
            raise ArchiveDependencyMissing(
                self._backend_resolver.missing_message(encrypted=password is not None)
            ) from exc
        except module.NeedFirstVolume as exc:
            raise ArchiveUnsupportedFormat("Multi-volume RAR archives are not supported yet.") from exc
        except module.BadRarFile as exc:
            raise ArchiveCorruptError(f"Corrupt RAR archive: {source.display_name}") from exc
        except OSError as exc:
            raise ArchiveOpenError(f"Could not open RAR archive: {source.display_name}") from exc

    def read_entry(
        self,
        source: ArchiveSource,
        name: str,
        password: str | None,
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> bytes:
        module = self._module_getter()
        rar_failure: Exception | None = None
        if module is not None and hasattr(module, "RarFile"):
            try:
                with self._lock:
                    self._configure_rarfile_tools(module)
                    self._ensure_rar_backend(module)
                    with module.RarFile(source.open_arg(), "r") as archive:
                        opener = getattr(archive, "open", None)
                        if callable(opener):
                            with opener(name, pwd=password) as stream:
                                return read_stream_bounded(
                                    stream,
                                    name,
                                    max_item_bytes=limits.max_extracted_item_bytes,
                                    budget=budget,
                                )
                        payload = archive.read(name, pwd=password)
                        ensure_item_size(len(payload), limits.max_extracted_item_bytes, name)
                        budget.consume(len(payload), name)
                        return payload
            except module.PasswordRequired as exc:
                raise ArchivePasswordRequired(
                    f"Password required for RAR archive: {source.display_name}",
                    archive_path=source.display_name,
                ) from exc
            except module.RarWrongPassword as exc:
                raise ArchivePasswordRejected(
                    f"Password rejected for RAR archive: {source.display_name}",
                    archive_path=source.display_name,
                ) from exc
            except (module.RarCannotExec, module.BadRarFile, OSError) as exc:
                if (
                    password is not None
                    and isinstance(exc, module.BadRarFile)
                    and (source.path is None or looks_like_password_error_text(str(exc)))
                ):
                    raise ArchivePasswordRejected(
                        f"Password rejected for RAR archive: {source.display_name}",
                        archive_path=source.display_name,
                    ) from exc
                rar_failure = exc

        try:
            return self._read_external(source, name, password, limits=limits, budget=budget)
        except ArchiveDependencyMissing:
            if rar_failure is not None:
                raise ArchiveDependencyMissing(
                    self._backend_resolver.missing_message(encrypted=password is not None)
                ) from rar_failure
            raise
        except ArchiveReadError:
            if rar_failure is not None:
                raise ArchiveReadError(f"Could not read RAR entry with any backend: {name}") from rar_failure
            raise

    def read_entries(
        self,
        source: ArchiveSource,
        entries: Sequence[tuple[str, str | None]],
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> dict[str, bytes]:
        if not entries:
            return {}
        targets = [name for name, _password in entries]
        passwords = {password for _name, password in entries}
        if len(targets) > 1 and len(passwords) == 1:
            # One invocation for the whole batch. The per-entry chain below
            # launches an extractor per page, and on a solid RAR each launch
            # re-decompresses its own prefix, so a batch of eight cost eight
            # prefixes rather than one.
            payloads = self._read_many_with_7zip(
                source,
                targets,
                passwords.pop(),
                limits=limits,
                budget=budget,
            )
            if payloads is not None:
                return payloads
        return {
            name: self.read_entry(source, name, password, limits=limits, budget=budget)
            for name, password in entries
        }

    def supports_bulk_extraction(self, source: ArchiveSource) -> bool:
        """Whether this source can be converted in one executable pass.

        7-Zip reads RAR3 and RAR5, so the same whole-document conversion the 7z
        backend uses applies here. ``rarfile`` is not an alternative: it exposes
        no cancellable process handle and no output budget.
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
        """Extract exactly ``members`` into ``destination`` in one pass."""

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
            # Background conversion has no wall-clock ceiling; the stall
            # watchdog, written-bytes bound and cancellation are the controls.
            timeout_seconds=None,
            thread_limit=background_thread_limit(),
            stall_seconds=EXTRACT_STALL_SECONDS,
            is_cancelled=is_cancelled,
        )

    def _read_many_with_7zip(
        self,
        source: ArchiveSource,
        targets: Sequence[str],
        password: str | None,
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> dict[str, bytes] | None:
        """Read a whole batch in one invocation, or ``None`` to fall back."""

        if source.path is None:
            return None
        backend = self._backend_resolver.seven_zip()
        if backend is None:
            return None
        # No exception handling here on purpose. `read_members_via_executable`
        # owns the fallback boundary: it returns None only while nothing has
        # been charged to the shared operation budget, and raises once reading
        # has begun. Catching those raises would send the per-entry chain over
        # the same members and charge them to the budget twice.
        payloads = read_members_via_executable(
            backend.executable,
            source.path,
            targets,
            password,
            limits=limits,
            budget=budget,
        )
        if payloads is None:
            logger.warning(
                "7-Zip could not serve this RAR batch; falling back to per-entry reads"
            )
            return None
        if reader_perf_enabled():
            reader_perf_event(
                "archive.rar.extract",
                reader="7zip",
                targets=len(targets),
                buffered_bytes=sum(len(value) for value in payloads.values()),
            )
        return payloads

    def _verify_password(
        self,
        source: ArchiveSource,
        name: str,
        password: str | None,
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> None:
        self.read_entry(source, name, password, limits=limits, budget=budget)

    def _ensure_rar_backend(self, module: object) -> None:
        self._configure_rarfile_tools(module)
        try:
            module.tool_setup()
        except module.RarCannotExec as exc:
            raise ArchiveDependencyMissing(self._backend_resolver.missing_message()) from exc

    def _configure_rarfile_tools(self, module: object) -> None:
        seven_zip = self._backend_resolver.seven_zip()
        if seven_zip is not None:
            if hasattr(module, "SEVENZIP_TOOL"):
                module.SEVENZIP_TOOL = seven_zip.executable
            if hasattr(module, "SEVENZIP2_TOOL"):
                module.SEVENZIP2_TOOL = seven_zip.executable
        unar = self._backend_resolver.unar()
        if unar is not None and hasattr(module, "UNAR_TOOL"):
            module.UNAR_TOOL = unar.executable
        bsdtar = self._backend_resolver.bsdtar()
        if bsdtar is not None and hasattr(module, "BSDTAR_TOOL"):
            module.BSDTAR_TOOL = bsdtar.executable

    def _read_external(
        self,
        source: ArchiveSource,
        name: str,
        password: str | None,
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> bytes:
        dependency_errors: list[str] = []
        read_errors: list[str] = []
        for reader in (self._read_with_7zip, self._read_with_unar, self._read_with_bsdtar):
            try:
                return reader(source, name, password, limits=limits, budget=budget)
            except ArchiveDependencyMissing as exc:
                dependency_errors.append(str(exc))
            except ArchiveReadError as exc:
                read_errors.append(str(exc))
        if read_errors:
            raise ArchiveReadError("; ".join(read_errors))
        message = "; ".join(dependency_errors)
        raise ArchiveDependencyMissing(
            message or self._backend_resolver.missing_message(encrypted=password is not None)
        )

    def _read_with_bsdtar(
        self,
        source: ArchiveSource,
        name: str,
        password: str | None,
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> bytes:
        if source.path is None:
            raise ArchiveDependencyMissing("bsdtar fallback requires a filesystem archive path.")
        if password is not None:
            raise ArchiveDependencyMissing("bsdtar password-protected RAR fallback is not enabled.")
        backend = self._backend_resolver.bsdtar()
        if backend is None:
            raise ArchiveDependencyMissing("bsdtar is not installed.")
        return run_archive_stdout_command(
            [backend.executable, "-xOf", str(source.path), name],
            name,
            max_item_bytes=limits.max_extracted_item_bytes,
            budget=budget,
            timeout_seconds=limits.external_command_timeout_seconds,
        )

    def _read_with_7zip(
        self,
        source: ArchiveSource,
        name: str,
        password: str | None,
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> bytes:
        if source.path is None:
            raise ArchiveDependencyMissing("7z fallback requires a filesystem archive path.")
        backend = self._backend_resolver.seven_zip()
        if backend is None:
            raise ArchiveDependencyMissing("7zz/7z is not installed.")
        # Entry names come from archive metadata and are attacker controlled.
        # "-spd" stops a name being read as a wildcard and "--" stops it being
        # read as a switch; without the latter a member called "-oESCAPED"
        # becomes a second output directory. "-scsUTF-8" keeps CJK names intact.
        command = [backend.executable, "x", "-so", "-y", "-spd", "-scsUTF-8"]
        if password is not None:
            # Known and accepted exposure: the plaintext password lands in the
            # child's command line, readable through `ps` or
            # /proc/<pid>/cmdline by any process running as the same user. See
            # "Known exposure" under Password Handling in
            # docs/ARCHIVE_CORE_HANDBOOK.md.
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
                f"Password rejected for RAR archive: {source.display_name}",
                archive_path=source.display_name,
            ) from exc

    def _read_with_unar(
        self,
        source: ArchiveSource,
        name: str,
        password: str | None,
        *,
        limits: ArchiveOpenLimits,
        budget: ArchiveOperationBudget,
    ) -> bytes:
        if source.path is None:
            raise ArchiveDependencyMissing("unar fallback requires a filesystem archive path.")
        backend = self._backend_resolver.unar()
        if backend is None:
            raise ArchiveDependencyMissing("unar is not installed.")
        if safe_entry_name(name) != name:
            raise ArchiveReadError(f"unar received unsafe archive entry: {name}")
        with TemporaryDirectory(prefix="joyread-rar-") as temp_dir:
            output_root = Path(temp_dir)
            command = [backend.executable, "-quiet", "-force-overwrite", "-output-directory", temp_dir]
            if password is not None:
                command.extend(["-password", password])
            command.extend([str(source.path), name])
            try:
                output_bytes = run_archive_file_command(
                    command,
                    name,
                    password=password,
                    timeout_seconds=limits.external_command_timeout_seconds,
                    output_directory=output_root,
                    max_output_bytes=limits.max_extracted_item_bytes,
                    budget=budget,
                )
            except ArchivePasswordRejected as exc:
                raise ArchivePasswordRejected(
                    f"Password rejected for RAR archive: {source.display_name}",
                    archive_path=source.display_name,
                ) from exc
            extracted = output_root / PurePosixPath(name)
            if (
                not extracted.exists()
                or not extracted.is_file()
                or extracted.is_symlink()
                or not extracted.resolve().is_relative_to(output_root.resolve())
            ):
                raise ArchiveReadError(f"unar did not produce expected entry: {name}")
            expected_size = extracted.stat().st_size
            if output_bytes < expected_size:
                raise ArchiveReadError(f"unar output accounting was incomplete for entry: {name}")
            extra_output = output_bytes - expected_size
            if extra_output:
                budget.consume(extra_output, name)
            return read_file_bounded(
                extracted,
                name,
                max_item_bytes=limits.max_extracted_item_bytes,
                budget=budget,
            )
