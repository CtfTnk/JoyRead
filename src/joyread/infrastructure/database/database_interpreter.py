"""Priority-queued SQLite actor used by repositories and services."""

from __future__ import annotations

import logging
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import IntEnum
from itertools import count
from pathlib import Path
from queue import PriorityQueue
import sqlite3
from threading import Event, Thread
from typing import Callable, Generic, TypeVar

from joyread.core.operation_context import OperationContext, bind_operation, create_operation
from joyread.infrastructure.database.sqlite_connection import open_sqlite_connection
from joyread.infrastructure.logging import describe_callback, log_event


T = TypeVar("T")

logger = logging.getLogger(__name__)

# Single-callback runtime above this triggers a WARNING. The DB actor is
# serial, so a slow query directly stalls every other queued request.
_SLOW_QUERY_MS = 200


class DatabasePriority(IntEnum):
    CRITICAL = 0
    HIGH = 10
    NORMAL = 20
    LOW = 30
    BACKGROUND = 40


@dataclass(order=True)
class _QueuedDatabaseRequest(Generic[T]):
    priority: int
    sequence: int
    callback: Callable[[sqlite3.Connection], T] | None = field(compare=False)
    future: Future[T] | None = field(compare=False)
    callback_label: str = field(default="<stop>", compare=False)
    operation_context: OperationContext | None = field(default=None, compare=False)


class DatabaseInterpreter:
    """Owns one SQLite connection and executes callbacks serially.

    Repositories submit small functions instead of opening their own
    connections. That makes SQLite thread ownership explicit: all DB work
    happens on ``JoyReadDatabaseInterpreter``, while callers wait on a Future
    or receive errors through that Future.
    """

    def __init__(self, database_path: Path, *, autostart: bool = True) -> None:
        self.database_path = database_path
        self._queue: PriorityQueue[_QueuedDatabaseRequest[object]] = PriorityQueue()
        self._sequence = count(1)
        self._ready = Event()
        self._closed = False
        self._autostart = autostart
        self._thread = Thread(target=self._run, name="JoyReadDatabaseInterpreter", daemon=True)
        if autostart:
            self.start()

    def start(self) -> None:
        if not self._thread.is_alive():
            logger.info("DatabaseInterpreter starting (path=%s)", self.database_path)
            self._thread.start()
            self._ready.wait(timeout=5)

    def submit(
        self,
        callback: Callable[[sqlite3.Connection], T],
        priority: DatabasePriority = DatabasePriority.NORMAL,
    ) -> Future[T]:
        if self._closed:
            raise RuntimeError("DatabaseInterpreter is closed.")
        if self._autostart:
            self.start()
        future: Future[T] = Future()
        request: _QueuedDatabaseRequest[T] = _QueuedDatabaseRequest(
            priority=int(priority),
            sequence=next(self._sequence),
            callback=callback,
            future=future,
            callback_label=describe_callback(callback),
            operation_context=create_operation("database.request", category="database"),
        )
        self._queue.put(request)  # type: ignore[arg-type]
        logger.debug(
            "DB request queued seq=%d priority=%s callback=%s queue_depth=%d",
            request.sequence,
            DatabasePriority(request.priority).name,
            request.callback_label,
            self._queue.qsize(),
        )
        return future

    def execute(
        self,
        callback: Callable[[sqlite3.Connection], T],
        priority: DatabasePriority = DatabasePriority.NORMAL,
        timeout: float | None = None,
    ) -> T:
        self.start()
        return self.submit(callback, priority).result(timeout=timeout)

    def close(self) -> None:
        if self._closed:
            return
        logger.info(
            "DatabaseInterpreter closing (path=%s, pending=%d)",
            self.database_path,
            self._queue.qsize(),
        )
        self._closed = True
        # Sentinel "stop running" marker for the interpreter thread: a
        # callback of `None` plus an effectively-infinite priority means the
        # worker can drain any pre-queued real work first and only then
        # encounter this entry and break out of its run loop.
        self._queue.put(
            _QueuedDatabaseRequest(
                priority=1_000_000,
                sequence=next(self._sequence),
                callback=None,
                future=None,
                callback_label="<stop>",
            )
        )
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        timed_out = self._thread.is_alive()
        log_event(
            logger,
            logging.WARNING if timed_out else logging.INFO,
            "database.interpreter.closed",
            "DatabaseInterpreter close timed out" if timed_out else "DatabaseInterpreter closed",
            category="database",
            status="timed_out" if timed_out else "finished",
            count=self._queue.qsize(),
        )

    def _run(self) -> None:
        connection = open_sqlite_connection(self.database_path)
        logger.debug("DatabaseInterpreter ready (thread=%s)", self._thread.name)
        self._ready.set()
        try:
            while True:
                request = self._queue.get()
                if request.callback is None:
                    self._queue.task_done()
                    break
                with bind_operation(request.operation_context):
                    if request.future is not None and request.future.cancelled():
                        # The caller abandoned this read before the queue reached
                        # it (typically a viewmodel that was torn down between
                        # submit and execute). Running the callback now would
                        # just waste a connection-bound SQL roundtrip, so skip.
                        logger.debug(
                            "DB request skipped seq=%d priority=%s callback=%s (future cancelled)",
                            request.sequence,
                            DatabasePriority(request.priority).name,
                            request.callback_label,
                        )
                        self._queue.task_done()
                        continue
                    start = time.perf_counter()
                    logger.debug(
                        "DB request starting seq=%d priority=%s callback=%s",
                        request.sequence,
                        DatabasePriority(request.priority).name,
                        request.callback_label,
                    )
                    try:
                        result = request.callback(connection)
                    except Exception as exc:
                        # Surface the failure before stashing on the future so the
                        # error path is visible even when the caller never reads
                        # ``future.result()``. duration_ms lives here rather than
                        # in a separate slow-query notice, so a request never gets
                        # a "finished" record after it already got a "failed" one.
                        elapsed_ms = (time.perf_counter() - start) * 1000.0
                        log_event(
                            logger,
                            logging.ERROR,
                            "database.request.failed",
                            "Database request failed",
                            category="database",
                            status="failed",
                            priority=DatabasePriority(request.priority).name,
                            callback=request.callback_label,
                            error_type=type(exc).__name__,
                            reason=str(exc),
                            duration_ms=round(elapsed_ms, 3),
                            exc_info=True,
                        )
                        if request.future is not None:
                            request.future.set_exception(exc)
                    else:
                        if request.future is not None:
                            request.future.set_result(result)
                        elapsed_ms = (time.perf_counter() - start) * 1000.0
                        if elapsed_ms >= _SLOW_QUERY_MS:
                            log_event(
                                logger,
                                logging.WARNING,
                                "database.request.slow",
                                "Database request exceeded the slow-query threshold",
                                category="database",
                                status="finished",
                                priority=DatabasePriority(request.priority).name,
                                callback=request.callback_label,
                                duration_ms=round(elapsed_ms, 3),
                            )
                        else:
                            logger.debug(
                                "DB request completed seq=%d priority=%s callback=%s",
                                request.sequence,
                                DatabasePriority(request.priority).name,
                                request.callback_label,
                            )
                    finally:
                        self._queue.task_done()
        finally:
            connection.close()
            logger.debug("DatabaseInterpreter thread exited")
