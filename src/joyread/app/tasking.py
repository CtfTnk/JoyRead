"""Qt-free task contracts shared by application workflows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Generic, Protocol, TypeVar

from joyread.core.operation_context import OperationContext


T = TypeVar("T")
I = TypeVar("I")


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskPriority(IntEnum):
    """Executor-independent priorities for user-facing work."""

    BACKGROUND = -100
    LOW = -10
    THUMBNAIL = -20
    THUMBNAIL_PREFETCH = -30
    NORMAL = 0
    HIGH = 10
    CRITICAL = 100


@dataclass
class TaskHandle(Generic[T]):
    """Cooperative cancellation and status shared across executor adapters."""

    task_id: str
    callback_label: str = ""
    status: TaskStatus = TaskStatus.PENDING
    result: T | None = None
    error: Exception | None = None
    operation_context: OperationContext | None = None
    _signals: object | None = field(default=None, repr=False)

    def cancel(self) -> None:
        if self.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            self.status = TaskStatus.CANCELLED


class TaskExecutor(Protocol):
    """Application port for prioritized background execution.

    submit's on_finished is a terminal GUI-thread handshake, including cancelled
    jobs. Rejected submissions call it inline. Result callbacks may be suppressed
    on cancellation, so admission/lifetime owners must use this handshake.
    """

    def submit(
        self,
        name: str,
        callback: Callable[[], T],
        *,
        on_success: Callable[[T], None] | None = None,
        on_failure: Callable[[Exception], None] | None = None,
        on_discard: Callable[[T], None] | None = None,
        on_finished: Callable[[], None] | None = None,
        priority: TaskPriority | int = TaskPriority.NORMAL,
    ) -> TaskHandle[T]: ...

    def submit_stream(
        self,
        name: str,
        callback: Callable[[Callable[[I], None]], T],
        *,
        on_item: Callable[[I], None],
        on_success: Callable[[T], None] | None = None,
        on_failure: Callable[[Exception], None] | None = None,
        priority: TaskPriority | int = TaskPriority.NORMAL,
    ) -> TaskHandle[T]: ...
