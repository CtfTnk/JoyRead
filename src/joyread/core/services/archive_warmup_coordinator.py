"""Application-scope deduplication for expensive archive cache warmup."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from pathlib import Path

from joyread.core.reader import ReaderSessionService
from joyread.core.services.task_service import TaskHandle, TaskPriority, TaskService


logger = logging.getLogger(__name__)


@dataclass
class _WarmupState:
    path: Path
    nested_depth: int
    global_depth: int
    callbacks: dict[str, Callable[[], None]] = field(default_factory=dict)
    handle: TaskHandle[None] | None = None


class ArchiveWarmupCoordinator:
    """Runs at most one whole-document warmup and deduplicates consumers."""

    def __init__(self, session_service: ReaderSessionService, task_service: TaskService) -> None:
        self._session_service = session_service
        self._task_service = task_service
        self._states: dict[str, _WarmupState] = {}
        self._queue: deque[str] = deque()
        self._active_key: str | None = None

    def acquire(
        self,
        source_path: Path,
        client_id: str,
        *,
        nested_depth: int,
        global_depth: int,
        on_ready: Callable[[], None],
    ) -> None:
        key = self._source_key(source_path, nested_depth, global_depth)
        state = self._states.get(key)
        if state is None:
            state = _WarmupState(Path(source_path), int(nested_depth), int(global_depth))
            self._states[key] = state
            self._queue.append(key)
        state.callbacks[client_id] = on_ready
        self._start_next()

    def release(self, client_id: str) -> None:
        for state in self._states.values():
            state.callbacks.pop(client_id, None)

    def close(self) -> None:
        for state in self._states.values():
            state.callbacks.clear()

    def replace_session_service(self, session_service: ReaderSessionService) -> None:
        self.close()
        self._session_service = session_service

    def _start_next(self) -> None:
        if self._active_key is not None:
            return
        while self._queue:
            key = self._queue.popleft()
            state = self._states.get(key)
            if state is None:
                continue
            if not state.callbacks:
                self._states.pop(key, None)
                continue
            self._active_key = key

            def work(key: str = key, state: _WarmupState = state) -> None:
                self._session_service.warm_disk_cache(
                    state.path,
                    nested_archive_max_depth=state.nested_depth,
                    archive_global_file_max_depth=state.global_depth,
                    chunk_size=8,
                    is_cancelled=lambda key=key: not self._has_consumers(key),
                )

            kwargs = {
                "on_success": lambda _result, key=key: self._finish(key, notify=True),
                "on_failure": lambda error, key=key: self._fail(key, error),
            }
            try:
                handle = self._task_service.submit(
                    "archive-cache-warm",
                    work,
                    priority=TaskPriority.BACKGROUND,
                    **kwargs,
                )
            except TypeError:
                handle = self._task_service.submit("archive-cache-warm", work, **kwargs)
            current = self._states.get(key)
            if current is not None:
                current.handle = handle
            return

    def _finish(self, key: str, *, notify: bool) -> None:
        state = self._states.pop(key, None)
        if self._active_key == key:
            self._active_key = None
        callbacks = tuple(state.callbacks.values()) if state is not None and notify else ()
        for callback in callbacks:
            callback()
        self._start_next()

    def _fail(self, key: str, error: Exception) -> None:
        logger.warning("Archive cache warmup failed for %s: %s", key, error)
        self._finish(key, notify=False)

    def _has_consumers(self, key: str) -> bool:
        state = self._states.get(key)
        return state is not None and bool(state.callbacks)

    @staticmethod
    def _source_key(source_path: Path, nested_depth: int, global_depth: int) -> str:
        try:
            stat = source_path.stat()
            signature = f"{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            signature = "missing"
        return f"{source_path.resolve(strict=False)}:{signature}:{nested_depth}:{global_depth}"
