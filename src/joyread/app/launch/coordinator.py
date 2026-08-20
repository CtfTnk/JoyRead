"""Turn launch requests into the first window, then into ongoing window traffic.

This object owns exactly one decision: what JoyRead shows when it starts. Every
launch request that arrives before the platform gate resolves is buffered, so
argv and Finder documents are merged into a single choice instead of racing to
create competing windows.

After the gate resolves the coordinator stops arbitrating and forwards each
request straight to the window sink. Ongoing ownership and reuse of top-level
windows belong to ``ApplicationWindowManager``.
"""

from __future__ import annotations

from collections.abc import Iterable
import logging
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QObject, Signal as QtSignal
from PySide6.QtWidgets import QMainWindow

from joyread.app.launch.intent import LaunchAction, LaunchIntent, merge_open_intents
from joyread.app.launch.ready_gate import LaunchReadyGate
from joyread.core.operation_context import (
    OperationContext,
    bind_operation,
    create_operation,
)


logger = logging.getLogger(__name__)


class LaunchWindowSink(Protocol):
    """The window operations a launch request can ask for."""

    def show_library(self) -> QMainWindow: ...

    def open_files(self, paths: Iterable[str | Path]) -> tuple[QMainWindow, ...]: ...


class LaunchCoordinator(QObject):
    """Buffer launch requests until the platform says the launch is complete."""

    settled = QtSignal()

    def __init__(
        self,
        *,
        windows: LaunchWindowSink,
        gate: LaunchReadyGate,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._windows = windows
        self._gate = gate
        self._started = False
        self._ready = False
        self._buffered: list[LaunchIntent] = []
        self._initial_windows: tuple[QMainWindow, ...] = ()
        self._settle_operation: OperationContext | None = None

    @property
    def ready(self) -> bool:
        """Whether the first-window decision has been made."""

        return self._ready

    @property
    def initial_windows(self) -> tuple[QMainWindow, ...]:
        return self._initial_windows

    @property
    def initial_window(self) -> QMainWindow | None:
        return self._initial_windows[0] if self._initial_windows else None

    def start(self, initial_intent: LaunchIntent | None = None) -> None:
        """Begin arbitration, optionally seeded with the argv intent."""

        if self._started:
            raise RuntimeError("LaunchCoordinator.start() may only be called once.")
        self._started = True
        self._settle_operation = create_operation(
            "launch.window_arbitration",
            category="launch",
        )
        if initial_intent is not None:
            self._buffered.append(initial_intent)
        with bind_operation(self._settle_operation):
            logger.info(
                "Launch window arbitration started",
                extra={
                    "event": "launch.window_arbitration.started",
                    "category": "launch",
                    "status": "started",
                    "count": len(self._buffered),
                },
            )
        self._gate.when_ready(self._settle)

    def submit(self, intent: LaunchIntent) -> tuple[QMainWindow, ...]:
        """Route a launch request from the OS, local IPC, or a reopen event."""

        if not self._ready:
            self._buffered.append(intent)
            logger.debug(
                "Launch request buffered until the platform gate resolves",
                extra={
                    "event": "launch.request.buffered",
                    "category": "launch",
                    "status": "pending",
                    "action": intent.action.value,
                    "count": len(intent.paths),
                },
            )
            return ()
        # Always a fresh operation, never a reuse of whatever happens to be
        # ambient: a reentrant submit() (e.g. a document arriving while
        # _settle_bound() is still pumping events to build the first window)
        # must not alias its distinct request onto that outer operation's ID.
        # create_operation() already links it as a child of the ambient
        # operation when one exists, which is the correct relationship.
        operation = create_operation("launch.request", category="launch")
        with bind_operation(operation):
            return self._dispatch(intent)

    def open_file(self, path: Path) -> None:
        """Adapt the single-path file-open callback used by ``FileOpenRouter``."""

        self.submit(LaunchIntent.open_files((path,)))

    def _settle(self) -> None:
        if self._ready:
            return
        operation, self._settle_operation = self._settle_operation, None
        with bind_operation(operation):
            try:
                self._settle_bound()
            except Exception as exc:
                logger.error(
                    "Launch window arbitration failed",
                    exc_info=True,
                    extra={
                        "event": "launch.window_arbitration.failed",
                        "category": "launch",
                        "status": "failed",
                        "error_type": type(exc).__name__,
                    },
                )
                raise

    def _settle_bound(self) -> None:
        """Choose the first window while the launch operation is bound."""

        buffered, self._buffered = self._buffered, []
        merged = merge_open_intents(*buffered)
        # Mark ready before touching windows: constructing the first window
        # pumps events, and a document arriving during that pump must be
        # dispatched rather than appended to a buffer nobody will drain.
        self._ready = True

        if merged is None:
            logger.info("Launch settled with no document; showing the Library")
            windows: tuple[QMainWindow, ...] = (self._windows.show_library(),)
        else:
            logger.info("Launch settled with %d document(s)", len(merged.paths))
            windows = self._windows.open_files(merged.paths)
            if not windows:
                # Every requested document was rejected. The user still asked
                # JoyRead to start, so give them something usable.
                logger.warning("No document could be opened at launch; showing the Library")
                windows = (self._windows.show_library(),)

        self._initial_windows = windows
        logger.info(
            "Launch window arbitration finished",
            extra={
                "event": "launch.window_arbitration.finished",
                "category": "launch",
                "status": "finished",
                "action": merged.action.value if merged is not None else LaunchAction.SHOW_LIBRARY.value,
                "count": len(windows),
            },
        )
        self.settled.emit()

    def _dispatch(self, intent: LaunchIntent) -> tuple[QMainWindow, ...]:
        logger.info(
            "Dispatching launch request",
            extra={
                "event": "launch.request.dispatch.started",
                "category": "launch",
                "status": "started",
                "action": intent.action.value,
                "count": len(intent.paths),
            },
        )
        try:
            windows = (
                (self._windows.show_library(),)
                if intent.action == LaunchAction.SHOW_LIBRARY
                else self._windows.open_files(intent.paths)
            )
        except Exception as exc:
            logger.error(
                "Launch request dispatch failed",
                exc_info=True,
                extra={
                    "event": "launch.request.dispatch.failed",
                    "category": "launch",
                    "status": "failed",
                    "action": intent.action.value,
                    "count": len(intent.paths),
                    "error_type": type(exc).__name__,
                },
            )
            raise
        logger.info(
            "Launch request dispatch finished",
            extra={
                "event": "launch.request.dispatch.finished",
                "category": "launch",
                "status": "finished",
                "action": intent.action.value,
                "count": len(windows),
            },
        )
        return windows
