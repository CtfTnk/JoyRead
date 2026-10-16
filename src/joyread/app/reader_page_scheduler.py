"""GUI-thread-owned, bounded page admission for confirmed random-access sources.

The executor owns threads; this scheduler owns demand, priority and fairness.
Only admitted pages reach the executor. Retiring an attempt does not release
its slot until the executor's unconditional finished callback arrives.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Sequence
from contextvars import Context, copy_context
from dataclasses import dataclass, field
from threading import Event

from joyread.app.tasking import TaskExecutor, TaskHandle, TaskPriority


@dataclass(frozen=True)
class PageDemand:
    key: Hashable
    priority: TaskPriority
    work: Callable[[Event], object]
    ready: Callable[[object], None]
    failed: Callable[[Exception], None]
    context: Context = field(default_factory=copy_context, compare=False, repr=False)
    is_thumbnail: bool = False
    group: object | None = None


@dataclass(eq=False)
class _Attempt:
    owner: object
    executor: TaskExecutor
    demand: PageDemand
    cancelled: Event = field(default_factory=Event)
    handle: TaskHandle | None = None

    @property
    def group(self) -> object:
        return self.demand.group if self.demand.group is not None else self.owner


class ReaderPageScheduler:
    """Dispatch at most four pages, at most two per Reader, in priority order.

    Callers replace their complete demand set. Pending pages are promoted or
    demoted in place, and equal-priority Readers take turns. This is not a
    preemptive executor: an already-running native decode is allowed to finish.
    Body and Topic owners can share a group, preserving the combined Reader
    limit. Thumbnails occupy at most two global slots and cannot dispatch while
    body demand waits, even when that body group is currently at its limit.
    """

    def __init__(self, *, capacity: int = 4, per_reader: int = 2) -> None:
        self.capacity = max(1, min(4, capacity))
        self.per_reader = max(1, min(self.capacity, per_reader))
        self._owners: dict[object, dict[Hashable, _Attempt]] = {}
        self._active: set[_Attempt] = set()
        self._last_owner: dict[TaskPriority, object] = {}
        self._pumping = False
        self._closed = False
        self._sealed: set[TaskExecutor] = set()

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def pending_count(self) -> int:
        return sum(attempt not in self._active for jobs in self._owners.values() for attempt in jobs.values())

    def active_keys(self, owner: object) -> tuple[Hashable, ...]:
        """Live admitted demand for one client, excluding retired attempts."""
        return tuple(attempt.demand.key for attempt in self._active
                     if attempt.owner is owner and self._wanted(attempt))

    def replace(self, owner: object, executor: TaskExecutor, demands: Sequence[PageDemand]) -> None:
        if self._closed or executor in self._sealed:
            self.cancel(owner)
            return
        previous = self._owners.get(owner, {})
        current: dict[Hashable, _Attempt] = {}
        for demand in demands:
            attempt = previous.get(demand.key)
            if attempt is None or attempt.cancelled.is_set() or attempt.executor is not executor:
                attempt = _Attempt(owner, executor, demand)
            else:
                attempt.demand = demand
            current[demand.key] = attempt
        # Publish the new demand before cancellation callbacks can run.
        if current:
            self._owners[owner] = current
        else:
            self._owners.pop(owner, None)
        for key, attempt in previous.items():
            if current.get(key) is not attempt:
                self._retire(attempt)
        self._pump()

    def cancel(self, owner: object) -> None:
        for attempt in self._owners.pop(owner, {}).values():
            self._retire(attempt)
        self._pump()

    def quiesce(self, executor: TaskExecutor) -> None:
        self._sealed.add(executor)
        for owner, jobs in tuple(self._owners.items()):
            if any(attempt.executor is executor for attempt in jobs.values()):
                self.cancel(owner)

    def resume(self, executor: TaskExecutor) -> None:
        self._sealed.discard(executor)

    def close(self) -> None:
        self._closed = True
        for owner in tuple(self._owners):
            self.cancel(owner)
        self._last_owner.clear()

    @staticmethod
    def _retire(attempt: _Attempt) -> None:
        attempt.cancelled.set()
        if attempt.handle is not None:
            attempt.handle.cancel()

    def _next(self) -> _Attempt | None:
        body_waiting = any(
            attempt not in self._active and not attempt.demand.is_thumbnail
            and attempt.executor not in self._sealed
            for jobs in self._owners.values() for attempt in jobs.values()
        )
        thumbnail_slots_full = sum(
            attempt.demand.is_thumbnail for attempt in self._active
        ) >= min(2, self.capacity)
        candidates: dict[object, _Attempt] = {}
        for owner, jobs in self._owners.items():
            waiting = [
                attempt for attempt in jobs.values()
                if attempt not in self._active
                and attempt.executor not in self._sealed
                and sum(active.group is attempt.group for active in self._active) < self.per_reader
                and not (attempt.demand.is_thumbnail and (body_waiting or thumbnail_slots_full))
            ]
            if waiting:
                candidates[owner] = max(waiting, key=lambda attempt: attempt.demand.priority)
        if not candidates:
            return None
        priority = max(attempt.demand.priority for attempt in candidates.values())
        owners = list(self._owners)
        last = self._last_owner.get(priority)
        if last in owners:
            pivot = owners.index(last) + 1
            owners = owners[pivot:] + owners[:pivot]
        for owner in owners:
            attempt = candidates.get(owner)
            if attempt is not None and attempt.demand.priority == priority:
                self._last_owner[priority] = owner
                return attempt
        return None

    def _pump(self) -> None:
        if self._pumping or self._closed:
            return
        self._pumping = True
        try:
            while len(self._active) < self.capacity:
                attempt = self._next()
                if attempt is None:
                    break
                self._active.add(attempt)
                # Capture the accepted work, not the mutable priority/delivery
                # record: workers never read GUI-owned scheduling dictionaries.
                work = attempt.demand.work
                try:
                    attempt.handle = attempt.demand.context.run(
                        attempt.executor.submit,
                        "reader-random-thumbnail" if attempt.demand.is_thumbnail else "reader-random-page",
                        lambda attempt=attempt, work=work: (
                            None if attempt.cancelled.is_set() else work(attempt.cancelled)
                        ),
                        on_success=lambda result, attempt=attempt: self._ready(attempt, result),
                        on_failure=lambda error, attempt=attempt: self._failed(attempt, error),
                        on_finished=lambda attempt=attempt: self._finished(attempt),
                        priority=attempt.demand.priority,
                    )
                    if attempt.cancelled.is_set():
                        attempt.handle.cancel()
                except Exception as exc:
                    try:
                        self._failed(attempt, exc)
                    finally:
                        self._finished(attempt)
        finally:
            self._pumping = False

    def _wanted(self, attempt: _Attempt) -> bool:
        return (
            not attempt.cancelled.is_set()
            and self._owners.get(attempt.owner, {}).get(attempt.demand.key) is attempt
        )

    def _ready(self, attempt: _Attempt, result: object) -> None:
        if self._wanted(attempt) and result is not None:
            self._remove_demand(attempt)
            attempt.demand.ready(result)

    def _failed(self, attempt: _Attempt, error: Exception) -> None:
        if self._wanted(attempt):
            self._remove_demand(attempt)
            attempt.demand.failed(error)

    def _remove_demand(self, attempt: _Attempt) -> None:
        jobs = self._owners.get(attempt.owner)
        if jobs is not None and jobs.get(attempt.demand.key) is attempt:
            del jobs[attempt.demand.key]
        # Retain the owner's position until its last active attempt finishes.

    def _finished(self, attempt: _Attempt) -> None:
        if attempt not in self._active:
            return
        self._active.remove(attempt)
        self._remove_demand(attempt)
        if not self._owners.get(attempt.owner) and not any(
            active.owner is attempt.owner for active in self._active
        ):
            self._owners.pop(attempt.owner, None)
        self._pump()
