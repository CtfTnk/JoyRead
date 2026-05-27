"""Qt application bootstrap."""

from __future__ import annotations

import logging
import os
import platform
import sys
from pathlib import Path

from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication, QMainWindow

from joyread.app.app_context import AppContext, create_app_context
from joyread.core.reader import SUPPORTED_READER_EXTENSIONS
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.infrastructure.logging.logging_service import configure_early_logging, configure_logging
from joyread.ui.views.main_window import MainWindow
from joyread.ui.views.reader_window import ReaderWindow


logger = logging.getLogger(__name__)


def create_application(argv: list[str] | None = None) -> tuple[QApplication, AppContext, QMainWindow]:
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
    app = QApplication.instance() or QApplication(argv)
    app.setQuitOnLastWindowClosed(True)
    context = create_app_context()
    context.paths.ensure_directories()
    configure_logging(context.paths.paths.logs)

    app.setApplicationName(context.config.app_name)
    app.setOrganizationName(context.config.app_author)
    app.setWindowIcon(QIcon(str(context.resources.app_icon_path())))
    _load_application_fonts(context.resources)
    app.setStyleSheet(context.resources.load_stylesheet())
    app.aboutToQuit.connect(_log_about_to_quit)
    app.aboutToQuit.connect(context.close)

    direct_path = _direct_reader_path(argv[1:])
    if direct_path is not None:
        # OS "Open With" should go straight to reader-only mode. Do not show
        # the bookshelf or a QFileDialog here; macOS native panel helpers are
        # only expected when the user explicitly opens an in-app file picker.
        logger.info("Direct-reader launch path=%s", direct_path)
        window = ReaderWindow(context, direct_path)
    else:
        logger.info("Main window launch")
        window = MainWindow(context)
    return app, context, window


def _log_about_to_quit() -> None:
    logger.info("Qt aboutToQuit signal received")


def run(argv: list[str] | None = None) -> int:
    app, _context, window = create_application(argv)
    center_window_on_launch(window)
    window.show()
    return app.exec()


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
