"""Application logging setup."""

from __future__ import annotations

import logging
import logging.handlers
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from PySide6 import QtCore


LOG_FILE_NAME = "joyread.log"
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 5
LOG_ENV_VAR = "JOYREAD_LOG_LEVEL"

_LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(threadName)s] %(name)s:%(lineno)d  %(message)s"
_LOG_DATEFMT = "%H:%M:%S"

# Third-party libraries whose DEBUG output drowns the dev log. Pin them to
# WARNING regardless of the root level so JOYREAD_LOG_LEVEL=DEBUG still
# leaves joyread-only traces readable.
_QUIET_LIBRARIES = ("PIL", "urllib3", "asyncio", "matplotlib")


def configure_early_logging() -> None:
    """Stderr-only logging suitable before the writable logs path is known.

    Called from bootstrap before AppContext creation so that
    ``create_app_context`` lifecycle lines (migrations, service init) make it
    to stderr at the level requested by ``JOYREAD_LOG_LEVEL``. Once the path
    service exists, ``configure_logging`` is called again to add the rotating
    file handler; both calls are idempotent on the root logger.
    """

    level, _ = _resolve_level()
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)
    _reset_root_handlers()
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(level)
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(stream_handler)
    for name in _QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)


def configure_logging(logs_dir: Path) -> None:
    """Install JoyRead's logging handlers.

    Idempotent: existing handlers on the root logger are removed first so
    repeated calls (e.g. across tests, or after a storage reload) do not
    double-emit.
    """

    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / LOG_FILE_NAME

    level, unknown_value = _resolve_level()
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(level)

    _reset_root_handlers()
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    for name in _QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)

    _install_qt_message_handler()

    if unknown_value is not None:
        logging.getLogger(__name__).warning(
            "Unknown %s=%r; falling back to INFO.", LOG_ENV_VAR, unknown_value
        )


def _reset_root_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - close() failure is best-effort.
            pass


def _resolve_level() -> tuple[int, str | None]:
    raw = os.environ.get(LOG_ENV_VAR)
    if not raw:
        return logging.INFO, None
    candidate = raw.strip().upper()
    resolved = logging.getLevelName(candidate)
    if isinstance(resolved, int):
        return resolved, None
    return logging.INFO, raw


def _install_qt_message_handler() -> None:
    qt_logger = logging.getLogger("joyread.qt")

    def handler(msg_type, context, message):  # noqa: ANN001 - Qt callback signature.
        level = _QT_LEVEL_MAP.get(msg_type, logging.INFO)
        location = ""
        if context is not None and getattr(context, "file", None):
            location = f" ({context.file}:{context.line})"
        qt_logger.log(level, "%s%s", message, location)

    QtCore.qInstallMessageHandler(handler)


_QT_LEVEL_MAP = {
    QtCore.QtMsgType.QtDebugMsg: logging.DEBUG,
    QtCore.QtMsgType.QtInfoMsg: logging.INFO,
    QtCore.QtMsgType.QtWarningMsg: logging.WARNING,
    QtCore.QtMsgType.QtCriticalMsg: logging.ERROR,
    QtCore.QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def get_logger(name: str) -> logging.Logger:
    """One-line wrapper around :func:`logging.getLogger` for the joyread tree.

    Same semantics as ``logging.getLogger(name)`` today. The indirection lets
    future cross-cutting concerns (rate-limiting, structured context fields,
    per-subsystem filters) be added in one place instead of touching every
    module's ``getLogger`` call.
    """

    return logging.getLogger(name)


@contextmanager
def log_timed_block(
    logger: logging.Logger,
    label: str,
    *,
    level: int = logging.DEBUG,
) -> Iterator[None]:
    """Emit ``label start`` / ``label done in X ms`` around a block.

    Lets one-off code paths (archive scans, import phases, storage moves) get
    a start/finish trace without sprinkling ``perf_counter()`` + ``logger``
    pairs everywhere. Exceptions propagate; the "done" line still fires from
    the ``finally`` clause so a failure-aborted block is still bracketed in
    the log.
    """

    start = time.perf_counter()
    logger.log(level, "%s start", label)
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        logger.log(level, "%s done in %.0f ms", label, elapsed_ms)
