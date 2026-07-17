"""Qt application bootstrap."""

from __future__ import annotations

import logging
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow

from joyread.app.app_context import AppContext, create_app_context
from joyread.app.file_open_router import FileOpenRouter, JoyReadApplication
from joyread.app.startup_window_coordinator import StartupWindowCoordinator
from joyread.core.file_types import EPUB_EXTENSIONS
from joyread.core.reader import SUPPORTED_READER_EXTENSIONS
from joyread.core.services.storage_recovery_service import (
    StorageRecoveryCancelled,
    StorageRecoveryPromptResult,
)
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.infrastructure.logging.logging_service import configure_early_logging, configure_logging
from joyread.ui.dialogs.storage_recovery_dialog import (
    StorageRecoveryDialog,
    StorageRecoveryDialogResult,
)
from joyread.ui.views.main_window import MainWindow
from joyread.ui.views.novel_reader_window import NovelReaderWindow
from joyread.ui.views.reader_window import ReaderWindow


logger = logging.getLogger(__name__)

MACOS_FILE_OPEN_GRACE_MS = 250


@dataclass(frozen=True)
class _ApplicationRuntime:
    app: QApplication
    context: AppContext
    file_open_router: FileOpenRouter
    direct_path: Path | None


def create_application(argv: list[str] | None = None) -> tuple[QApplication, AppContext, QMainWindow]:
    runtime = _create_application_runtime(argv)
    if runtime.direct_path is not None:
        logger.info("Direct-reader launch path=%s", runtime.direct_path)
        window = _create_standalone_reader_window(runtime.context, runtime.direct_path)
    else:
        logger.info("Main window launch")
        window = MainWindow(runtime.context)

    runtime.file_open_router.set_open_handler(
        lambda path: _show_external_reader_window(runtime.context, path)
    )
    return runtime.app, runtime.context, window


def _create_application_runtime(argv: list[str] | None = None) -> _ApplicationRuntime:
    argv = argv or sys.argv
    # Early stderr logging so AppContext setup (migrations, service init) is
    # visible. Once the writable logs path is known we upgrade to the rotating
    # file handler. Both configure calls are idempotent on the root logger.
    configure_early_logging()
    logger.info(
        "JoyRead starting pid=%d platform=%s python=%s argv=%s",
        os.getpid(),
        platform.platform(),
        platform.python_version(),
        argv[1:],
    )
    app = _create_qt_application(argv)
    previous_router = getattr(app, "_joyread_file_open_router", None)
    if isinstance(previous_router, FileOpenRouter):
        previous_router.dispose()
    file_open_router = FileOpenRouter(app, SUPPORTED_READER_EXTENSIONS)
    setattr(app, "_joyread_file_open_router", file_open_router)
    if isinstance(app, JoyReadApplication):
        for path in app.take_startup_file_open_paths():
            file_open_router.enqueue(path)

    app.setQuitOnLastWindowClosed(True)
    context = create_app_context(recovery_prompt=_prompt_storage_recovery)
    context.paths.ensure_directories()
    configure_logging(context.paths.paths.logs)

    app.setApplicationName(context.config.app_name)
    app.setOrganizationName(context.config.app_author)
    app.setWindowIcon(QIcon(str(context.resources.app_icon_path())))
    _load_application_fonts(context.resources)
    app.setStyleSheet(context.resources.load_stylesheet())
    app.aboutToQuit.connect(_log_about_to_quit)
    app.aboutToQuit.connect(context.close)

    # Process any native open-document event queued while services and storage
    # were initialized. A command-line path wins if both channels report the
    # same launch request.
    app.processEvents()
    direct_path = _direct_reader_path(argv[1:])
    if direct_path is not None:
        file_open_router.discard_pending(direct_path)
    else:
        direct_path = file_open_router.take_pending_path()
    return _ApplicationRuntime(
        app=app,
        context=context,
        file_open_router=file_open_router,
        direct_path=direct_path,
    )


def _create_qt_application(argv: list[str]) -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return existing
    return JoyReadApplication(argv, SUPPORTED_READER_EXTENSIONS)


def _create_standalone_reader_window(context: AppContext, path: Path) -> QMainWindow:
    if path.suffix.lower() in EPUB_EXTENSIONS:
        return NovelReaderWindow(context, path)
    return ReaderWindow(context, path)


def _show_external_reader_window(context: AppContext, path: Path) -> QMainWindow:
    window = _create_standalone_reader_window(context, path)
    _present_window(window)
    return window


def _prompt_storage_recovery(current: str, message: str) -> StorageRecoveryPromptResult:
    """Ask the user to initialize the default library or select an existing one.

    Shown before the main window and the stylesheet exist, so this uses a
    small parentless ``QDialog`` rather than the in-app themed overlay.
    """

    logger.info("Prompting storage recovery for unavailable location %s", current)
    while True:
        dialog = StorageRecoveryDialog(current, message)
        result = dialog.exec()

        if result == StorageRecoveryDialogResult.INITIALIZE:
            logger.info("Storage recovery: user chose initialize for %s", current)
            return StorageRecoveryPromptResult.initialize()
        if result == StorageRecoveryDialogResult.SELECT:
            directory = QFileDialog.getExistingDirectory(
                None,
                "Select an existing JoyRead library",
                current,
            )
            if directory:
                logger.info("Storage recovery: user selected candidate %s", directory)
                return StorageRecoveryPromptResult.select(directory)
            logger.info("Storage recovery: select cancelled; returning to prompt")
            continue

        logger.info("Storage recovery: user closed recovery dialog for %s", current)
        return StorageRecoveryPromptResult.quit()


def _log_about_to_quit() -> None:
    logger.info("Qt aboutToQuit signal received")


def run(argv: list[str] | None = None) -> int:
    try:
        runtime = _create_application_runtime(argv)
    except StorageRecoveryCancelled:
        logger.info("JoyRead startup cancelled during storage recovery")
        app = QApplication.instance()
        router = getattr(app, "_joyread_file_open_router", None) if app is not None else None
        if isinstance(router, FileOpenRouter):
            router.dispose()
        return 0

    grace_ms = (
        MACOS_FILE_OPEN_GRACE_MS
        if platform.system() == "Darwin" and runtime.direct_path is None
        else 0
    )
    coordinator = StartupWindowCoordinator(
        create_main_window=lambda: MainWindow(runtime.context),
        create_reader_window=lambda path: _create_standalone_reader_window(runtime.context, path),
        present_window=_present_window,
        file_open_grace_ms=grace_ms,
        parent=runtime.app,
    )
    setattr(runtime.app, "_joyread_startup_window_coordinator", coordinator)
    coordinator.start(runtime.direct_path)
    runtime.file_open_router.set_open_handler(coordinator.open_file)
    return runtime.app.exec()


def _present_window(window: QMainWindow) -> None:
    center_window_on_launch(window)
    window.show()
    window.raise_()
    window.activateWindow()


def center_window_on_launch(window: QMainWindow) -> None:
    """Place the initial top-level window in the center of its launch screen."""
    screen = window.screen() or QApplication.primaryScreen()
    if screen is None:
        return

    window_geometry = window.frameGeometry()
    if window_geometry.isNull() or window_geometry.width() <= 0 or window_geometry.height() <= 0:
        window_geometry = window.geometry()
    if window_geometry.isNull() or window_geometry.width() <= 0 or window_geometry.height() <= 0:
        return

    window_geometry.moveCenter(screen.availableGeometry().center())
    window.move(window_geometry.topLeft())


def _load_application_fonts(resources: ResourceLoader) -> None:
    for path in resources.font_paths():
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))


def _direct_reader_path(arguments: list[str]) -> Path | None:
    for argument in arguments:
        path = Path(argument).expanduser()
        if path.suffix.lower() in SUPPORTED_READER_EXTENSIONS:
            return path
    return None
