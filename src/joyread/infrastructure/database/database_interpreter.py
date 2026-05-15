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

from joyread.infrastructure.database.sqlite_connection import open_sqlite_connection


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


class DatabaseInterpreter:
    """Owns a single SQLite connection and executes requests serially."""

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
        )
        self._queue.put(request)  # type: ignore[arg-type]
        logger.debug(
            "DB request queued seq=%d priority=%s queue_depth=%d",
            request.sequence,
            DatabasePriority(request.priority).name,
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
        self._queue.put(
            _QueuedDatabaseRequest(
                priority=1_000_000,
                sequence=next(self._sequence),
                callback=None,
                future=None,
            )
        )
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("DatabaseInterpreter closed")

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
                if request.future is not None and request.future.cancelled():
                    self._queue.task_done()
                    continue
                start = time.perf_counter()
                try:
                    result = request.callback(connection)
                except Exception as exc:
                    # Surface the failure before stashing on the future so the
                    # error path is visible even when the caller never reads
                    # ``future.result()``. Previously this was the silent
                    # site that hid the schema-drift OperationalError.
                    logger.error(
                        "DB request failed seq=%d priority=%s: %s",
                        request.sequence,
                        DatabasePriority(request.priority).name,
                        exc,
                        exc_info=True,
                    )
                    if request.future is not None:
                        request.future.set_exception(exc)
                else:
                    if request.future is not None:
                        request.future.set_result(result)
                finally:
                    elapsed_ms = (time.perf_counter() - start) * 1000.0
                    if elapsed_ms >= _SLOW_QUERY_MS:
                        logger.warning(
                            "Slow DB request seq=%d priority=%s elapsed_ms=%.0f",
                            request.sequence,
                            DatabasePriority(request.priority).name,
                            elapsed_ms,
                        )
                    self._queue.task_done()
        finally:
            connection.close()
            logger.debug("DatabaseInterpreter thread exited")
