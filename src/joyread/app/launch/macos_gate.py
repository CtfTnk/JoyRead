"""macOS launch gate built on ``NSApplicationDidFinishLaunching``.

AppKit delivers a launch document to the application *before* it posts
``NSApplicationDidFinishLaunchingNotification``. That ordering is a platform
guarantee rather than a timing coincidence, which makes the notification an
exact barrier: once it arrives, every document belonging to this launch has
already been converted into a ``QFileOpenEvent`` and buffered.

Measured on macOS 15 with PySide6 6.11, cold launch through LaunchServices:

* one document  -- ``QFileOpenEvent`` at 1881.15 ms, notification at 1881.47 ms
* three documents -- all three events at 415.09-415.34 ms, notification at 415.54 ms
* no document   -- notification at 419.75 ms with nothing buffered
* 1500 ms of blocking work before ``exec()`` -- ordering unchanged, because the
  notification tracks the run loop rather than wall-clock time

``QTimer.singleShot(0, ...)`` is *not* an equivalent barrier and was measured
firing ~20 ms too early on every one of four cold launches. Do not replace this
gate with a zero timer.

The observer must be registered before ``QApplication`` exists. Instantiating
``NSApplication`` here is safe: Qt reuses the existing shared instance.
"""

from __future__ import annotations

from collections.abc import Callable
import logging

from PySide6.QtCore import QTimer


logger = logging.getLogger(__name__)

# Only reachable if AppKit never posts its launch notification, which would mean
# the platform is behaving in a way Apple does not document. The budget is armed
# from the first event-loop iteration, not from process start, so a slow
# AppContext build cannot consume it. Resolving late is bad; never showing a
# window at all is worse.
LAUNCH_NOTIFICATION_BACKSTOP_MS = 3000

DisposeNotifier = Callable[[], None]
#: Register ``callback`` for the launch notification and return its disposer.
NotifierFactory = Callable[[Callable[[], None]], DisposeNotifier]


def _default_notifier(callback: Callable[[], None]) -> DisposeNotifier:
    from AppKit import NSApplication, NSApplicationDidFinishLaunchingNotification
    from Foundation import NSNotificationCenter

    # Force the shared application into existence so the notification has a
    # sender before Qt builds its own delegate around it.
    NSApplication.sharedApplication()
    center = NSNotificationCenter.defaultCenter()
    observer = center.addObserverForName_object_queue_usingBlock_(
        NSApplicationDidFinishLaunchingNotification,
        None,
        None,
        lambda _notification: callback(),
    )

    def dispose() -> None:
        center.removeObserver_(observer)

    return dispose


class MacOSLaunchGate:
    """Resolve when AppKit reports that this launch is fully delivered."""

    def __init__(
        self,
        *,
        notifier_factory: NotifierFactory | None = None,
        backstop_ms: int = LAUNCH_NOTIFICATION_BACKSTOP_MS,
    ) -> None:
        self._backstop_ms = max(0, backstop_ms)
        self._callback: Callable[[], None] | None = None
        self._resolved = False
        self._delivered = False
        self._launched = False
        factory = notifier_factory or _default_notifier
        self._dispose_notifier: DisposeNotifier | None = factory(
            self._handle_did_finish_launching
        )

    @property
    def resolved(self) -> bool:
        return self._resolved

    @property
    def launched(self) -> bool:
        """Whether AppKit has posted its launch notification yet."""

        return self._launched

    def when_ready(self, callback: Callable[[], None]) -> None:
        if self._delivered:
            return
        self._callback = callback
        if self._resolved:
            # The barrier passed while the caller was still building the
            # application context. Deliver now rather than waiting for a
            # notification that has already been and gone.
            self._deliver()
            return
        if self._backstop_ms:
            # Arm from the first event-loop iteration. A timer started here would
            # otherwise start counting during blocking startup work and could
            # expire before AppKit ever gets to run.
            QTimer.singleShot(0, self._arm_backstop)

    def dispose(self) -> None:
        dispose_notifier, self._dispose_notifier = self._dispose_notifier, None
        if dispose_notifier is not None:
            dispose_notifier()

    def _handle_did_finish_launching(self) -> None:
        self._launched = True
        self._resolve()

    def _arm_backstop(self) -> None:
        if self._resolved:
            return
        QTimer.singleShot(self._backstop_ms, self._handle_backstop)

    def _handle_backstop(self) -> None:
        if self._resolved:
            return
        logger.warning(
            "AppKit did not post its launch notification within %d ms; "
            "resolving the launch gate on the backstop timer.",
            self._backstop_ms,
        )
        self._resolve()

    def _resolve(self) -> None:
        """Record that the barrier has passed, and deliver if anyone is waiting."""

        if self._resolved:
            return
        self._resolved = True
        self.dispose()
        self._deliver()

    def _deliver(self) -> None:
        if self._delivered:
            return
        callback, self._callback = self._callback, None
        if callback is None:
            return
        self._delivered = True
        callback()
