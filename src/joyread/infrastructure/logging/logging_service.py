"""Production logging runtime for JoyRead.

Callers keep using the standard :mod:`logging` API. This module owns the
cross-cutting concerns around those calls: asynchronous file I/O, correlation
metadata, privacy filtering, rotation, exception hooks, and bounded shutdown.
"""

from __future__ import annotations

import hashlib
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


LOG_FILE_NAME = "joyread.log"
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 5
LOG_QUEUE_CAPACITY = 8192
LOG_ENV_VAR = "JOYREAD_LOG_LEVEL"
LOG_SENSITIVE_ENV_VAR = "JOYREAD_LOG_SENSITIVE"
LOG_BUILD_ENV_VAR = "JOYREAD_BUILD_ID"

_TEXT_FORMAT = (
    "%(asctime)s.%(msecs)03dZ %(levelname)-7s [%(process)d:%(threadName)s] "
    "%(name)s:%(lineno)d %(message)s"
)
_TEXT_DATEFMT = "%Y-%m-%dT%H:%M:%S"
_EARLY_RECORD_CAPACITY = 2048
_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 2.0

# Third-party DEBUG output is noisy enough to hide JoyRead's own diagnostics.
_QUIET_LIBRARIES = ("PIL", "urllib3", "asyncio", "matplotlib")
_SENSITIVE_KEY_PARTS = ("password", "passwd", "secret", "token", "credential")

# Real path/word delimiters this app's own log messages use around a path
# (see e.g. `f"Could not open PDF: {path} ({error.name})"`,
# `f"...at {path}: {exc}"`): a path is always followed by end-of-string, ": ",
# or " (" -- never by bare prose with no punctuation. None of these
# characters are legal within a path segment, so excluding them lets a
# segment safely contain spaces (real book/library folder names do) while
# still stopping at the point a message continues in free text. The bounded
# repeat on the final segment caps how many stray words a missing-delimiter
# edge case could pull in, instead of consuming to end-of-string like the
# unbounded form used to.
_PATH_DELIMS = r",:;()\[\]{}<>\"'"
_WINDOWS_PATH_RE = re.compile(
    r"(?<![\w])(?:[A-Za-z]:\\|\\\\)"
    rf"(?:[^\\{_PATH_DELIMS}]+\\)*"
    rf"[^{_PATH_DELIMS}\s]+(?:[ \t][^{_PATH_DELIMS}\s]+){{0,4}}"
)
_POSIX_PATH_RE = re.compile(
    r"(?<![\w])/"
    rf"(?:[^/{_PATH_DELIMS}]+/)*"
    rf"[^{_PATH_DELIMS}\s]+(?:[ \t][^{_PATH_DELIMS}\s]+){{0,4}}"
)
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


@dataclass(frozen=True, slots=True)
class LoggingShutdownResult:
    drained: bool
    pending_records: int
    dropped_records: int


class _PrivacyRedactor:
    def __init__(self, run_id: str, *, sensitive_enabled: bool) -> None:
        self._salt = run_id.encode("utf-8")
        self._sensitive_enabled = sensitive_enabled

    def redact_text(self, value: object) -> str:
        text = str(value)
        # Credentials are never diagnostic output. The opt-in below only
        # permits raw filesystem paths for a local debugging session.
        text = _PASSWORD_ASSIGNMENT_RE.sub(r"\1\2<redacted>", text)
        text = _PASSWORD_ARGUMENT_SEPARATED_RE.sub("-p<redacted>", text)
        if _ARCHIVE_TOOL_HINT_RE.search(text):
            text = _PASSWORD_ARGUMENT_GLUED_RE.sub("-p<redacted>", text)
        if self._sensitive_enabled:
            return text
        text = _WINDOWS_PATH_RE.sub(self._path_token, text)
        return _POSIX_PATH_RE.sub(self._path_token, text)

    def safe_field(self, key: str, value: object) -> object:
        if any(part in key.casefold() for part in _SENSITIVE_KEY_PARTS):
            return "<redacted>"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Path):
            # A structured Path field holds nothing but the path -- redact
            # the whole value directly instead of pattern-matching for one
            # inside a larger string. That sidesteps the inherent ambiguity
            # regex-based path detection has around embedded spaces.
            return self._redact_path(str(value))
        if isinstance(value, (tuple, list)):
            return [self.safe_field(key, item) for item in value]
        return self.redact_text(value)

    def _redact_path(self, raw: str) -> str:
        suffix = Path(raw.rstrip(".\"")).suffix.lower()
        # A per-run keyed digest keeps repeated paths correlatable in one log
        # without creating a stable identifier that survives log sharing.
        digest = hashlib.blake2s(raw.encode("utf-8"), key=self._salt, digest_size=4).hexdigest()
        return f"<path:{digest}{suffix}>"

    def _path_token(self, match: re.Match[str]) -> str:
        return self._redact_path(match.group(0))


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


class _UtcTextFormatter(logging.Formatter):
    converter = time.gmtime

    def __init__(self, redactor: _PrivacyRedactor) -> None:
        super().__init__(_TEXT_FORMAT, datefmt=_TEXT_DATEFMT)
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redactor.redact_text(super().format(record))


class _JsonFormatter(logging.Formatter):
    def __init__(self, redactor: _PrivacyRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp_utc": _utc_timestamp(record.created),
            "level": record.levelname,
            "category": getattr(record, "category", _category_from_logger(record.name)),
            "event": getattr(record, "event", "log.message"),
            "message": self._redactor.redact_text(record.getMessage()),
            "run_id": getattr(record, "run_id", None),
            "run_started_at_utc": getattr(record, "run_started_at_utc", None),
            "operation_id": getattr(record, "operation_id", None),
            "parent_operation_id": getattr(record, "parent_operation_id", None),
            "operation_name": getattr(record, "operation_name", None),
            "pid": record.process,
            "thread": record.threadName,
            "logger": record.name,
            "source": f"{Path(record.pathname).name}:{record.lineno}",
            "app_version": getattr(record, "app_version", None),
            "build_id": getattr(record, "build_id", None),
            "status": getattr(record, "status", None),
        }
        for key, value in record.__dict__.items():
            if key in _LOG_RECORD_RESERVED_FIELDS or key in payload or key.startswith("_"):
                continue
            payload[key] = self._redactor.safe_field(key, value)
        if record.exc_info:
            payload["exception"] = self._redactor.redact_text(self.formatException(record.exc_info))
            payload.setdefault("error_type", record.exc_info[0].__name__)
        if record.stack_info:
            payload["stack"] = self._redactor.redact_text(self.formatStack(record.stack_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


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
    def _open(self):  # noqa: ANN202 - stdlib override.
        stream = super()._open()
        if os.name == "posix":
            try:
                os.chmod(self.baseFilename, 0o600)
            except OSError as exc:
                _emergency_write(f"Unable to restrict log-file permissions: {exc}")
        return stream


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
        redactor = _PrivacyRedactor(_metadata.run_id, sensitive_enabled=_sensitive_logging_enabled())
        context_filter = _ContextFilter(_metadata)
        formatter = _UtcTextFormatter(redactor)
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

    global _early_buffer, _metadata, _runtime
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
        redactor = _PrivacyRedactor(_metadata.run_id, sensitive_enabled=_sensitive_logging_enabled())
        handlers: list[logging.Handler] = []
        log_file: Path | None = None
        setup_error: str | None = None
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_file = logs_dir / LOG_FILE_NAME
            file_handler = _SecureRotatingFileHandler(
                log_file,
                maxBytes=LOG_FILE_MAX_BYTES,
                backupCount=LOG_FILE_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(_JsonFormatter(redactor))
            handlers.append(file_handler)
        except Exception as exc:
            setup_error = f"{type(exc).__name__}: {exc}"
            _emergency_write(f"Unable to initialize JoyRead file logging: {setup_error}")

        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(level)
        stream_handler.setFormatter(_UtcTextFormatter(redactor))
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
        return LoggingConfigurationResult(log_file is not None, log_file, setup_error)


def flush_logging(timeout_seconds: float = 1.0) -> bool:
    """Wait for already-queued records without stopping the runtime."""

    runtime = _runtime
    return True if runtime is None else runtime.wait_until_empty(timeout_seconds)


def shutdown_logging(
    timeout_seconds: float = _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
) -> LoggingShutdownResult:
    """Seal producers, drain queued records, and close handlers once."""

    global _early_buffer, _runtime
    with _runtime_lock:
        runtime = _runtime
        if runtime is None:
            _reset_root_handlers()
            _early_buffer = None
            _restore_exception_hooks()
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
        return result


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
    redactor = _PrivacyRedactor(metadata.run_id, sensitive_enabled=_sensitive_logging_enabled())
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


def _sensitive_logging_enabled() -> bool:
    return os.environ.get(LOG_SENSITIVE_ENV_VAR, "").strip().casefold() in {"1", "true", "yes", "on"}


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
    "LOG_FILE_BACKUP_COUNT",
    "LOG_FILE_MAX_BYTES",
    "LOG_FILE_NAME",
    "LOG_QUEUE_CAPACITY",
    "LOG_SENSITIVE_ENV_VAR",
    "LoggingConfigurationResult",
    "LoggingMetadata",
    "LoggingShutdownResult",
    "configure_early_logging",
    "configure_logging",
    "describe_callback",
    "flush_logging",
    "get_logger",
    "log_event",
    "log_timed_block",
    "operation_scope",
    "shutdown_logging",
    "write_emergency_log",
]
