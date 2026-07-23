"""Application-scope deduplication for expensive archive cache warmup."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from pathlib import Path
from uuid import uuid4

from joyread.core.archive import ArchiveOpenLimits
from joyread.core.reader import ReaderSessionService
from joyread.core.services.task_service import TaskHandle, TaskPriority, TaskService


logger = logging.getLogger(__name__)


@dataclass
class _WarmupState:
    path: Path
    limits: ArchiveOpenLimits
    document_cache_key: str
    allow_persistent_cache: bool
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
        on_ready: Callable[[], None],
        nested_depth: int | None = None,
        global_depth: int | None = None,
        limits: ArchiveOpenLimits | None = None,
        document_cache_key: str | None = None,
        allow_persistent_cache: bool = False,
    ) -> None:
        effective_limits = limits or ArchiveOpenLimits(
            nested_archive_max_depth=(
                2 if nested_depth is None else _core_depth_limit(nested_depth)
            ),
            global_file_max_depth=(
                100 if global_depth is None else _core_depth_limit(global_depth)
            ),
        )
        cache_key = document_cache_key or f"session:{uuid4().hex}"
        key = self._source_key(cache_key, effective_limits)
        state = self._states.get(key)
        if state is None:
            state = _WarmupState(
                Path(source_path),
                effective_limits,
                cache_key,
                allow_persistent_cache,
            )
            self._states[key] = state
            self._queue.append(key)
        state.callbacks[client_id] = on_ready
        self._start_next()

    def release(self, client_id: str) -> None:
        for state in self._states.values():
            state.callbacks.pop(client_id, None)

    def invalidate(self) -> None:
        """Retire warmups created under an older archive-limits snapshot.

        Active workers are intentionally not force-cancelled: TaskService
        suppresses completion callbacks for cancelled tasks, which would leave
        the coordinator thinking that the worker still owns the sole warmup
        slot. Clearing its consumers makes ``is_cancelled`` true and lets the
        archive reader stop at its next chunk boundary, then normal completion
        releases the slot for the replacement policy.
        """

        active_key = self._active_key
        for key, state in tuple(self._states.items()):
            state.callbacks.clear()
            if key != active_key:
                self._states.pop(key, None)
        self._queue.clear()

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
                try:
                    self._session_service.warm_disk_cache(
                        state.path,
                        limits=state.limits,
                        document_cache_key=state.document_cache_key,
                        allow_persistent_cache=state.allow_persistent_cache,
                        chunk_size=8,
                        is_cancelled=lambda key=key: not self._has_consumers(key),
                    )
                except TypeError as exc:
                    unsupported = str(exc)
                    if (
                        "limits" not in unsupported
                        and "document_cache_key" not in unsupported
                        and "allow_persistent_cache" not in unsupported
                    ):
                        raise
                    self._session_service.warm_disk_cache(
                        state.path,
                        nested_archive_max_depth=_legacy_depth_limit(
                            state.limits.nested_archive_max_depth
                        ),
                        archive_global_file_max_depth=_legacy_depth_limit(
                            state.limits.global_file_max_depth
                        ),
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
    def _source_key(document_cache_key: str, limits: ArchiveOpenLimits) -> str:
        return f"{document_cache_key}:limits={limits.cache_signature()}"


def _core_depth_limit(value: object) -> int | None:
    depth = int(value)
    return None if depth == -1 else depth


def _legacy_depth_limit(value: int | None) -> int:
    return -1 if value is None else value
