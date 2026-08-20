"""Unit tests for the shared window-drag-through-overlay helper."""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QToolButton, QWidget

from joyread.ui.views.window_drag import start_window_drag_if_on_drag_handle


def _press_at(global_pos: QPointF, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> QMouseEvent:
    return QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        global_pos,
        global_pos,
        button,
        button,
        Qt.KeyboardModifier.NoModifier,
    )


def test_returns_false_when_drag_handle_is_none(qtbot) -> None:
    window = QWidget()
    qtbot.addWidget(window)
    window.show()

    event = _press_at(window.mapToGlobal(window.rect().center()).toPointF())

    assert start_window_drag_if_on_drag_handle(event, None) is False


def test_returns_false_when_press_is_outside_the_drag_handle(qtbot) -> None:
    window = QWidget()
    qtbot.addWidget(window)
    window.resize(400, 400)
    window.show()
    handle = QWidget(window)
    handle.setGeometry(0, 0, 400, 50)
    handle.show()

    outside = window.mapToGlobal(window.rect().center()).toPointF()
    event = _press_at(outside)

    assert start_window_drag_if_on_drag_handle(event, handle) is False


def test_returns_true_when_press_lands_on_the_drag_handle(qtbot) -> None:
    window = QWidget()
    qtbot.addWidget(window)
    window.resize(400, 400)
    window.show()
    handle = QWidget(window)
    handle.setGeometry(0, 0, 400, 50)
    handle.show()

    inside = handle.mapToGlobal(handle.rect().center()).toPointF()
    event = _press_at(inside)

    assert start_window_drag_if_on_drag_handle(event, handle) is True


def test_returns_false_when_press_lands_on_a_drag_handle_child(qtbot) -> None:
    window = QWidget()
    qtbot.addWidget(window)
    window.resize(400, 400)
    window.show()
    handle = QWidget(window)
    handle.setGeometry(0, 0, 400, 50)
    handle.show()
    button = QToolButton(handle)
    button.setGeometry(10, 10, 24, 24)
    button.show()

    inside_button = button.mapToGlobal(button.rect().center()).toPointF()
    event = _press_at(inside_button)

    assert start_window_drag_if_on_drag_handle(event, handle) is False


def test_returns_false_for_a_non_left_button_press(qtbot) -> None:
    window = QWidget()
    qtbot.addWidget(window)
    window.resize(400, 400)
    window.show()
    handle = QWidget(window)
    handle.setGeometry(0, 0, 400, 50)
    handle.show()

    inside = handle.mapToGlobal(handle.rect().center()).toPointF()
    event = _press_at(inside, button=Qt.MouseButton.RightButton)

    assert start_window_drag_if_on_drag_handle(event, handle) is False
