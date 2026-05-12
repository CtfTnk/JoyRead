"""Qt application bootstrap."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication, QMainWindow

from joyread.app.app_context import AppContext, create_app_context
from joyread.core.archive.service import ARCHIVE_EXTENSIONS
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.infrastructure.logging.logging_service import configure_logging
from joyread.ui.views.main_window import MainWindow
from joyread.ui.views.reader_window import ReaderWindow


def create_application(argv: list[str] | None = None) -> tuple[QApplication, AppContext, QMainWindow]:
    argv = argv or sys.argv
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
    app.aboutToQuit.connect(context.close)

    direct_path = _direct_reader_path(argv[1:])
    if direct_path is not None:
        # OS "Open With" should go straight to reader-only mode. Do not show
        # the bookshelf or a QFileDialog here; macOS native panel helpers are
        # only expected when the user explicitly opens an in-app file picker.
        window = ReaderWindow(context, direct_path)
    else:
        window = MainWindow(context)
    return app, context, window


def run(argv: list[str] | None = None) -> int:
    app, _context, window = create_application(argv)
    window.show()
    return app.exec()


def _load_application_fonts(resources: ResourceLoader) -> None:
    for path in resources.font_paths():
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))


def _direct_reader_path(arguments: list[str]) -> Path | None:
    for argument in arguments:
        path = Path(argument).expanduser()
        if path.suffix.lower() in ARCHIVE_EXTENSIONS:
            return path
    return None
