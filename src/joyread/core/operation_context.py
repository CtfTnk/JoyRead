"""Qt-free correlation context shared by JoyRead operation boundaries.

The contract contains no logging implementation. Core services can establish
business operation identity, while application and infrastructure adapters
carry it across task, database, and Qt execution boundaries.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Iterator
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class OperationContext:
    """Stable identity shared by one business operation and its descendants."""

    operation_id: str
    parent_operation_id: str | None
    category: str
    name: str


_CURRENT_OPERATION: ContextVar[OperationContext | None] = ContextVar(
    "joyread_current_operation",
    default=None,
)


def current_operation() -> OperationContext | None:
    """Return the operation bound to the current execution context."""

    return _CURRENT_OPERATION.get()


def create_operation(
    name: str,
    *,
    category: str,
    operation_id: str | None = None,
    parent: OperationContext | None = None,
) -> OperationContext:
    """Create a child of the current operation unless ``parent`` is supplied."""

    resolved_parent = current_operation() if parent is None else parent
    return OperationContext(
        operation_id=operation_id or uuid4().hex,
        parent_operation_id=(resolved_parent.operation_id if resolved_parent is not None else None),
        category=category,
        name=name,
    )


@contextmanager
def bind_operation(operation: OperationContext | None) -> Iterator[None]:
    """Bind ``operation`` for logs and descendants, then restore the caller."""

    token: Token[OperationContext | None] = _CURRENT_OPERATION.set(operation)
    try:
        yield
    finally:
        _CURRENT_OPERATION.reset(token)


__all__ = [
    "OperationContext",
    "bind_operation",
    "create_operation",
    "current_operation",
]
