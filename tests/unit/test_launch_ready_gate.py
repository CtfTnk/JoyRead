from __future__ import annotations

from collections.abc import Callable

from joyread.app.launch import ready_gate
from joyread.app.launch.macos_gate import MacOSLaunchGate
from joyread.app.launch.ready_gate import ImmediateLaunchGate, create_launch_gate


class _Notifier:
    """Stand-in for the AppKit launch notification."""

    def __init__(self) -> None:
        self.callback: Callable[[], None] | None = None
        self.disposed = False

    def register(self, callback: Callable[[], None]) -> Callable[[], None]:
        self.callback = callback
        return self._dispose

    def fire(self) -> None:
        assert self.callback is not None
        self.callback()

    def _dispose(self) -> None:
        self.disposed = True


def test_immediate_gate_resolves_synchronously_exactly_once() -> None:
    gate = ImmediateLaunchGate()
    calls: list[int] = []

    gate.when_ready(lambda: calls.append(1))
    gate.when_ready(lambda: calls.append(2))

    assert calls == [1]
    assert gate.resolved


def test_macos_gate_resolves_on_the_launch_notification(qtbot) -> None:
    notifier = _Notifier()
    gate = MacOSLaunchGate(notifier_factory=notifier.register, backstop_ms=0)
    calls: list[int] = []

    gate.when_ready(lambda: calls.append(1))
    assert calls == []
    assert not gate.launched

    notifier.fire()

    assert calls == [1]
    assert gate.resolved
    assert notifier.disposed


def test_macos_gate_resolves_immediately_if_launch_already_finished(qtbot) -> None:
    """AppKit may finish launching while AppContext is still being built."""

    notifier = _Notifier()
    gate = MacOSLaunchGate(notifier_factory=notifier.register, backstop_ms=0)
    notifier.fire()

    calls: list[int] = []
    gate.when_ready(lambda: calls.append(1))

    assert calls == [1]


def test_macos_gate_never_fires_its_callback_twice(qtbot) -> None:
    notifier = _Notifier()
    gate = MacOSLaunchGate(notifier_factory=notifier.register, backstop_ms=0)
    calls: list[int] = []
    gate.when_ready(lambda: calls.append(1))

    notifier.fire()
    notifier.fire()

    assert calls == [1]


def test_macos_gate_backstop_resolves_when_the_notification_never_arrives(qtbot) -> None:
    notifier = _Notifier()
    gate = MacOSLaunchGate(notifier_factory=notifier.register, backstop_ms=10)
    calls: list[int] = []

    gate.when_ready(lambda: calls.append(1))
    qtbot.waitUntil(lambda: bool(calls), timeout=2000)

    assert calls == [1]
    assert gate.resolved
    assert not gate.launched


def test_non_darwin_platforms_use_the_immediate_gate(monkeypatch) -> None:
    monkeypatch.setattr(ready_gate.platform, "system", lambda: "Windows")

    assert isinstance(create_launch_gate(), ImmediateLaunchGate)


def test_a_broken_macos_gate_falls_back_instead_of_stalling_startup(monkeypatch) -> None:
    """Without the platform signal JoyRead must still open a window."""

    monkeypatch.setattr(ready_gate.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        "joyread.app.launch.macos_gate.MacOSLaunchGate",
        _unavailable_gate,
    )

    assert isinstance(create_launch_gate(), ImmediateLaunchGate)


def _unavailable_gate(*_args: object, **_kwargs: object) -> object:
    raise RuntimeError("AppKit unavailable")
