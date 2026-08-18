"""When has the operating system finished describing *this* launch?

Windows and Linux answer synchronously: the document a user picked with
"Open With" is in ``sys.argv`` before Python runs, so the launch is fully
described at process start.

macOS does not. Finder delivers the document asynchronously as a
``QFileOpenEvent``, using the same channel whether the process is cold-starting
or already running. Deciding the first window from ``sys.argv`` alone therefore
shows the Library, and the document then opens a Reader on top of it.

A gate names the moment that ambiguity ends, so window policy never has to
guess a duration. See :mod:`joyread.app.launch.macos_gate` for the macOS
implementation and the platform signal it waits on.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import platform
from typing import Protocol


logger = logging.getLogger(__name__)

ReadyCallback = Callable[[], None]


class LaunchReadyGate(Protocol):
    """Report the moment the launch is fully described.

    Implementations must invoke the callback exactly once. Invoking it
    synchronously from :meth:`when_ready` is allowed and expected on platforms
    whose launch is complete at process start.
    """

    def when_ready(self, callback: ReadyCallback) -> None: ...


class ImmediateLaunchGate:
    """Resolve synchronously, for platforms whose argv already says everything.

    This is also the degraded macOS path. If the platform signal is
    unavailable, resolving immediately reproduces JoyRead's historical
    behaviour -- the Library opens, and a Finder document opens a Reader
    alongside it -- which is a worse experience but never a stalled one.
    """

    def __init__(self) -> None:
        self._resolved = False

    @property
    def resolved(self) -> bool:
        return self._resolved

    def when_ready(self, callback: ReadyCallback) -> None:
        if self._resolved:
            return
        self._resolved = True
        callback()


def create_launch_gate() -> LaunchReadyGate:
    """Build the gate for the current platform.

    Must be called before ``QApplication`` is constructed: the macOS gate has to
    observe a notification that Qt itself triggers during startup.
    """

    if platform.system() != "Darwin":
        return ImmediateLaunchGate()

    try:
        from joyread.app.launch.macos_gate import MacOSLaunchGate
    except Exception:
        # PyObjC is a declared macOS dependency, so this means a broken or
        # trimmed install rather than an expected configuration.
        logger.warning(
            "macOS launch signal unavailable; falling back to an immediate "
            "launch gate. A Finder document will open a Reader alongside the "
            "Library instead of replacing it.",
            exc_info=True,
        )
        return ImmediateLaunchGate()

    try:
        return MacOSLaunchGate()
    except Exception:
        logger.warning(
            "macOS launch signal could not be installed; falling back to an "
            "immediate launch gate.",
            exc_info=True,
        )
        return ImmediateLaunchGate()
