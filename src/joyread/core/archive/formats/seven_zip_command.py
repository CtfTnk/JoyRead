"""One fenced ``7zz`` invocation surface, shared by the 7z and RAR backends.

7-Zip reads both containers, so the command construction, option fencing,
staging, and thread policy belong in one place rather than being written twice
with two chances to forget ``--``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from joyread.core.archive.errors import (
    ArchiveBulkUnsupported,
    ArchiveDependencyMissing,
    ArchiveError,
    ArchiveReadError,
)
from joyread.core.archive.formats.common import read_file_bounded, run_archive_file_command
from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget


logger = logging.getLogger(__name__)

# A page turn must not sit behind a wedged subprocess, but a big archive that
# is still working deserves to finish. Liveness is progress -- staged bytes or
# backend progress output -- not elapsed time, so a long solid-archive
# decompression that has written nothing yet still counts as alive because
# "-bsp2" keeps reporting. Silence for this long means genuinely wedged.
EXTRACT_STALL_SECONDS = 30.0

#: Thread cap for background whole-document conversion.
#:
#: LZMA2 gives every decoder thread its own dictionary, so memory scales with
#: thread count while speed does not. Measured on a 2.20 GiB single-block
#: LZMA2:28 archive, extracting all 2.21 GiB of payload:
#:
#:     -mmt=1      21.3 s    0.25 GiB child peak
#:     -mmt=2      12.4 s    1.04 GiB
#:     -mmt=4       8.5 s    2.07 GiB
#:     default      3.5 s    4.56 GiB
#:
#: Unbounded threading buys 6x the speed for 18x the memory, and a background
#: conversion overlapping a foreground read then puts two such processes on the
#: machine at once. Background work is the side that should yield: it has no
#: user waiting on it, and the pages a reader actually wants are served by the
#: foreground path meanwhile.
BACKGROUND_THREAD_LIMIT = 2

#: Foreground reads keep 7-Zip's own default. They are what the user is waiting
#: for, and they extract a handful of members rather than a whole archive.
FOREGROUND_THREAD_LIMIT: int | None = None

BACKGROUND_THREADS_ENV_VAR = "JOYREAD_ARCHIVE_BACKGROUND_THREADS"
FOREGROUND_THREADS_ENV_VAR = "JOYREAD_ARCHIVE_FOREGROUND_THREADS"


class MemberNameNotRepresentable(ArchiveBulkUnsupported):
    """A member name cannot be expressed in a 7-Zip listfile."""


def _thread_limit_from_env(variable: str, default: int | None) -> int | None:
    """Read a thread cap override, so the harness can sweep it without a build.

    ``0`` means "7-Zip's own default", matching how the switch is omitted.
    """

    raw = os.environ.get(variable, "").strip()
    if not raw:
        return default
    try:
        threads = int(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r", variable, raw)
        return default
    return None if threads <= 0 else threads


def background_thread_limit() -> int | None:
    return _thread_limit_from_env(BACKGROUND_THREADS_ENV_VAR, BACKGROUND_THREAD_LIMIT)


def foreground_thread_limit() -> int | None:
    return _thread_limit_from_env(FOREGROUND_THREADS_ENV_VAR, FOREGROUND_THREAD_LIMIT)


def build_listfile_text(members: Sequence[str]) -> str:
    """Render members as listfile lines, one per line.

    A listfile is newline-delimited, so a member whose name contains CR or LF
    would silently become two entries and extract the wrong files. Such a name
    is refused rather than mangled; the caller treats it as "not bulk capable"
    and stays on the on-demand path.
    """

    for name in members:
        if not name:
            raise MemberNameNotRepresentable("empty member name")
        if "\n" in name or "\r" in name:
            raise MemberNameNotRepresentable("member name contains a line break")
    return "".join(f"{name}\n" for name in members)


def extract_members_to_directory(
    executable: str,
    archive_path: Path,
    members: Sequence[str],
    destination: Path,
    password: str | None,
    *,
    budget: ArchiveOperationBudget,
    max_output_bytes: int | None,
    timeout_seconds: int | None,
    thread_limit: int | None,
    stall_seconds: float | None = EXTRACT_STALL_SECONDS,
    is_cancelled: Callable[[], bool] | None = None,
    use_listfile: bool = True,
) -> int:
    """Extract exactly ``members`` into ``destination`` in one invocation.

    Member names come from archive metadata and are attacker controlled, so
    every path here is fenced the same way: ``-spd`` stops a name being read as
    a wildcard, ``--`` stops it being read as a switch (without it a member
    called ``-oESCAPED`` becomes a second output directory), and ``-scsUTF-8``
    keeps non-ASCII names intact.

    ``use_listfile`` moves the names out of argv entirely, which is required
    once a list can be long enough to approach ``ARG_MAX``. Short foreground
    batches pass them as arguments instead, so a name a listfile cannot express
    does not make a normal read fail.
    """

    destination.mkdir(parents=True, exist_ok=True)
    command = [executable, "x", "-y", "-bso0", "-bsp2", "-spd", "-scsUTF-8"]
    if password is not None:
        # Known and accepted exposure: this puts the plaintext password in the
        # child's command line, where `ps` and /proc/<pid>/cmdline expose it to
        # any process running as the same user for the length of the
        # extraction. 7-Zip has no stdin or environment alternative, so the
        # only way to avoid it is to stop using the executable for encrypted
        # archives. See "Known exposure" under Password Handling in
        # docs/ARCHIVE_CORE_HANDBOOK.md before changing this.
        command.append(f"-p{password}")
    if thread_limit is not None:
        command.append(f"-mmt={max(1, int(thread_limit))}")
    subject = members[0] if members else str(archive_path.name)

    def run(extra: Sequence[str], trailing: Sequence[str]) -> int:
        return run_archive_file_command(
            [*command, *extra, f"-o{destination}", "--", str(archive_path), *trailing],
            subject,
            password=password,
            timeout_seconds=timeout_seconds,
            output_directory=destination,
            max_output_bytes=max_output_bytes,
            budget=budget,
            # Every invocation here can contain multiple members, so this is
            # an aggregate ceiling rather than a per-member limit.
            output_limit_name="operation_bytes",
            stall_seconds=stall_seconds,
            is_cancelled=is_cancelled,
        )

    if not use_listfile:
        return run((), members)
    listfile_text = build_listfile_text(members)
    with TemporaryDirectory(prefix="joyread-7z-list-") as list_root:
        listfile = Path(list_root) / "members.txt"
        listfile.write_text(listfile_text, encoding="utf-8")
        return run((f"-i@{listfile}",), ())


def read_members_via_executable(
    executable: str,
    archive_path: Path,
    members: Sequence[str],
    password: str | None,
    *,
    limits: ArchiveOpenLimits,
    budget: ArchiveOperationBudget,
    thread_limit: int | None = None,
) -> dict[str, bytes] | None:
    """Read several members in one invocation, or ``None`` to fall back.

    Staging to a temporary directory rather than piping to stdout is what makes
    a multi-member read possible at all: ``-so`` concatenates payloads with no
    framing, so a caller cannot tell where one member ends.

    **This function owns the fallback decision, and callers must not widen it.**
    ``None`` is returned only for failures that happen before any staged byte
    has been read, which is also before the shared operation budget has been
    charged. Once reading starts, every failure propagates: a caller that
    retried through a slower backend would charge the same bytes to the budget
    a second time and could trip a limit that the real workload never reached.
    Password and resource-limit errors propagate from either phase -- a
    different backend would reject the same password, and a guardrail that
    fired once must not be walked past.
    """

    if not members:
        return {}
    # Cleanup errors are swallowed deliberately: they surface after the
    # payloads are already read and charged, where raising would turn a
    # successful read into a terminal failure.
    with TemporaryDirectory(prefix="joyread-7z-", ignore_cleanup_errors=True) as staging:
        staging_root = Path(staging)
        try:
            extract_members_to_directory(
                executable,
                archive_path,
                members,
                staging_root,
                password,
                budget=budget,
                # This command stages the whole requested batch. Individual
                # members are still checked by ``read_file_bounded`` below;
                # applying the per-member ceiling here would incorrectly cap
                # the aggregate of several otherwise valid pages.
                max_output_bytes=limits.max_operation_bytes,
                timeout_seconds=limits.external_command_timeout_seconds,
                thread_limit=(
                    foreground_thread_limit() if thread_limit is None else thread_limit
                ),
                use_listfile=False,
            )
            staged = resolve_staged_targets(staging_root, members)
        except (ArchiveDependencyMissing, ArchiveReadError) as exc:
            # The executable could not run or could not parse this container.
            # Nothing has been read, so an independent backend may still try.
            logger.warning(
                "7-Zip multi-member read failed before any byte was charged (%s)",
                type(exc).__name__,
            )
            return None
        except OSError as exc:
            logger.warning(
                "7-Zip staging failed before any byte was charged (%s)", type(exc).__name__
            )
            return None
        if staged is None:
            # 7-Zip exits 0 for members it did not produce, so a shortfall is
            # detected here -- still before anything is charged.
            return None
        try:
            return read_staged_payloads(staged, limits, budget)
        except ArchiveError:
            raise
        except OSError as exc:
            # Terminal on purpose: some members are already charged.
            raise ArchiveReadError(
                f"Could not read staged archive entries: {members[0]}"
            ) from exc


def ensure_staged_file_readable(path: Path) -> bool:
    """Make a staged file readable by us, returning whether it now is.

    7-Zip applies the mode stored in the container, but that mode describes the
    file the archive was built from, not the throwaway copy we just extracted
    into our own temporary directory. A member stored with no owner-read bit --
    which some writers, including ``py7zr.writestr``, produce -- therefore
    stages as a file the reader cannot open, and the page fails even though the
    decompression succeeded. We own the staging tree, so the mode is ours to
    correct rather than something to fall back over.

    Called before any byte is read, so a failure here is still a clean
    capability failure with nothing charged to the operation budget.
    """

    if os.access(path, os.R_OK):
        return True
    try:
        path.chmod(path.stat().st_mode | 0o400)
    except OSError:
        return False
    return os.access(path, os.R_OK)


def resolve_staged_targets(
    staging_root: Path,
    targets: Sequence[str],
) -> dict[str, Path] | None:
    """Locate every requested entry, or ``None`` if any is absent.

    7-Zip exits 0 even when a requested member does not exist, so success is
    decided here rather than by the return code. Resolution happens before any
    byte is read so that a shortfall is a clean fallback with nothing charged
    to the shared budget yet.
    """

    resolved_root = staging_root.resolve()
    staged: dict[str, Path] = {}
    for name in targets:
        try:
            resolved = (staging_root / name).resolve()
            # Entry names come from archive metadata, so refuse anything that
            # escapes the staging root rather than trusting the container.
            if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
                return None
            if not ensure_staged_file_readable(resolved):
                return None
        except OSError:
            return None
        staged[name] = resolved
    return staged


def read_staged_payloads(
    staged: dict[str, Path],
    limits: ArchiveOpenLimits,
    budget: ArchiveOperationBudget,
) -> dict[str, bytes]:
    """Read staged entries under the item limit and the shared budget.

    The executable writes to disk instead of through a budgeted writer, so the
    budget must be charged here. ``read_file_bounded`` checks the size from
    ``stat`` before allocating and consumes as it reads, rather than
    materialising a whole member and validating it afterwards.
    """

    return {
        name: read_file_bounded(
            path,
            name,
            max_item_bytes=limits.max_extracted_item_bytes,
            budget=budget,
        )
        for name, path in staged.items()
    }
