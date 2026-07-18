"""Qt application bootstrap."""

from __future__ import annotations

import logging
import os
import platform
import sys
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QMessageBox

from joyread.app.app_context import AppContext, create_app_context
from joyread.app.application_window_manager import (
    ApplicationWindowManager,
    center_window_on_launch,
)
from joyread.app.file_open_router import FileOpenRouter, JoyReadApplication
from joyread.app.launch_intent import (
    LaunchIntent,
    intent_from_arguments,
    merge_open_intents,
)
from joyread.app.macos_reopen_bridge import MacOSReopenBridge
from joyread.app.single_instance_broker import (
    InstanceRole,
    SingleInstanceBroker,
    SingleInstanceError,
)
from joyread.app.startup_window_coordinator import StartupWindowCoordinator
from joyread.core.reader import SUPPORTED_READER_EXTENSIONS
from joyread.core.services.storage_recovery_service import (
    StorageRecoveryCancelled,
    StorageRecoveryPromptResult,
)
from joyread.infrastructure.config.app_config import AppConfig
from joyread.infrastructure.config.settings_store import (
    SettingsStore,
    create_environment_settings_store,
)
from joyread.infrastructure.logging.logging_service import configure_early_logging, configure_logging
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.dialogs.storage_recovery_dialog import (
    StorageRecoveryDialog,
    StorageRecoveryDialogResult,
)


logger = logging.getLogger(__name__)

MACOS_FILE_OPEN_GRACE_MS = 250


@dataclass(frozen=True)
class _StartupEnvironment:
    argv: list[str]
    app: QApplication
    config: AppConfig
    settings_store: SettingsStore
    startup_intent: LaunchIntent | None


@dataclass(frozen=True)
class _ApplicationRuntime:
    app: QApplication
    context: AppContext
    file_open_router: FileOpenRouter
    initial_intent: LaunchIntent | None


def create_application(argv: list[str] | None = None) -> tuple[QApplication, AppContext, QMainWindow]:
    """Create a deterministic immediate window for tests and embedded callers.

    Production uses :func:`run`, which adds single-instance arbitration and the
    macOS Finder grace period before constructing the first top-level window.
    """

    environment = _prepare_startup_environment(argv)
    runtime = _build_primary_runtime(environment)
    _manager, coordinator = _configure_window_management(
        runtime,
        file_open_grace_ms=0,
        enable_macos_reopen=False,
    )
    window = coordinator.initial_window
    if window is None:
        raise RuntimeError("Immediate application creation did not produce a window.")
    return runtime.app, runtime.context, window


def _prepare_startup_environment(argv: list[str] | None = None) -> _StartupEnvironment:
    resolved_argv = list(sys.argv if argv is None else argv)
    configure_early_logging()
    logger.info(
        "JoyRead starting pid=%d platform=%s python=%s argv=%s",
        os.getpid(),
        platform.platform(),
        platform.python_version(),
        resolved_argv[1:],
    )
    app = _create_qt_application(resolved_argv)
    config = AppConfig()
    settings_store = create_environment_settings_store(config.app_name, config.app_author)
    app.setApplicationName(config.app_name)
    app.setOrganizationName(config.app_author)
    argument_intent = intent_from_arguments(resolved_argv[1:])
    startup_event_intent = None
    if isinstance(app, JoyReadApplication):
        # macOS may deliver the first Finder document while QApplication is
        # being constructed. Capture it before instance arbitration so a
        # secondary process forwards OPEN_FILES rather than SHOW_LIBRARY.
        app.processEvents()
        startup_paths = app.take_startup_file_open_paths()
        if startup_paths:
            startup_event_intent = LaunchIntent.open_files(startup_paths)
    return _StartupEnvironment(
        argv=resolved_argv,
        app=app,
        config=config,
        settings_store=settings_store,
        startup_intent=merge_open_intents(argument_intent, startup_event_intent),
    )


def _build_primary_runtime(environment: _StartupEnvironment) -> _ApplicationRuntime:
    app = environment.app
    previous_router = getattr(app, "_joyread_file_open_router", None)
    if isinstance(previous_router, FileOpenRouter):
        previous_router.dispose()
    file_open_router = FileOpenRouter(app, SUPPORTED_READER_EXTENSIONS)
    setattr(app, "_joyread_file_open_router", file_open_router)
    if isinstance(app, JoyReadApplication):
        for path in app.take_startup_file_open_paths():
            file_open_router.enqueue(path)

    app.setQuitOnLastWindowClosed(True)
    context = create_app_context(
        recovery_prompt=_prompt_storage_recovery,
        config=environment.config,
        settings_store=environment.settings_store,
    )
    context.paths.ensure_directories()
    configure_logging(context.paths.paths.logs)

    app.setWindowIcon(QIcon(str(context.resources.app_icon_path())))
    _load_application_fonts(context.resources)
    app.setStyleSheet(context.resources.load_stylesheet())
    app.aboutToQuit.connect(_log_about_to_quit)
    app.aboutToQuit.connect(context.close)

    # Merge every startup channel. Resolving and de-duplicating here handles
    # platforms that report the same document in argv and QFileOpenEvent.
    app.processEvents()
    pending_paths = file_open_router.take_pending_paths()
    pending_intent = LaunchIntent.open_files(pending_paths) if pending_paths else None
    initial_intent = merge_open_intents(environment.startup_intent, pending_intent)
    return _ApplicationRuntime(
        app=app,
        context=context,
        file_open_router=file_open_router,
        initial_intent=initial_intent,
    )


def _configure_window_management(
    runtime: _ApplicationRuntime,
    *,
    file_open_grace_ms: int,
    broker: SingleInstanceBroker | None = None,
    enable_macos_reopen: bool,
) -> tuple[ApplicationWindowManager, StartupWindowCoordinator]:
    manager = ApplicationWindowManager(runtime.context, parent=runtime.app)
    coordinator = StartupWindowCoordinator(
        show_library=manager.show_library,
        open_files=manager.open_files,
        file_open_grace_ms=file_open_grace_ms,
        parent=runtime.app,
    )
    setattr(runtime.app, "_joyread_window_manager", manager)
    setattr(runtime.app, "_joyread_startup_window_coordinator", coordinator)

    if enable_macos_reopen and platform.system() == "Darwin":
        bridge = MacOSReopenBridge(parent=runtime.app)
        bridge.reopen_requested.connect(
            lambda: coordinator.handle_intent(LaunchIntent.show_library())
        )
        coordinator.settled.connect(lambda: _install_macos_reopen_bridge(bridge))
        runtime.app.aboutToQuit.connect(bridge.dispose)
        setattr(runtime.app, "_joyread_macos_reopen_bridge", bridge)

    coordinator.start(runtime.initial_intent)
    runtime.file_open_router.set_open_handler(coordinator.open_file)
    if broker is not None:
        broker.set_intent_handler(_broker_intent_handler(coordinator))
    return manager, coordinator


def _create_qt_application(argv: list[str]) -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return existing
    return JoyReadApplication(argv, SUPPORTED_READER_EXTENSIONS)


def _prompt_storage_recovery(current: str, message: str) -> StorageRecoveryPromptResult:
    """Ask the user to initialize the default library or select an existing one."""

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
    environment = _prepare_startup_environment(argv)
    broker = SingleInstanceBroker(
        environment.settings_store.support_root,
        application_id=environment.config.application_id,
        parent=environment.app,
    )
    secondary_intent = environment.startup_intent or LaunchIntent.show_library()
    try:
        role = broker.start(secondary_intent)
    except SingleInstanceError as exc:
        logger.error("Single-instance startup failed: %s", exc)
        broker.dispose()
        _show_startup_error(str(exc))
        return 2
    if role == InstanceRole.SECONDARY:
        broker.dispose()
        return 0

    setattr(environment.app, "_joyread_single_instance_broker", broker)
    environment.app.aboutToQuit.connect(broker.dispose)
    try:
        runtime = _build_primary_runtime(environment)
    except StorageRecoveryCancelled:
        logger.info("JoyRead startup cancelled during storage recovery")
        _dispose_file_open_router(environment.app)
        broker.dispose()
        return 0
    except Exception:
        _dispose_file_open_router(environment.app)
        broker.dispose()
        raise

    grace_ms = (
        MACOS_FILE_OPEN_GRACE_MS
        if platform.system() == "Darwin" and runtime.initial_intent is None
        else 0
    )
    _configure_window_management(
        runtime,
        file_open_grace_ms=grace_ms,
        broker=broker,
        enable_macos_reopen=True,
    )
    return runtime.app.exec()


def _install_macos_reopen_bridge(bridge: MacOSReopenBridge) -> None:
    try:
        bridge.install()
    except Exception:
        logger.exception("Unable to install the macOS reopen bridge")


def _broker_intent_handler(
    coordinator: StartupWindowCoordinator,
) -> Callable[[LaunchIntent], None]:
    """Adapt the coordinator's result-bearing API to the broker's command sink."""

    def handle(intent: LaunchIntent) -> None:
        coordinator.handle_intent(intent)

    return handle


def _show_startup_error(message: str) -> None:
    QMessageBox.critical(None, "JoyRead", message)


def _dispose_file_open_router(app: QApplication) -> None:
    router = getattr(app, "_joyread_file_open_router", None)
    if isinstance(router, FileOpenRouter):
        router.dispose()


def _load_application_fonts(resources: ResourceLoader) -> None:
    for path in resources.font_paths():
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
