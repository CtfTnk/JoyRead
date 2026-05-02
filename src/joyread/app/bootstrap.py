"""Qt application bootstrap."""

from __future__ import annotations

import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from joyread.app.app_context import AppContext, create_app_context
from joyread.infrastructure.logging.logging_service import configure_logging
from joyread.ui.views.main_window import MainWindow


def create_application(argv: list[str] | None = None) -> tuple[QApplication, AppContext, MainWindow]:
    app = QApplication(argv or sys.argv)
    context = create_app_context()
    context.paths.ensure_directories()
    configure_logging(context.paths.paths.logs)

    app.setApplicationName(context.config.app_name)
    app.setOrganizationName(context.config.app_author)
    app.setWindowIcon(QIcon(str(context.resources.app_icon_path())))
    app.setStyleSheet(context.resources.load_stylesheet())

    window = MainWindow(context)
    return app, context, window


def run(argv: list[str] | None = None) -> int:
    app, _context, window = create_application(argv)
    window.show()
    return app.exec()
