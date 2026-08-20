"""Qt-free structured logging helpers built on :mod:`joyread.core.operation_context`.

These wrap the standard :mod:`logging` API only -- no file I/O, rotation, or
Qt integration -- so Core services can log a correlated, truthfully-terminated
operation without importing Infrastructure (which pulls in PySide6 for its
Qt message-handler bridge). Infrastructure re-exports every name here from
``joyread.infrastructure.logging`` unchanged, so existing callers there are
unaffected by where the implementation lives.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from functools import partial
from types import TracebackType
from typing import Iterator, Mapping

from joyread.core.operation_context import OperationContext, bind_operation, create_operation


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    message: str,
    *,
    category: str | None = None,
    exc_info: bool | tuple[type[BaseException], BaseException, TracebackType] | None = None,
    **fields: object,
) -> None:
    """Emit one structured event through the standard logging API."""

    extra = {"event": event, "category": category or event.partition(".")[0]}
    extra.update(fields)
    logger.log(level, message, extra=extra, exc_info=exc_info)


@contextmanager
def operation_scope(
    logger: logging.Logger,
    event: str,
    *,
    category: str | None = None,
    level: int = logging.INFO,
    operation: OperationContext | None = None,
    fields: Mapping[str, object] | None = None,
) -> Iterator[OperationContext]:
    """Log one synchronous operation with an unambiguous terminal event."""

    resolved_category = category or event.partition(".")[0]
    resolved = operation or create_operation(event, category=resolved_category)
    start = time.perf_counter()
    with bind_operation(resolved):
        log_event(
            logger,
            level,
            f"{event}.started",
            f"{event} started",
            category=resolved_category,
            status="started",
            **dict(fields or {}),
        )
        try:
            yield resolved
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            failure_kind = getattr(exc, "task_failure_kind", "unexpected")
            cancelled = failure_kind == "cancelled"
            controlled = failure_kind in {"expected", "controlled"}
            log_event(
                logger,
                logging.INFO if cancelled else logging.WARNING if controlled else logging.ERROR,
                f"{event}.cancelled" if cancelled else f"{event}.failed",
                f"{event} cancelled" if cancelled else f"{event} failed",
                category=resolved_category,
                status="cancelled" if cancelled else "failed",
                duration_ms=round(elapsed_ms, 3),
                error_type=type(exc).__name__,
                reason=str(exc),
                exc_info=not (cancelled or controlled),
                **dict(fields or {}),
            )
            raise
        else:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            log_event(
                logger,
                level,
                f"{event}.finished",
                f"{event} finished",
                category=resolved_category,
                status="finished",
                duration_ms=round(elapsed_ms, 3),
                **dict(fields or {}),
            )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def describe_callback(callback: object) -> str:
    """Return a stable human-readable name for a function-like object."""

    if isinstance(callback, partial):
        return f"functools.partial({describe_callback(callback.func)})"
    function = getattr(callback, "__func__", None)
    if function is not None:
        owner = getattr(callback, "__self__", None)
        owner_type = owner if isinstance(owner, type) else type(owner)
        module = getattr(function, "__module__", None) or owner_type.__module__
        return f"{module}.{owner_type.__qualname__}.{function.__name__}"
    owner = getattr(callback, "__self__", None)
    name = getattr(callback, "__name__", None)
    if owner is not None and name is not None:
        owner_type = owner if isinstance(owner, type) else type(owner)
        return f"{owner_type.__module__}.{owner_type.__qualname__}.{name}"
    module = getattr(callback, "__module__", None)
    qualname = getattr(callback, "__qualname__", None) or name
    if module is not None and qualname is not None:
        return f"{module}.{qualname}"
    callback_type = type(callback)
    return f"{callback_type.__module__}.{callback_type.__qualname__}"


@contextmanager
def log_timed_block(
    logger: logging.Logger,
    label: str,
    *,
    level: int = logging.DEBUG,
) -> Iterator[None]:
    """Bracket a diagnostic block with truthful success/failure terminal logs."""

    start = time.perf_counter()
    logger.log(level, "%s start", label)
    try:
        yield
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        logger.log(level, "%s failed in %.0f ms", label, elapsed_ms, exc_info=True)
        raise
    else:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        logger.log(level, "%s done in %.0f ms", label, elapsed_ms)


__all__ = [
    "describe_callback",
    "get_logger",
    "log_event",
    "log_timed_block",
    "operation_scope",
]
