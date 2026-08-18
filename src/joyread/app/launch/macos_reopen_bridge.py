"""Exact macOS reopen Apple Event bridge, loaded only on Darwin.

Installing a handler for ``aevt``/``rapp`` displaces AppKit's default one, so
this bridge also has to reproduce the part of the default behaviour users
depend on: a hidden application unhides when its Dock icon is clicked. What the
reopen should then *do* is policy, and lives in
:mod:`joyread.app.windows.activation`; this module only reports the event.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

from PySide6.QtCore import QObject, Signal as QtSignal


logger = logging.getLogger(__name__)

_CORE_EVENT_CLASS = int.from_bytes(b"aevt", "big")
_REOPEN_APPLICATION_EVENT = int.from_bytes(b"rapp", "big")
_HANDLER_SELECTOR = b"handleJoyReadReopen:withReplyEvent:"
_cocoa_handler_class: type | None = None

EventManagerFactory = Callable[[], Any]
HandlerFactory = Callable[[Callable[[], None]], tuple[Any, bytes]]
UnhideCallback = Callable[[], None]


def _default_unhide() -> None:
    """Restore a hidden application, as AppKit's own reopen handler would."""

    from AppKit import NSApplication

    application = NSApplication.sharedApplication()
    if application.isHidden():
        application.unhide_(None)


class MacOSReopenBridge(QObject):
    """Convert the native reopen event into an application-level Qt signal."""

    reopen_requested = QtSignal()

    def __init__(
        self,
        *,
        event_manager_factory: EventManagerFactory | None = None,
        handler_factory: HandlerFactory | None = None,
        unhide: UnhideCallback | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._event_manager_factory = event_manager_factory or _default_event_manager
        self._handler_factory = handler_factory or _default_handler
        self._unhide = unhide or _default_unhide
        self._event_manager: Any | None = None
        self._handler: Any | None = None
        self._selector: bytes | None = None
        self._installed = False

    @property
    def installed(self) -> bool:
        return self._installed

    def install(self) -> None:
        if self._installed:
            return
        manager = self._event_manager_factory()
        handler, selector = self._handler_factory(self._emit_reopen)
        manager.setEventHandler_andSelector_forEventClass_andEventID_(
            handler,
            selector,
            _CORE_EVENT_CLASS,
            _REOPEN_APPLICATION_EVENT,
        )
        self._event_manager = manager
        self._handler = handler
        self._selector = selector
        self._installed = True
        logger.info("macOS reopen Apple Event bridge installed")

    def dispose(self) -> None:
        if not self._installed:
            return
        manager = self._event_manager
        if manager is not None:
            manager.removeEventHandlerForEventClass_andEventID_(
                _CORE_EVENT_CLASS,
                _REOPEN_APPLICATION_EVENT,
            )
        self._event_manager = None
        self._handler = None
        self._selector = None
        self._installed = False

    def _emit_reopen(self) -> None:
        try:
            self._unhide()
        except Exception:  # pragma: no cover - AppKit safety net.
            logger.exception("Unable to unhide the application on reopen")
        self.reopen_requested.emit()


def _default_event_manager() -> Any:
    from AppKit import NSAppleEventManager

    return NSAppleEventManager.sharedAppleEventManager()


def _default_handler(callback: Callable[[], None]) -> tuple[Any, bytes]:
    global _cocoa_handler_class
    if _cocoa_handler_class is None:
        import objc
        from Foundation import NSObject

        def handle_reopen(self: Any, _event: Any, _reply: Any) -> None:
            try:
                self._joyread_callback()
            except Exception:  # pragma: no cover - AppKit callback safety net.
                logger.exception("macOS reopen callback failed")

        class JoyReadReopenEventHandler(NSObject):
            # The Apple Event manager expects a void method with two object args.
            handleJoyReadReopen_withReplyEvent_ = objc.selector(
                handle_reopen,
                selector=_HANDLER_SELECTOR,
                signature=b"v@:@@",
            )

        _cocoa_handler_class = JoyReadReopenEventHandler

    handler = _cocoa_handler_class.alloc().init()
    handler._joyread_callback = callback
    return handler, _HANDLER_SELECTOR
