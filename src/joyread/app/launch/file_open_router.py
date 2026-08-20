"""Route operating-system file-open events into standalone reader windows."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QFileOpenEvent
from PySide6.QtWidgets import QApplication


logger = logging.getLogger(__name__)

OpenFileHandler = Callable[[Path], None]


def _supported_file_open_path(
    event: QEvent,
    supported_extensions: frozenset[str],
) -> Path | None:
    if event.type() != QEvent.Type.FileOpen or not isinstance(event, QFileOpenEvent):
        return None

    value = event.file()
    if not value:
        url = event.url()
        if not url.isLocalFile():
            return None
        value = url.toLocalFile()

    path = Path(value).expanduser()
    if path.suffix.lower() not in supported_extensions:
        return None
    return path


class JoyReadApplication(QApplication):
    """Capture macOS document events that arrive during QApplication init."""

    def __init__(self, argv: list[str], supported_extensions: Iterable[str]) -> None:
        self._supported_extensions = frozenset(supported_extensions)
        self._startup_file_open_paths: list[Path] = []
        super().__init__(argv)

    def event(self, event: QEvent) -> bool:
        path = _supported_file_open_path(event, self._supported_extensions)
        if path is not None:
            self._startup_file_open_paths.append(path)
            event.accept()
            return True
        return super().event(event)

    def take_startup_file_open_paths(self) -> tuple[Path, ...]:
        paths = tuple(self._startup_file_open_paths)
        self._startup_file_open_paths.clear()
        return paths


class FileOpenRouter(QObject):
    """Queue startup events, then open one window for each requested file."""

    def __init__(self, app: QApplication, supported_extensions: Iterable[str]) -> None:
        super().__init__(app)
        self._app = app
        self._supported_extensions = frozenset(supported_extensions)
        self._pending_paths: list[Path] = []
        self._open_handler: OpenFileHandler | None = None
        app.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self._app:
            path = _supported_file_open_path(event, self._supported_extensions)
            if path is not None:
                self.enqueue(path)
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def enqueue(self, path: str | Path) -> None:
        source = Path(path).expanduser()
        if source.suffix.lower() not in self._supported_extensions:
            logger.warning(
                "Operating-system file-open request was rejected",
                extra={
                    "event": "launch.file_open.rejected",
                    "category": "launch",
                    "status": "rejected",
                    "reason": "unsupported_extension",
                },
            )
            return
        if self._open_handler is None:
            self._pending_paths.append(source)
            logger.debug(
                "Operating-system file-open request queued",
                extra={
                    "event": "launch.file_open.queued",
                    "category": "launch",
                    "status": "pending",
                    "count": len(self._pending_paths),
                },
            )
            return
        self._open(source)

    def take_pending_path(self) -> Path | None:
        if not self._pending_paths:
            return None
        return self._pending_paths.pop(0)

    def set_open_handler(self, handler: OpenFileHandler) -> None:
        self._open_handler = handler
        pending, self._pending_paths = self._pending_paths, []
        for path in pending:
            self._open(path)

    def dispose(self) -> None:
        self._app.removeEventFilter(self)
        self._open_handler = None
        self._pending_paths.clear()

    def _open(self, path: Path) -> None:
        handler = self._open_handler
        if handler is None:
            self._pending_paths.append(path)
            return
        logger.info(
            "Opening file from operating-system request path=%s",
            path,
            extra={
                "event": "launch.file_open.dispatched",
                "category": "launch",
                "status": "finished",
            },
        )
        handler(path)
