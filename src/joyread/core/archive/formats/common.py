"""Shared bounded I/O and process handling for archive format backends."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
import subprocess
from threading import Thread
import time

from joyread.core.archive.errors import (
    ArchiveDependencyMissing,
    ArchivePasswordRejected,
    ArchiveReadError,
    ArchiveResourceLimitError,
)
from joyread.core.archive.limits import ArchiveOperationBudget, ensure_item_size


logger = logging.getLogger(__name__)


def looks_like_password_error(exc: Exception) -> bool:
    return looks_like_password_error_text(str(exc))


def looks_like_password_error_text(text: str) -> bool:
    normalized = text.lower()
    direct_markers = (
        "password",
        "encrypted",
        "bad decrypt",
        "wrong pass",
        "incorrect pass",
    )
    if any(marker in normalized for marker in direct_markers):
        return True
    if "data error" in normalized and ("encrypted" in normalized or "wrong" in normalized):
        return True
    return "crc failed" in normalized and ("password" in normalized or "encrypted" in normalized)


def read_stream_bounded(
    stream: object,
    subject: str,
    *,
    max_item_bytes: int | None,
    budget: ArchiveOperationBudget,
) -> bytes:
    """Read a file-like archive member without allocating past configured limits."""

    reader = getattr(stream, "read", None)
    if not callable(reader):
        raise ArchiveReadError(f"Archive backend returned a non-readable entry: {subject}")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = reader(64 * 1024)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            chunk = bytes(chunk)
        total += len(chunk)
        ensure_item_size(total, max_item_bytes, subject)
        budget.consume(len(chunk), subject)
        chunks.append(chunk)
    return b"".join(chunks)


def read_file_bounded(
    path: Path,
    subject: str,
    *,
    max_item_bytes: int | None,
    budget: ArchiveOperationBudget,
) -> bytes:
    try:
        declared_size = path.stat().st_size
    except OSError as exc:
        raise ArchiveReadError(f"Could not inspect extracted archive entry: {subject}") from exc
    ensure_item_size(declared_size, max_item_bytes, subject)
    with path.open("rb") as stream:
        return read_stream_bounded(stream, subject, max_item_bytes=max_item_bytes, budget=budget)


def run_archive_stdout_command(
    command: Sequence[str],
    entry_name: str,
    *,
    password: str | None = None,
    max_item_bytes: int | None,
    budget: ArchiveOperationBudget,
    timeout_seconds: int | None,
) -> bytes:
    """Run a managed extractor with bounded stdout and no command logging."""

    output: list[bytes] = []
    stderr: list[bytes] = []
    output_total = 0
    output_limit, limit_name = _remaining_output_limit(max_item_bytes, budget)
    exceeded = [False]

    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise ArchiveDependencyMissing(f"Could not start archive backend: {command[0]}") from exc

    def read_stdout() -> None:
        nonlocal output_total
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read(64 * 1024)
            if not chunk:
                return
            output_total += len(chunk)
            if output_limit is not None and output_total > output_limit:
                exceeded[0] = True
                process.kill()
                return
            output.append(chunk)

    def read_stderr() -> None:
        assert process.stderr is not None
        remaining = 64 * 1024
        while remaining > 0:
            chunk = process.stderr.read(min(64 * 1024, remaining))
            if not chunk:
                return
            stderr.append(chunk)
            remaining -= len(chunk)
        while process.stderr.read(64 * 1024):
            pass

    stdout_thread = Thread(target=read_stdout, daemon=True)
    stderr_thread = Thread(target=read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        stdout_thread.join()
        stderr_thread.join()
        raise ArchiveResourceLimitError(
            "external_command_timeout_seconds",
            maximum=timeout_seconds,
            subject=entry_name,
        ) from exc
    stdout_thread.join()
    stderr_thread.join()
    if exceeded[0]:
        raise ArchiveResourceLimitError(
            limit_name,
            actual=output_total,
            maximum=output_limit,
            subject=entry_name,
        )
    stderr_text = b"".join(stderr).decode("utf-8", errors="replace").strip()
    if returncode != 0:
        if password is not None and looks_like_password_error_text(stderr_text):
            raise ArchivePasswordRejected(f"Password rejected for archive entry: {entry_name}")
        logger.debug("Archive backend %s failed for entry=%s code=%d", command[0], entry_name, returncode)
        raise ArchiveReadError(f"Archive backend could not extract entry: {entry_name}")
    if not output:
        raise ArchiveReadError(f"Archive backend returned no data for entry: {entry_name}")
    payload = b"".join(output)
    budget.consume(len(payload), entry_name)
    return payload


def run_archive_file_command(
    command: Sequence[str],
    entry_name: str,
    *,
    password: str | None,
    timeout_seconds: int | None,
    output_directory: Path,
    max_output_bytes: int | None,
    budget: ArchiveOperationBudget,
) -> int:
    """Run a managed extractor and bound bytes written under its temp root."""

    stderr: list[bytes] = []
    output_limit, limit_name = _remaining_output_limit(max_output_bytes, budget)
    if output_limit is not None and output_limit <= 0:
        raise ArchiveResourceLimitError(
            limit_name,
            actual=budget.used + 1,
            maximum=budget.maximum if limit_name == "operation_bytes" else output_limit,
            subject=entry_name,
        )
    try:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError as exc:
        raise ArchiveDependencyMissing(f"Could not start archive backend: {command[0]}") from exc

    def read_stderr() -> None:
        assert process.stderr is not None
        remaining = 64 * 1024
        while True:
            chunk = process.stderr.read(64 * 1024)
            if not chunk:
                return
            if remaining > 0:
                stderr.append(chunk[:remaining])
                remaining -= len(chunk)

    stderr_thread = Thread(target=read_stderr, daemon=True)
    stderr_thread.start()
    started_at = time.monotonic()
    while True:
        wait_timeout = 0.05
        if timeout_seconds is not None:
            remaining = timeout_seconds - (time.monotonic() - started_at)
            if remaining <= 0:
                process.kill()
                process.wait()
                stderr_thread.join()
                raise ArchiveResourceLimitError(
                    "external_command_timeout_seconds",
                    maximum=timeout_seconds,
                    subject=entry_name,
                )
            wait_timeout = min(wait_timeout, remaining)
        try:
            returncode = process.wait(timeout=wait_timeout)
            break
        except subprocess.TimeoutExpired:
            output_total = _directory_output_bytes(output_directory, stop_after=output_limit)
            if output_limit is not None and output_total > output_limit:
                process.kill()
                process.wait()
                stderr_thread.join()
                raise ArchiveResourceLimitError(
                    limit_name,
                    actual=output_total,
                    maximum=output_limit,
                    subject=entry_name,
                )
    stderr_thread.join()
    output_total = _directory_output_bytes(output_directory, stop_after=output_limit)
    if output_limit is not None and output_total > output_limit:
        raise ArchiveResourceLimitError(
            limit_name,
            actual=output_total,
            maximum=output_limit,
            subject=entry_name,
        )
    if returncode != 0:
        stderr_text = b"".join(stderr).decode("utf-8", errors="replace").strip()
        if password is not None and looks_like_password_error_text(stderr_text):
            raise ArchivePasswordRejected(f"Password rejected for archive entry: {entry_name}")
        logger.debug("Archive backend %s failed for entry=%s code=%d", command[0], entry_name, returncode)
        raise ArchiveReadError(f"Archive backend could not extract entry: {entry_name}")
    return output_total


def _remaining_output_limit(
    max_item_bytes: int | None,
    budget: ArchiveOperationBudget,
) -> tuple[int | None, str]:
    if budget.maximum is None:
        return max_item_bytes, "extracted_item_bytes"
    remaining = max(0, budget.maximum - budget.used)
    if max_item_bytes is None or remaining < max_item_bytes:
        return remaining, "operation_bytes"
    return max_item_bytes, "extracted_item_bytes"


def _directory_output_bytes(directory: Path, *, stop_after: int | None) -> int:
    total = 0
    try:
        entries = directory.rglob("*")
        for entry in entries:
            if not entry.is_file() or entry.is_symlink():
                continue
            total += entry.stat().st_size
            if stop_after is not None and total > stop_after:
                return total
    except OSError as exc:
        raise ArchiveReadError("Could not inspect external archive extraction output.") from exc
    return total
