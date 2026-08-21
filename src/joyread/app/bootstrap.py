"""Qt application bootstrap.

This module is the composition root. It wires the launch gate, instance
arbitration, the application context, and window ownership together, but owns
no policy of its own: what the first window should be lives in
:mod:`joyread.app.launch.coordinator`, and which windows outlive which lives in
:mod:`joyread.app.windows.ownership`.
"""

from __future__ import annotations

import logging
import platform
import sys
from collections.abc import Callable
from pathlib import Path
from dataclasses import dataclass
from threading import Thread

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QMessageBox

from joyread.app.app_context import AppContext, create_app_context
from joyread.app.launch.coordinator import LaunchCoordinator
from joyread.app.launch.file_open_router import FileOpenRouter, JoyReadApplication
from joyread.app.launch.intent import (
    LaunchIntent,
    intent_from_arguments,
    merge_open_intents,
)
from joyread.app.launch.macos_reopen_bridge import MacOSReopenBridge
from joyread.app.launch.ready_gate import (
    ImmediateLaunchGate,
    LaunchReadyGate,
    create_launch_gate,
)
from joyread.app.launch.single_instance_broker import (
    InstanceRole,
    SingleInstanceBroker,
    SingleInstanceError,
)
from joyread.app.windows.manager import (
    ApplicationWindowManager,
    center_window_on_launch,
)
from joyread.app.windows.novel_provider import NovelReaderProvider
from joyread.core.file_types import EPUB_ACCESS_ENABLED
from joyread.core.reader import SUPPORTED_READER_EXTENSIONS
from joyread.core.tag_indexing import warm_romanizers
from joyread.core.services.storage_recovery_service import (
    StorageRecoveryCancelled,
    StorageRecoveryPromptResult,
)
from joyread.infrastructure.config.app_config import AppConfig
from joyread.infrastructure.config.settings_store import (
    SettingsStore,
    create_environment_settings_store,
)
from joyread.infrastructure.logging import (
    configure_early_logging,
    configure_logging,
    log_event,
    shutdown_logging,
    write_emergency_log,
)
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.dialogs.storage_recovery_dialog import (
    StorageRecoveryDialog,
    StorageRecoveryDialogResult,
)


logger = logging.getLogger(__name__)

__all__ = ["center_window_on_launch", "create_application", "run"]

# Upper bound on how long a secondary process waits for the platform to finish
# describing its launch before forwarding. It must exceed the macOS gate's own
# backstop so the gate resolves itself first; this only guards against a gate
# that never calls back at all, which would otherwise hang a doomed process.
SECONDARY_LAUNCH_WAIT_MS = 5000


@dataclass(frozen=True)
class _StartupEnvironment:
    argv: list[str]
    app: QApplication
    config: AppConfig
    settings_store: SettingsStore
    gate: LaunchReadyGate
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
    platform launch gate. Here the gate always resolves synchronously so the
    caller gets its window back from this call.
    """

    environment = _prepare_startup_environment(argv, gate=ImmediateLaunchGate())
    _configure_primary_logging(environment)
    runtime = _build_primary_runtime(environment)
    _manager, coordinator = _configure_window_management(
        runtime,
        gate=environment.gate,
        enable_macos_reopen=False,
    )
    window = coordinator.initial_window
    if window is None:
        raise RuntimeError("Immediate application creation did not produce a window.")
    return runtime.app, runtime.context, window


def _prepare_startup_environment(
    argv: list[str] | None = None,
    *,
    gate: LaunchReadyGate | None = None,
) -> _StartupEnvironment:
    resolved_argv = list(sys.argv if argv is None else argv)
    configure_early_logging()
    log_event(
        logger,
        logging.INFO,
        "process.starting",
        "JoyRead process starting",
        category="process",
        status="started",
        count=max(0, len(resolved_argv) - 1),
        outcome=f"platform={platform.system()} python={platform.python_version()}",
    )
    # The gate has to exist before QApplication: on macOS it observes a
    # notification that Qt's own startup triggers.
    resolved_gate = gate if gate is not None else create_launch_gate()
    app = _create_qt_application(resolved_argv)
    config = AppConfig()
    settings_store = create_environment_settings_store(config.app_name, config.app_author)
    app.setApplicationName(config.app_name)
    app.setOrganizationName(config.app_author)
    return _StartupEnvironment(
        argv=resolved_argv,
        app=app,
        config=config,
        settings_store=settings_store,
        gate=resolved_gate,
        startup_intent=intent_from_arguments(resolved_argv[1:]),
    )


def _build_primary_runtime(environment: _StartupEnvironment) -> _ApplicationRuntime:
    app = environment.app
    previous_router = getattr(app, "_joyread_file_open_router", None)
    if isinstance(previous_router, FileOpenRouter):
        previous_router.dispose()
    file_open_router = FileOpenRouter(app, SUPPORTED_READER_EXTENSIONS)
    setattr(app, "_joyread_file_open_router", file_open_router)
    # Documents delivered while QApplication was still being constructed predate
    # the router's event filter, so hand them over explicitly.
    for path in _take_startup_document_paths(app):
        file_open_router.enqueue(path)

    app.setQuitOnLastWindowClosed(True)
    context = create_app_context(
        recovery_prompt=_prompt_storage_recovery,
        config=environment.config,
        settings_store=environment.settings_store,
    )
    context.paths.ensure_directories()

    app.setWindowIcon(QIcon(str(context.resources.app_icon_path())))
    _load_application_fonts(context.resources)
    app.setStyleSheet(context.resources.load_stylesheet())
    _warm_tag_romanizers()
    app.aboutToQuit.connect(_log_about_to_quit)
    app.aboutToQuit.connect(context.close)
    app.aboutToQuit.connect(_shutdown_application_logging)

    return _ApplicationRuntime(
        app=app,
        context=context,
        file_open_router=file_open_router,
        initial_intent=environment.startup_intent,
    )


def _create_novel_reader_provider() -> NovelReaderProvider | None:
    """Wire the novel reader in, or leave the app without one.

    This is the single place the novel feature enters the application, and
    the import is deliberately inside the gate: with ``EPUB_ACCESS_ENABLED``
    off, ``joyread.novel`` is never imported, so neither it nor ``lxml`` needs
    to be installed at all. Flipping the flag is the whole release switch.
    """

    if not EPUB_ACCESS_ENABLED:
        return None
    from joyread.novel.app.provider import create_novel_reader_provider

    return create_novel_reader_provider()


def _configure_window_management(
    runtime: _ApplicationRuntime,
    *,
    gate: LaunchReadyGate,
    broker: SingleInstanceBroker | None = None,
    enable_macos_reopen: bool,
) -> tuple[ApplicationWindowManager, LaunchCoordinator]:
    # create_application() is re-entrant (tests and embedded callers invoke it
    # repeatedly against one shared QApplication). Unlike SingleInstanceBroker
    # and LaunchCoordinator, which each refuse a second start(), this
    # composition root has no such guard -- so, matching the file_open_router
    # handling just above, dispose whatever manager/coordinator a previous
    # call left parented to `app` rather than orphaning it as a still-wired
    # QObject that outlives its own caller.
    previous_manager = getattr(runtime.app, "_joyread_window_manager", None)
    if isinstance(previous_manager, ApplicationWindowManager):
        previous_manager.deleteLater()
    previous_coordinator = getattr(runtime.app, "_joyread_launch_coordinator", None)
    if isinstance(previous_coordinator, LaunchCoordinator):
        previous_coordinator.deleteLater()

    manager = ApplicationWindowManager(
        runtime.context,
        novel_reader_provider=_create_novel_reader_provider(),
        parent=runtime.app,
    )
    coordinator = LaunchCoordinator(windows=manager, gate=gate, parent=runtime.app)
    setattr(runtime.app, "_joyread_window_manager", manager)
    setattr(runtime.app, "_joyread_launch_coordinator", coordinator)

    if enable_macos_reopen and platform.system() == "Darwin":
        bridge = MacOSReopenBridge(parent=runtime.app)
        bridge.reopen_requested.connect(manager.handle_reopen)
        # Installing before the first window exists would let a reopen race the
        # launch decision and build a Library the launch was about to replace.
        coordinator.settled.connect(lambda: _install_macos_reopen_bridge(bridge))
        runtime.app.aboutToQuit.connect(bridge.dispose)
        setattr(runtime.app, "_joyread_macos_reopen_bridge", bridge)

    # The router and the broker must be connected before the gate can resolve,
    # so that a document arriving during startup reaches the coordinator's
    # buffer rather than being dropped.
    runtime.file_open_router.set_open_handler(coordinator.open_file)
    if broker is not None:
        broker.set_intent_handler(_broker_intent_handler(coordinator))
    coordinator.start(runtime.initial_intent)
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
    log_event(
        logger,
        logging.INFO,
        "process.qt_quit_requested",
        "Qt aboutToQuit signal received",
        category="process",
        status="stopping",
    )


def _shutdown_application_logging() -> None:
    log_event(
        logger,
        logging.INFO,
        "process.stopped",
        "JoyRead process lifecycle finished",
        category="process",
        status="finished",
    )
    shutdown_logging()


def _configure_primary_logging(environment: _StartupEnvironment) -> None:
    result = configure_logging(environment.settings_store.support_root / "Logs")
    log_event(
        logger,
        logging.INFO,
        "process.primary_logging_ready",
        "Primary-process logging configured",
        category="process",
        status="finished",
        outcome="file" if result.file_logging_enabled else "stderr_only",
    )


def run(argv: list[str] | None = None) -> int:
    environment = _prepare_startup_environment(argv)
    broker = SingleInstanceBroker(
        environment.settings_store.support_root,
        application_id=environment.config.application_id,
        parent=environment.app,
    )
    try:
        role = broker.start(lambda: _resolve_secondary_intent(environment))
    except SingleInstanceError as exc:
        logger.error("Single-instance startup failed: %s", exc)
        write_emergency_log(
            environment.settings_store.support_root / "Logs",
            "launch.single_instance.failed",
            "JoyRead could not establish or contact the primary process",
            error=exc,
        )
        broker.dispose()
        _show_startup_error(str(exc))
        shutdown_logging()
        return 2
    if role == InstanceRole.SECONDARY:
        log_event(
            logger,
            logging.INFO,
            "launch.secondary_forwarded",
            "Secondary process forwarded its launch intent",
            category="launch",
            status="finished",
        )
        broker.dispose()
        shutdown_logging()
        return 0

    _configure_primary_logging(environment)
    log_event(
        logger,
        logging.INFO,
        "launch.primary_acquired",
        "Primary process acquired the single-instance role",
        category="launch",
        status="finished",
    )
    setattr(environment.app, "_joyread_single_instance_broker", broker)
    environment.app.aboutToQuit.connect(broker.dispose)
    try:
        runtime = _build_primary_runtime(environment)
    except StorageRecoveryCancelled:
        log_event(
            logger,
            logging.INFO,
            "process.startup_cancelled",
            "JoyRead startup cancelled during storage recovery",
            category="process",
            status="cancelled",
            reason="storage_recovery",
        )
        _dispose_file_open_router(environment.app)
        broker.dispose()
        shutdown_logging()
        return 0
    except Exception as exc:
        log_event(
            logger,
            logging.CRITICAL,
            "process.startup_failed",
            "JoyRead primary runtime failed to initialize",
            category="process",
            status="failed",
            error_type=type(exc).__name__,
            exc_info=True,
        )
        _dispose_file_open_router(environment.app)
        broker.dispose()
        shutdown_logging()
        raise

    _configure_window_management(
        runtime,
        gate=environment.gate,
        broker=broker,
        enable_macos_reopen=True,
    )
    log_event(
        logger,
        logging.INFO,
        "process.ready",
        "JoyRead primary runtime is ready",
        category="process",
        status="finished",
    )
    return runtime.app.exec()


def _take_startup_document_paths(app: QApplication) -> tuple[Path, ...]:
    """Drain documents ``JoyReadApplication`` buffered before a router existed."""

    take = getattr(app, "take_startup_file_open_paths", None)
    return tuple(take()) if callable(take) else ()


def _resolve_secondary_intent(environment: _StartupEnvironment) -> LaunchIntent:
    """Decide what a secondary process forwards to the primary.

    A secondary exits before ``AppContext`` exists, so it cannot hand the
    decision to ``LaunchCoordinator``. It still faces the same platform problem:
    on macOS the document it was launched to open has not arrived yet, because
    Finder only delivers it once an event loop runs. Forwarding argv alone would
    silently turn "open this manga" into "show the Library".

    So the secondary waits on the same gate the primary uses. It can afford to:
    it opens no storage and is about to exit either way.
    """

    app = environment.app
    resolved = False
    loop = QEventLoop()

    def on_ready() -> None:
        nonlocal resolved
        resolved = True
        loop.quit()

    environment.gate.when_ready(on_ready)
    if not resolved:
        # Windows and Linux resolve synchronously and never reach this loop.
        QTimer.singleShot(SECONDARY_LAUNCH_WAIT_MS, loop.quit)
        loop.exec()

    startup_paths = _take_startup_document_paths(app)
    document_intent = LaunchIntent.open_files(startup_paths) if startup_paths else None
    intent = merge_open_intents(environment.startup_intent, document_intent)
    if intent is None:
        logger.info("Forwarding a plain launch to the primary process")
        return LaunchIntent.show_library()
    logger.info("Forwarding %d document(s) to the primary process", len(intent.paths))
    return intent


def _install_macos_reopen_bridge(bridge: MacOSReopenBridge) -> None:
    try:
        bridge.install()
    except Exception:
        logger.exception("Unable to install the macOS reopen bridge")


def _broker_intent_handler(
    coordinator: LaunchCoordinator,
) -> Callable[[LaunchIntent], None]:
    """Adapt the coordinator's result-bearing API to the broker's command sink."""

    def handle(intent: LaunchIntent) -> None:
        coordinator.submit(intent)

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


def _warm_tag_romanizers() -> None:
    """Build the CJK reading dictionaries off the UI thread.

    Measured at ~110ms. Left lazy, that lands on the UI thread the first time
    a library with Japanese or Chinese tags opens a tag surface, which is a
    visible stall on a dialog the user just asked for. A plain daemon thread
    is enough: the work touches no Qt object and nothing waits on it, so a
    quit mid-warm simply abandons it.
    """

    Thread(
        target=warm_romanizers,
        name="joyread-tag-romanizer-warmup",
        daemon=True,
    ).start()
