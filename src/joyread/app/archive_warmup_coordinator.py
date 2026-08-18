"""Application-scope deduplication for expensive archive cache warmup."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from pathlib import Path
from threading import Event
from uuid import uuid4

from joyread.app.tasking import TaskExecutor, TaskHandle, TaskPriority
from joyread.core.archive import ArchiveOpenLimits
from joyread.core.reader import ReaderSessionService


logger = logging.getLogger(__name__)


@dataclass
class _WarmupState:
    path: Path
    limits: ArchiveOpenLimits
    document_cache_key: str
    allow_persistent_cache: bool
    #: The archive password, for an encrypted document only. Warmup opens its
    #: own session, so without this it cannot read a single page and the format
    #: that most needs pre-conversion is the one that never gets it. Held for
    #: the length of the conversion and dropped with the state.
    password: str | None = None
    callbacks: dict[str, Callable[[], None]] = field(default_factory=dict)
    handle: TaskHandle[None] | None = None
    #: Set on the worker thread when the job begins and when it leaves. A
    #: cancelled task suppresses its callbacks, so these are the only reliable
    #: evidence of whether a worker exists and whether it has gone.
    started: Event = field(default_factory=Event)
    exited: Event = field(default_factory=Event)


class ArchiveWarmupCoordinator:
    """Run one whole-document warmup at a time and deduplicate consumers."""

    def __init__(self, session_service: ReaderSessionService, task_service: TaskExecutor) -> None:
        self._session_service = session_service
        self._task_service = task_service
        self._states: dict[str, _WarmupState] = {}
        self._queue: deque[str] = deque()
        self._active_key: str | None = None
        #: A worker that was forgotten while still running. It keeps the single
        #: warmup slot until it actually exits, so a reset cannot overlap two
        #: whole-document extractions.
        self._retired: _WarmupState | None = None

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
        password: str | None = None,
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
                password,
            )
            self._states[key] = state
            self._queue.append(key)
        state.callbacks[client_id] = on_ready
        self._start_next()

    def release(self, client_id: str) -> None:
        for state in self._states.values():
            state.callbacks.pop(client_id, None)

    def invalidate(self) -> None:
        """Retire warmups created under an older archive-limits snapshot."""

        active_key = self._active_key
        for key, state in tuple(self._states.items()):
            state.callbacks.clear()
            if key != active_key:
                self._states.pop(key, None)
        self._queue.clear()

    def close(self) -> None:
        for state in self._states.values():
            state.callbacks.clear()

    def reset(self) -> None:
        """Forget every warmup, including one whose task never reported back.

        ``close()`` only withdraws consumers, which asks a running warmup to
        stop. If that task is then cancelled at the task-service level, its
        success and failure callbacks are both suppressed, so ``_finish`` never
        runs and ``_active_key`` stays set -- and ``_start_next`` returns
        immediately forever after, so no later warmup can ever start.

        Call this once the worker has actually drained, when the absence of a
        callback means the task is gone rather than still running.

        A worker that has *not* drained is not forgotten outright: it keeps the
        single warmup slot until it exits. Dropping it would let the next
        ``acquire`` of the same document start a second whole-document
        extraction while the first was still unwinding.
        """

        active = self._states.get(self._active_key) if self._active_key is not None else None
        self._states.clear()
        self._queue.clear()
        self._active_key = None
        if active is not None and active.started.is_set() and not active.exited.is_set():
            self._retired = active
        else:
            self._retired = None

    def replace_session_service(self, session_service: ReaderSessionService) -> None:
        # A rebuilt session service means the old warmups are pointed at
        # retired storage, so they are dropped rather than merely muted.
        self.reset()
        self._session_service = session_service

    def _start_next(self) -> None:
        if self._retired is not None:
            if not self._retired.exited.is_set():
                # A forgotten worker is still unwinding. Hold the single slot
                # rather than overlapping two whole-document extractions; the
                # request stays queued and the Reader calls acquire() again on
                # its next layout pass, which retries this.
                return
            self._retired = None
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
                # Marked here rather than at submit: a task cancelled before
                # its runnable starts never reaches this line, and that is
                # exactly the case where there is no worker to wait for.
                state.started.set()
                try:
                    self._run_warmup(key, state)
                finally:
                    state.exited.set()

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

    def _run_warmup(self, key: str, state: _WarmupState) -> None:
        """Call the session service, tolerating an older keyword signature."""

        cancelled = lambda: not self._is_wanted(key, state)  # noqa: E731
        try:
            self._session_service.warm_disk_cache(
                state.path,
                password=state.password,
                limits=state.limits,
                document_cache_key=state.document_cache_key,
                allow_persistent_cache=state.allow_persistent_cache,
                chunk_size=8,
                is_cancelled=cancelled,
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
                is_cancelled=cancelled,
            )

    def _is_wanted(self, key: str, state: _WarmupState) -> bool:
        """Whether *this* job is still wanted -- not merely whether its key is.

        Identity matters because the key is derived from the document and the
        limits snapshot, so reopening the same book produces the same key. A
        purely key-based test would let a *new* state answer on behalf of an
        old worker: after a reset, the old job would see consumers again and
        resume, running a second whole-document extraction alongside the one
        that just started, into a pool that may since have been replaced.
        """

        return self._states.get(key) is state and bool(state.callbacks)

    @staticmethod
    def _source_key(document_cache_key: str, limits: ArchiveOpenLimits) -> str:
        return f"{document_cache_key}:limits={limits.cache_signature()}"


def _core_depth_limit(value: object) -> int | None:
    depth = int(value)
    return None if depth == -1 else depth


def _legacy_depth_limit(value: int | None) -> int:
    return -1 if value is None else value
