from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QWidget

from joyread.ui.viewmodels.signals import Signal


class _Receiver(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def handle(self) -> None:
        self.calls += 1


def test_signal_keeps_plain_callable_subscribers() -> None:
    signal: Signal[str] = Signal()
    received: list[str] = []

    signal.connect(received.append)
    signal.emit("ready")

    assert received == ["ready"]


def test_signal_deduplicates_and_disconnects_bound_methods(qtbot) -> None:
    signal: Signal[None] = Signal()
    receiver = _Receiver()
    qtbot.addWidget(receiver)

    signal.connect(receiver.handle)
    signal.connect(receiver.handle)
    signal.emit()
    signal.disconnect(receiver.handle)
    signal.emit()

    assert receiver.calls == 1


def test_signal_drops_deleted_qt_bound_method(qtbot) -> None:
    signal: Signal[None] = Signal()
    receiver = _Receiver()
    qtbot.addWidget(receiver)

    signal.connect(receiver.handle)
    receiver.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    signal.emit()

    assert signal._subscribers == []
