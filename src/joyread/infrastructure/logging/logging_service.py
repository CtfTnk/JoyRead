"""Production logging runtime for JoyRead.

Callers keep using the standard :mod:`logging` API. This module owns the
cross-cutting concerns around those calls: asynchronous file I/O, correlation
metadata, privacy filtering, one file per launch with crash detection (see
``run_files``), exception hooks, and bounded shutdown.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import re
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from PySide6 import QtCore

from joyread import __version__
from joyread.core.operation_context import (
    OperationContext,
    bind_operation,
    create_operation,
    current_operation,
)
from joyread.core.operation_logging import (
    describe_callback,
    get_logger,
    log_event,
    log_timed_block,
    operation_scope,
)
from joyread.infrastructure.logging.log_format import (
    date_line,
    format_record,
    header_lines,
)
from joyread.infrastructure.logging.run_files import (
    EMERGENCY_LOG_GLOB,
    RUN_LOG_BACKUP_COUNT,
    RUN_LOG_MAX_BYTES,
    RunningMarker,
    prune_runs,
    recover_interrupted_runs,
    run_log_path,
    run_stem,
    running_marker_path,
)


LOG_QUEUE_CAPACITY = 8192
LOG_ENV_VAR = "JOYREAD_LOG_LEVEL"
LOG_BUILD_ENV_VAR = "JOYREAD_BUILD_ID"

_EARLY_RECORD_CAPACITY = 2048
_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 2.0

# Third-party DEBUG output is noisy enough to hide JoyRead's own diagnostics.
_QUIET_LIBRARIES = ("PIL", "urllib3", "asyncio", "matplotlib")
_SENSITIVE_KEY_PARTS = ("password", "passwd", "secret", "token", "credential")

_PASSWORD_ASSIGNMENT_RE = re.compile(
    r"(?i)(password|passwd|pwd|secret|token)\s*([=:])\s*([^\s,;]+)"
)
# A password flag with a real separator (space, "=", or the quote-comma-space
# a Python list repr uses between adjacent argv tokens) is unambiguous: no
# other flag spells out "password", and no one writes "-p <value>" to mean a
# flag named "-pages"/"-profile"/etc. Always redact this form -- it covers
# `-p <value>`, `-password <value>`, `--password=<value>`, and the quoted
# `'-password', 'hunter2'` shape `subprocess.TimeoutExpired`'s str() produces
# for the unrar external fallback's `["-password", password]` argv.
_PASSWORD_ARGUMENT_SEPARATED_RE = re.compile(
    r"(?<![\w])-p(?:assword)?(?:['\"]?,?\s+['\"]?|=)[^\s'\",<\]]+"
)
# The glued form (`-p<value>`, no separator -- 7-Zip's own `-p{password}`
# convention) is genuinely ambiguous: "-pages"/"-profile"/"-port" are the
# same shape and are legitimate flag names worth keeping for crash
# diagnosis, not passwords. Gate it on the log text actually mentioning one
# of the archive tools this app invokes with a glued password -- true in
# every real leak path (the executable is `command[0]`, so it appears
# alongside the flag whenever the whole argv surfaces, e.g. via
# `subprocess.TimeoutExpired`), and false for unrelated diagnostic text.
_ARCHIVE_TOOL_HINT_RE = re.compile(r"(?i)\b(?:7z|7zz|unar|bsdtar)\b")
_PASSWORD_ARGUMENT_GLUED_RE = re.compile(r"(?<![\w])-p(?!assword)[^\s'\",=<\]]+")
# An argv kept as a structured list field splits `-password` and its value
# into separate items, so the text patterns above never see them together.
_PASSWORD_FLAG_TOKENS = frozenset({"-p", "-password", "--password"})


@dataclass(frozen=True, slots=True)
class LoggingMetadata:
    run_id: str
    app_version: str
    build_id: str | None
    started_at_utc: str


@dataclass(frozen=True, slots=True)
class LoggingConfigurationResult:
    file_logging_enabled: bool
    log_file: Path | None
    error: str | None = None
    #: Earlier launches found to have ended without a clean quit.
    crashed_runs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LoggingShutdownResult:
    drained: bool
    pending_records: int
    dropped_records: int


class _CredentialRedactor:
    """Remove credentials from log output; leave everything else as written.

    The log stays on the user's machine and the user decides whether to share
    it, so filesystem paths and book names are recorded as they are: a
    support log that cannot name the file that failed is not worth sending.
    Credentials are different -- no troubleshooting needs a password, so they
    are removed from every message, field, traceback and argv unconditionally.
    """

    def redact_text(self, value: object) -> str:
        text = str(value)
        text = _PASSWORD_ASSIGNMENT_RE.sub(r"\1\2<redacted>", text)
        text = _PASSWORD_ARGUMENT_SEPARATED_RE.sub("-p<redacted>", text)
        if _ARCHIVE_TOOL_HINT_RE.search(text):
            text = _PASSWORD_ARGUMENT_GLUED_RE.sub("-p<redacted>", text)
        return text

    def safe_field(self, key: str, value: object) -> object:
        if any(part in key.casefold() for part in _SENSITIVE_KEY_PARTS):
            return "<redacted>"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, (tuple, list)):
            return self._safe_sequence(key, value)
        return self.redact_text(value)

    def _safe_sequence(self, key: str, items: tuple[object, ...] | list[object]) -> list[object]:
        safe: list[object] = []
        redact_next = False
        for item in items:
            if redact_next:
                safe.append("<redacted>")
                redact_next = False
                continue
            redact_next = isinstance(item, str) and item.casefold() in _PASSWORD_FLAG_TOKENS
            safe.append(self.safe_field(key, item))
        return safe


class _ContextFilter(logging.Filter):
    def __init__(self, metadata: LoggingMetadata) -> None:
        super().__init__()
        self._metadata = metadata

    def filter(self, record: logging.LogRecord) -> bool:
        operation = current_operation()
        record.run_id = self._metadata.run_id
        record.run_started_at_utc = self._metadata.started_at_utc
        record.app_version = self._metadata.app_version
        record.build_id = self._metadata.build_id
        if operation is not None:
            record.operation_id = getattr(record, "operation_id", operation.operation_id)
            record.parent_operation_id = getattr(
                record,
                "parent_operation_id",
                operation.parent_operation_id,
            )
            record.category = getattr(record, "category", operation.category)
            record.operation_name = getattr(record, "operation_name", operation.name)
        else:
            record.operation_id = getattr(record, "operation_id", None)
            record.parent_operation_id = getattr(record, "parent_operation_id", None)
            record.category = getattr(record, "category", _category_from_logger(record.name))
            record.operation_name = getattr(record, "operation_name", None)
        record.event = getattr(record, "event", "log.message")
        record.status = getattr(record, "status", None)
        return True


class _ReadableFormatter(logging.Formatter):
    """Lay records out as ``log_format`` describes, credentials removed.

    Fields JoyRead attaches to every record of a launch (run id, pid, version)
    are left out: the file header says them once. With ``mark_days`` a
    ``# YYYY-MM-DD`` line precedes the first record of each new local day, so
    the time-only lines of a launch that runs past midnight stay unambiguous.
    """

    def __init__(self, redactor: _CredentialRedactor, *, started_at: datetime | None = None,
                 mark_days: bool = False) -> None:
        super().__init__()
        self._redactor = redactor
        self._mark_days = mark_days
        self._started_day = (started_at or datetime.now(timezone.utc)).astimezone().date()
        self._last_day = self._started_day
        # RotatingFileHandler formats each record twice (size check, then
        # write); the day mark must survive into the second, written, copy.
        # One tuple, swapped whole, so concurrent stderr producers never pair
        # one record with another's text.
        self._last: tuple[logging.LogRecord | None, str] = (None, "")

    def restart_day_marks(self) -> None:
        """A new file begins with a header naming the start day; count from there."""

        self._last_day = self._started_day

    def format(self, record: logging.LogRecord) -> str:
        last_record, last_text = self._last
        if record is last_record:
            return last_text
        text = self._format(record)
        self._last = (record, text)
        return text

    def _format(self, record: logging.LogRecord) -> str:
        moment = datetime.fromtimestamp(record.created).astimezone()
        fields: list[tuple[str, object]] = []
        status = getattr(record, "status", None)
        if status is not None:
            fields.append(("status", status))
        operation_name = getattr(record, "operation_name", None)
        operation_id = getattr(record, "operation_id", None)
        if operation_name is not None or operation_id is not None:
            short_id = (operation_id or "")[:8]
            fields.append(("op", f"{operation_name or '?'}#{short_id}" if short_id else operation_name))
        parent_id = getattr(record, "parent_operation_id", None)
        if parent_id:
            fields.append(("parent", parent_id[:8]))
        for key, value in record.__dict__.items():
            if key in _LOG_RECORD_RESERVED_FIELDS or key.startswith("_") or value is None:
                continue
            fields.append((key, self._redactor.safe_field(key, value)))
        detail_parts: list[str] = []
        if record.exc_info:
            if getattr(record, "error_type", None) is None:
                fields.append(("error_type", record.exc_info[0].__name__))
            detail_parts.append(self.formatException(record.exc_info))
        if record.stack_info:
            detail_parts.append(self.formatStack(record.stack_info))
        line = format_record(
            moment=moment,
            level=record.levelname,
            category=str(getattr(record, "category", None) or _category_from_logger(record.name)),
            message=self._redactor.redact_text(record.getMessage()),
            event=getattr(record, "event", None),
            fields=fields,
            location=f"{record.name.removeprefix('joyread.')}:{record.lineno}",
            thread=None if record.threadName == "MainThread" else record.threadName,
            detail=self._redactor.redact_text("\n".join(detail_parts)) if detail_parts else None,
        )
        if self._mark_days and moment.date() != self._last_day:
            self._last_day = moment.date()
            line = f"{date_line(moment.date())}\n{line}"
        return line


_LOG_RECORD_RESERVED_FIELDS = frozenset(
    logging.LogRecord(
        name="",
        level=0,
        pathname="",
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    ).__dict__
) | {
    "message",
    "asctime",
    "run_id",
    "run_started_at_utc",
    "app_version",
    "build_id",
    "operation_id",
    "parent_operation_id",
    "category",
    "operation_name",
    "event",
    "status",
}


class _SecureRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """The launch's file: private, and every new or emptied file gets the header."""

    def __init__(self, *args, header: list[str] | None = None, **kwargs) -> None:  # noqa: ANN002, ANN003
        self._header = list(header or [])
        super().__init__(*args, **kwargs)

    def _open(self):  # noqa: ANN202 - stdlib override.
        stream = super()._open()
        if os.name == "posix":
            try:
                os.chmod(self.baseFilename, 0o600)
            except OSError as exc:
                _emergency_write(f"Unable to restrict log-file permissions: {exc}")
        try:
            if stream.tell() == 0:
                self._write_header(stream)
        except OSError as exc:
            _emergency_write(f"Unable to write the log-file header: {exc}")
        return stream

    def restart(self) -> None:
        """Empty the open file and start it again with the header. Hold the lock."""

        if self.stream is None:
            return
        self.stream.seek(0)
        self.stream.truncate()
        self._write_header(self.stream)

    def _write_header(self, stream) -> None:  # noqa: ANN001 - text stream.
        if self._header:
            stream.write("\n".join(self._header) + "\n")
            stream.flush()
        formatter = getattr(self, "formatter", None)
        if isinstance(formatter, _ReadableFormatter):
            formatter.restart_day_marks()


class _EarlyBufferHandler(logging.Handler):
    def __init__(self, capacity: int) -> None:
        super().__init__()
        self.capacity = max(1, capacity)
        self.records: list[logging.LogRecord] = []
        self.dropped = 0
        self._records_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        with self._records_lock:
            if len(self.records) >= self.capacity:
                self.records.pop(0)
                self.dropped += 1
            self.records.append(logging.makeLogRecord(record.__dict__.copy()))

    def take(self) -> tuple[list[logging.LogRecord], int]:
        with self._records_lock:
            records = self.records
            dropped = self.dropped
            self.records = []
            self.dropped = 0
        return records, dropped


class _AsyncDispatchHandler(logging.Handler):
    def __init__(self, runtime: "_AsyncLogRuntime", level: int) -> None:
        super().__init__(level)
        self._runtime = runtime

    def emit(self, record: logging.LogRecord) -> None:
        self._runtime.enqueue(logging.makeLogRecord(record.__dict__.copy()))


class _AsyncLogRuntime:
    def __init__(self, handlers: tuple[logging.Handler, ...], *, capacity: int) -> None:
        self.handlers = handlers
        self.queue: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=max(1, capacity))
        self._stopping = threading.Event()
        self._drop_lock = threading.Lock()
        self._dropped = 0
        self._close_lock = threading.Lock()
        self._handlers_closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="JoyReadLogWriter",
            daemon=True,
        )

    @property
    def dropped_records(self) -> int:
        with self._drop_lock:
            return self._dropped

    def start(self) -> None:
        self._thread.start()

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            with self._drop_lock:
                self._dropped += 1

    def replay_to_file(self, records: list[logging.LogRecord], dropped: int) -> None:
        file_handlers = [handler for handler in self.handlers if isinstance(handler, logging.FileHandler)]
        for record in records:
            for handler in file_handlers:
                if record.levelno >= handler.level:
                    handler.handle(record)
        if dropped:
            record = logging.LogRecord(
                name=__name__,
                level=logging.WARNING,
                pathname=__file__,
                lineno=0,
                msg="Early logging buffer dropped %d record(s)",
                args=(dropped,),
                exc_info=None,
            )
            record.event = "logging.early_records_dropped"
            record.category = "process"
            record.count = dropped
            for handler in file_handlers:
                handler.handle(record)

    def stop(self, timeout_seconds: float) -> LoggingShutdownResult:
        self._stopping.set()
        self._thread.join(max(0.0, timeout_seconds))
        drained = not self._thread.is_alive()
        pending = self.queue.qsize()
        dropped = self.dropped_records
        if drained:
            self._close_handlers()
        return LoggingShutdownResult(drained, pending, dropped)

    def wait_until_empty(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return self.queue.unfinished_tasks == 0

    def _run(self) -> None:
        try:
            reported_drops = 0
            while not self._stopping.is_set() or not self.queue.empty():
                try:
                    record = self.queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    for handler in self.handlers:
                        if record.levelno >= handler.level:
                            handler.handle(record)
                    dropped = self.dropped_records
                    if dropped > reported_drops:
                        self._write_drop_notice(dropped - reported_drops, dropped)
                        reported_drops = dropped
                except Exception as exc:  # Never let one malformed record kill the writer.
                    _emergency_write(f"Logging writer failed to emit a record: {exc}")
                finally:
                    self.queue.task_done()
        finally:
            # A bounded caller may stop waiting before a slow handler returns.
            # The writer still owns those handlers and closes them when it
            # eventually exits instead of leaking the rotating file handle.
            self._close_handlers()

    def _close_handlers(self) -> None:
        with self._close_lock:
            if self._handlers_closed:
                return
            self._handlers_closed = True
            for handler in self.handlers:
                try:
                    handler.flush()
                    handler.close()
                except Exception as exc:  # Logging teardown has no logger beneath it.
                    _emergency_write(f"Unable to close logging handler: {exc}")

    def _write_drop_notice(self, newly_dropped: int, total_dropped: int) -> None:
        record = logging.LogRecord(
            name=__name__,
            level=logging.WARNING,
            pathname=__file__,
            lineno=0,
            msg="Logging queue dropped %d record(s); total=%d",
            args=(newly_dropped, total_dropped),
            exc_info=None,
        )
        record.event = "logging.queue_records_dropped"
        record.category = "process"
        record.count = newly_dropped
        record.outcome = "degraded"
        for handler in self.handlers:
            if record.levelno >= handler.level:
                handler.handle(record)


_metadata: LoggingMetadata | None = None
_runtime: _AsyncLogRuntime | None = None
_early_buffer: _EarlyBufferHandler | None = None
_runtime_lock = threading.RLock()
# The configured log directory and the level the process started at. The
# Diagnostics switch raises the level to DEBUG for this run only; turning it
# off returns to ``_base_level`` (INFO, or what ``JOYREAD_LOG_LEVEL`` chose).
_logs_dir: Path | None = None
_base_level = logging.INFO
_detailed_logging = False
# This launch's file stem, its running marker, and earlier launches found to
# have crashed that the UI has not offered to report yet.
_run_stem: str | None = None
_running_marker: RunningMarker | None = None
_unreported_crashes: tuple[str, ...] = ()
_hooks_installed = False
_qt_handler_installed = False
_previous_qt_message_handler = None
_previous_sys_excepthook = None
_previous_threading_excepthook = None
_previous_unraisablehook = None


def configure_early_logging() -> LoggingMetadata:
    """Install stderr plus a bounded in-memory buffer before paths are known."""

    global _early_buffer, _metadata
    with _runtime_lock:
        _stop_runtime_without_log()
        _metadata = _new_metadata()
        redactor = _CredentialRedactor()
        context_filter = _ContextFilter(_metadata)
        formatter = _ReadableFormatter(redactor)
        level = _resolve_level()[0]
        buffer_handler = _EarlyBufferHandler(_EARLY_RECORD_CAPACITY)
        buffer_handler.addFilter(context_filter)
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(level)
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(context_filter)
        _reset_root_handlers()
        root = logging.getLogger()
        root.setLevel(level)
        root.addHandler(buffer_handler)
        root.addHandler(stream_handler)
        _early_buffer = buffer_handler
        _configure_library_levels()
        _install_exception_hooks()
        _install_qt_message_handler()
        return _metadata


def configure_logging(
    logs_dir: Path,
    *,
    app_version: str | None = None,
    build_id: str | None = None,
) -> LoggingConfigurationResult:
    """Start the asynchronous production logger and replay early records."""

    global _base_level, _detailed_logging, _early_buffer, _logs_dir, _metadata, _runtime
    global _run_stem, _running_marker, _unreported_crashes
    with _runtime_lock:
        early_records, early_dropped = _early_buffer.take() if _early_buffer is not None else ([], 0)
        _stop_runtime_without_log()
        current = _metadata or _new_metadata()
        _metadata = LoggingMetadata(
            run_id=current.run_id,
            app_version=app_version or current.app_version,
            build_id=build_id if build_id is not None else current.build_id,
            started_at_utc=current.started_at_utc,
        )
        level, unknown_value = _resolve_level()
        _base_level = level
        _detailed_logging = False
        _logs_dir = logs_dir
        redactor = _CredentialRedactor()
        handlers: list[logging.Handler] = []
        log_file: Path | None = None
        setup_error: str | None = None
        crashed: tuple[str, ...] = ()
        pruned = 0
        # Reconfiguring within one launch keeps writing the same file.
        stem = run_stem(_started_at(_metadata), os.getpid())
        _run_stem = stem
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            crashed = recover_interrupted_runs(logs_dir, stem)
            pruned = prune_runs(logs_dir, stem)
            log_file = run_log_path(logs_dir, stem)
            started_at = _started_at(_metadata)
            file_handler = _SecureRotatingFileHandler(
                log_file,
                maxBytes=RUN_LOG_MAX_BYTES,
                backupCount=RUN_LOG_BACKUP_COUNT,
                encoding="utf-8",
                header=header_lines(
                    app_version=_metadata.app_version,
                    build_id=_metadata.build_id,
                    run_id=_metadata.run_id,
                    pid=os.getpid(),
                    started_at=started_at,
                ),
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(
                _ReadableFormatter(redactor, started_at=started_at, mark_days=True)
            )
            handlers.append(file_handler)
        except Exception as exc:
            log_file = None
            setup_error = f"{type(exc).__name__}: {exc}"
            _emergency_write(f"Unable to initialize JoyRead file logging: {setup_error}")
        if log_file is not None:
            try:
                _running_marker = RunningMarker(running_marker_path(logs_dir, stem))
            except OSError as exc:
                _emergency_write(f"Unable to create the crash marker: {exc}")
        if crashed:
            _unreported_crashes = (*_unreported_crashes, *crashed)

        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(level)
        stream_handler.setFormatter(_ReadableFormatter(redactor))
        handlers.append(stream_handler)

        runtime = _AsyncLogRuntime(tuple(handlers), capacity=LOG_QUEUE_CAPACITY)
        runtime.start()
        dispatch_handler = _AsyncDispatchHandler(runtime, level)
        dispatch_handler.addFilter(_ContextFilter(_metadata))
        _reset_root_handlers()
        root = logging.getLogger()
        root.setLevel(level)
        root.addHandler(dispatch_handler)
        _runtime = runtime
        _early_buffer = None
        _configure_library_levels()
        _install_exception_hooks()
        _install_qt_message_handler()
        runtime.replay_to_file(early_records, early_dropped)

        log_event(
            logging.getLogger(__name__),
            logging.INFO,
            "logging.runtime.started",
            "Production logging runtime started",
            category="process",
            outcome="file" if log_file is not None else "stderr_only",
        )
        if unknown_value is not None:
            log_event(
                logging.getLogger(__name__),
                logging.WARNING,
                "logging.level.invalid",
                "Unknown logging level; falling back to INFO",
                category="settings",
                outcome="fallback",
            )
        if crashed:
            log_event(
                logging.getLogger(__name__),
                logging.WARNING,
                "logging.runs.crash_detected",
                "Earlier launches ended without a clean quit",
                category="process",
                outcome="crash_detected",
                crashed_runs=list(crashed),
            )
        if pruned:
            log_event(
                logging.getLogger(__name__),
                logging.INFO,
                "logging.runs.pruned",
                "Logs of older launches deleted",
                category="process",
                removed_count=pruned,
            )
        return LoggingConfigurationResult(log_file is not None, log_file, setup_error, crashed)


def flush_logging(timeout_seconds: float = 1.0) -> bool:
    """Wait for already-queued records without stopping the runtime."""

    runtime = _runtime
    return True if runtime is None else runtime.wait_until_empty(timeout_seconds)


def shutdown_logging(
    timeout_seconds: float = _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    *,
    unexpected: str | None = None,
) -> LoggingShutdownResult:
    """Seal producers, drain queued records, and close handlers once.

    A normal quit also removes this launch's running marker. Pass
    ``unexpected`` -- a short description -- when the process is exiting
    because something failed: the marker is kept with that note, and the next
    launch reports this one as a crash.
    """

    global _detailed_logging, _early_buffer, _runtime
    with _runtime_lock:
        _detailed_logging = False
        runtime = _runtime
        if runtime is None:
            _reset_root_handlers()
            _early_buffer = None
            _restore_exception_hooks()
            _close_running_marker(unexpected)
            return LoggingShutdownResult(True, 0, 0)
        log_event(
            logging.getLogger(__name__),
            logging.INFO,
            "logging.runtime.stopping",
            "Production logging runtime stopping",
            category="process",
        )
        _reset_root_handlers()
        _runtime = None
        result = runtime.stop(timeout_seconds)
        if not result.drained:
            _emergency_write(
                f"Logging shutdown timed out with {result.pending_records} queued record(s)"
            )
        _restore_exception_hooks()
        _close_running_marker(unexpected)
        return result


def log_directory() -> Path | None:
    """The directory the primary process writes logs to, once configured."""

    return _logs_dir


def current_run_stem() -> str | None:
    """The name this launch's log files share, once file logging is configured."""

    return _run_stem


def has_unreported_crashes() -> bool:
    return bool(_unreported_crashes)


def take_unreported_crashes() -> tuple[str, ...]:
    """Crashed launches found at startup, handed out once to offer a report."""

    global _unreported_crashes
    with _runtime_lock:
        crashes = _unreported_crashes
        _unreported_crashes = ()
        return crashes


def detailed_logging_enabled() -> bool:
    return _detailed_logging


def set_detailed_logging(enabled: bool) -> bool:
    """Switch DEBUG output on or off for the rest of this run.

    This is the in-app replacement for ``JOYREAD_LOG_LEVEL=DEBUG``, which a
    user who launches from Finder, Start or a desktop launcher cannot set. It
    is never persisted: the next launch starts at the base level again, so a
    forgotten switch cannot leave the log permanently verbose. Returns whether
    detailed logging is now on.
    """

    global _detailed_logging
    with _runtime_lock:
        if enabled == _detailed_logging:
            return _detailed_logging
        level = logging.DEBUG if enabled else _base_level
        if not enabled:
            # Record the change before the level drops, so it is not filtered.
            _log_level_change(enabled)
        # The level is enforced where records are admitted: the root logger and
        # the dispatch handler in front of the queue. The writer's own handlers
        # only ever widen, because they filter again at write time and would
        # otherwise drop DEBUG records still queued when the switch goes off.
        root = logging.getLogger()
        root.setLevel(level)
        for handler in root.handlers:
            handler.setLevel(level)
        if enabled and _runtime is not None:
            for handler in _runtime.handlers:
                handler.setLevel(min(handler.level, level))
        _detailed_logging = enabled
        if enabled:
            _log_level_change(enabled)
        return _detailed_logging


def _log_level_change(enabled: bool) -> None:
    log_event(
        logging.getLogger(__name__),
        logging.INFO,
        "logging.level.changed",
        "Detailed logging turned on" if enabled else "Detailed logging turned off",
        category="settings",
        level_name="DEBUG" if enabled else logging.getLevelName(_base_level),
        outcome="enabled" if enabled else "disabled",
    )


@contextmanager
def hold_log_files(timeout_seconds: float = 1.0) -> Iterator[logging.FileHandler | None]:
    """Keep the log files still while a caller reads or clears them.

    Queued records are written first, then the rotating handler's lock is held:
    the writer thread cannot append or rotate until the block exits, so the
    set of files and their contents stay consistent. Producers are unaffected
    -- they only enqueue -- and records queued meanwhile are written after.
    Yields the active file handler, or ``None`` when no file is attached.
    """

    with _runtime_lock:
        flush_logging(timeout_seconds)
        runtime = _runtime
        handler = None
        if runtime is not None:
            handler = next(
                (item for item in runtime.handlers if isinstance(item, logging.FileHandler)),
                None,
            )
        if handler is None:
            yield None
            return
        handler.acquire()
        try:
            if handler.stream is not None:
                handler.stream.flush()
            yield handler
        finally:
            handler.release()


def write_emergency_log(
    logs_dir: Path,
    event: str,
    message: str,
    *,
    error: BaseException | None = None,
) -> Path | None:
    """Write one exclusive startup-failure file without sharing rotation state.

    This path is reserved for failures that occur before a process has safely
    become the primary instance. A secondary must never attach a rotating
    handler to the primary's live file.
    """

    metadata = _metadata or _new_metadata()
    redactor = _CredentialRedactor()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = logs_dir / f"joyread-emergency-{timestamp}-{os.getpid()}-{uuid4().hex[:8]}.jsonl"
    payload: dict[str, object] = {
        "timestamp_utc": _utc_timestamp(time.time()),
        "level": "CRITICAL",
        "category": "process",
        "event": event,
        "message": redactor.redact_text(message),
        "run_id": metadata.run_id,
        "run_started_at_utc": metadata.started_at_utc,
        "operation_id": None,
        "parent_operation_id": None,
        "operation_name": None,
        "pid": os.getpid(),
        "thread": threading.current_thread().name,
        "logger": __name__,
        "source": "bootstrap",
        "app_version": metadata.app_version,
        "build_id": metadata.build_id,
        "status": "failed",
    }
    if error is not None:
        payload["error_type"] = type(error).__name__
        payload["exception"] = redactor.redact_text(
            "".join(traceback.format_exception(type(error), error, error.__traceback__))
        )
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")
        if os.name == "posix":
            os.chmod(path, 0o600)
        return path
    except Exception as exc:
        _emergency_write(f"Unable to persist startup failure: {exc}; original: {message}")
        return None


# log_event, operation_scope, get_logger, describe_callback, and
# log_timed_block now live in joyread.core.operation_logging (Qt-free, so
# Core services can use them without importing Infrastructure) and are
# imported above for re-export -- this module's public surface is unchanged.


def _new_metadata() -> LoggingMetadata:
    return LoggingMetadata(
        run_id=uuid4().hex,
        app_version=__version__,
        build_id=os.environ.get(LOG_BUILD_ENV_VAR) or None,
        started_at_utc=_utc_timestamp(time.time()),
    )


def _resolve_level() -> tuple[int, str | None]:
    raw = os.environ.get(LOG_ENV_VAR)
    if not raw:
        return logging.INFO, None
    candidate = raw.strip().upper()
    resolved = logging.getLevelName(candidate)
    if isinstance(resolved, int):
        return resolved, None
    return logging.INFO, raw


def _configure_library_levels() -> None:
    for name in _QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)


def _category_from_logger(name: str) -> str:
    parts = name.split(".")
    if len(parts) >= 3:
        return parts[2]
    return parts[-1]


def _utc_timestamp(created: float) -> str:
    return datetime.fromtimestamp(created, timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _reset_root_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception as exc:
            _emergency_write(f"Unable to close replaced logging handler: {exc}")


def _stop_runtime_without_log() -> None:
    global _runtime
    if _runtime is None:
        return
    _reset_root_handlers()
    result = _runtime.stop(_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS)
    if not result.drained:
        _emergency_write("Previous logging runtime did not drain during reconfiguration")
    _runtime = None
    _close_running_marker(None)


def _close_running_marker(unexpected: str | None) -> None:
    global _running_marker
    marker = _running_marker
    _running_marker = None
    if marker is not None:
        marker.close(unexpected=unexpected)


def _started_at(metadata: LoggingMetadata) -> datetime:
    try:
        return datetime.fromisoformat(metadata.started_at_utc.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _install_qt_message_handler() -> None:
    global _previous_qt_message_handler, _qt_handler_installed
    if _qt_handler_installed:
        return
    qt_logger = logging.getLogger("joyread.qt")

    def handler(msg_type, context, message):  # noqa: ANN001 - Qt callback signature.
        level = _QT_LEVEL_MAP.get(msg_type, logging.INFO)
        location = ""
        if context is not None and getattr(context, "file", None):
            location = f" ({context.file}:{context.line})"
        log_event(
            qt_logger,
            level,
            "qt.message",
            f"{message}{location}",
            category="qt",
        )

    _previous_qt_message_handler = QtCore.qInstallMessageHandler(handler)
    _qt_handler_installed = True


_QT_LEVEL_MAP = {
    QtCore.QtMsgType.QtDebugMsg: logging.DEBUG,
    QtCore.QtMsgType.QtInfoMsg: logging.INFO,
    QtCore.QtMsgType.QtWarningMsg: logging.WARNING,
    QtCore.QtMsgType.QtCriticalMsg: logging.ERROR,
    QtCore.QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def _install_exception_hooks() -> None:
    global _hooks_installed
    global _previous_sys_excepthook
    global _previous_threading_excepthook
    global _previous_unraisablehook
    if _hooks_installed:
        return
    _previous_sys_excepthook = sys.excepthook
    _previous_threading_excepthook = threading.excepthook
    _previous_unraisablehook = sys.unraisablehook

    def sys_hook(exc_type, exc_value, exc_traceback):  # noqa: ANN001
        if issubclass(exc_type, KeyboardInterrupt):
            if _previous_sys_excepthook is not None:
                _previous_sys_excepthook(exc_type, exc_value, exc_traceback)
            return
        log_event(
            logging.getLogger("joyread.unhandled"),
            logging.CRITICAL,
            "process.unhandled_exception",
            "Unhandled exception reached the process boundary",
            category="process",
            status="failed",
            error_type=exc_type.__name__,
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        # Main-thread failure can terminate Python before the daemon writer gets
        # another scheduling turn. Bound the wait, then preserve any debugger or
        # host hook that was installed before JoyRead.
        flush_logging(_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS)
        if _previous_sys_excepthook is not None:
            _previous_sys_excepthook(exc_type, exc_value, exc_traceback)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        log_event(
            logging.getLogger("joyread.unhandled"),
            logging.CRITICAL,
            "thread.unhandled_exception",
            "Unhandled exception reached a thread boundary",
            category="task",
            status="failed",
            error_type=args.exc_type.__name__,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        if _previous_threading_excepthook is not None:
            _previous_threading_excepthook(args)

    def unraisable_hook(args):  # noqa: ANN001
        exc = args.exc_value
        log_event(
            logging.getLogger("joyread.unhandled"),
            logging.ERROR,
            "process.unraisable_exception",
            "Unraisable exception reported by Python",
            category="process",
            status="failed",
            error_type=type(exc).__name__ if exc is not None else None,
            exc_info=(type(exc), exc, args.exc_traceback) if exc is not None else None,
        )
        if _previous_unraisablehook is not None:
            _previous_unraisablehook(args)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook
    sys.unraisablehook = unraisable_hook
    _hooks_installed = True


def _restore_exception_hooks() -> None:
    global _hooks_installed
    global _previous_sys_excepthook
    global _previous_threading_excepthook
    global _previous_unraisablehook
    global _previous_qt_message_handler
    global _qt_handler_installed
    if not _hooks_installed:
        return
    if _previous_sys_excepthook is not None:
        sys.excepthook = _previous_sys_excepthook
    if _previous_threading_excepthook is not None:
        threading.excepthook = _previous_threading_excepthook
    if _previous_unraisablehook is not None:
        sys.unraisablehook = _previous_unraisablehook
    _previous_sys_excepthook = None
    _previous_threading_excepthook = None
    _previous_unraisablehook = None
    _hooks_installed = False
    if _qt_handler_installed:
        QtCore.qInstallMessageHandler(_previous_qt_message_handler)
        _previous_qt_message_handler = None
        _qt_handler_installed = False


def _emergency_write(message: str) -> None:
    try:
        stream = sys.__stderr__ or sys.stderr
        stream.write(f"JoyRead logging: {message}\n")
        stream.flush()
    except Exception:
        # There is no lower diagnostic channel left at this point.
        pass


__all__ = [
    "LOG_BUILD_ENV_VAR",
    "LOG_ENV_VAR",
    "LOG_QUEUE_CAPACITY",
    "LoggingConfigurationResult",
    "LoggingMetadata",
    "LoggingShutdownResult",
    "EMERGENCY_LOG_GLOB",
    "configure_early_logging",
    "configure_logging",
    "current_run_stem",
    "describe_callback",
    "detailed_logging_enabled",
    "flush_logging",
    "get_logger",
    "has_unreported_crashes",
    "hold_log_files",
    "log_directory",
    "log_event",
    "log_timed_block",
    "operation_scope",
    "set_detailed_logging",
    "shutdown_logging",
    "take_unreported_crashes",
    "write_emergency_log",
]
