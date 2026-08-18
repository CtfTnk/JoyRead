from __future__ import annotations

from typing import Any

from joyread.app.launch.macos_reopen_bridge import MacOSReopenBridge


class _EventManager:
    def __init__(self) -> None:
        self.installed: tuple[Any, bytes, int, int] | None = None
        self.removed: tuple[int, int] | None = None

    def setEventHandler_andSelector_forEventClass_andEventID_(
        self,
        handler: Any,
        selector: bytes,
        event_class: int,
        event_id: int,
    ) -> None:
        self.installed = (handler, selector, event_class, event_id)

    def removeEventHandlerForEventClass_andEventID_(
        self,
        event_class: int,
        event_id: int,
    ) -> None:
        self.removed = (event_class, event_id)


def test_reopen_bridge_installs_emits_and_disposes(qtbot) -> None:
    manager = _EventManager()
    callbacks: list[object] = []

    def create_handler(callback):  # noqa: ANN001
        callbacks.append(callback)
        return object(), b"handleJoyReadReopen:withReplyEvent:"

    bridge = MacOSReopenBridge(
        event_manager_factory=lambda: manager,
        handler_factory=create_handler,
    )
    received: list[bool] = []
    bridge.reopen_requested.connect(lambda: received.append(True))

    bridge.install()
    bridge.install()
    assert bridge.installed
    assert manager.installed is not None
    assert len(callbacks) == 1

    callbacks[0]()  # type: ignore[operator]
    assert received == [True]

    installed = manager.installed
    bridge.dispose()
    assert not bridge.installed
    assert manager.removed == installed[2:]

