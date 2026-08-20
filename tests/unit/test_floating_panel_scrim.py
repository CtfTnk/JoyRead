"""Unit tests for FloatingPanelScrim, the input-capture backdrop for
non-modal floating panels."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QWidget

from joyread.ui.views.floating_panel_scrim import FloatingPanelScrim


def test_mouse_press_is_swallowed_and_invokes_the_dismiss_callback(qtbot) -> None:
    scrim = FloatingPanelScrim()
    qtbot.addWidget(scrim)
    scrim.resize(200, 200)
    scrim.show()
    dismissed: list[int] = []
    scrim.set_dismiss_callback(lambda: dismissed.append(1))

    qtbot.mouseClick(scrim, Qt.MouseButton.LeftButton)

    assert dismissed == [1]


def test_wheel_event_is_swallowed_without_invoking_the_dismiss_callback(qtbot) -> None:
    scrim = FloatingPanelScrim()
    qtbot.addWidget(scrim)
    scrim.resize(200, 200)
    scrim.show()
    dismissed: list[int] = []
    scrim.set_dismiss_callback(lambda: dismissed.append(1))

    event = QWheelEvent(
        QPointF(10, 10),
        QPointF(10, 10),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    scrim.wheelEvent(event)

    assert event.isAccepted()
    assert dismissed == []


class _AcceptingWidget(QWidget):
    """Stands in for a real floating panel: its own controls accept clicks
    rather than leaving the base QWidget default, which ignores them and
    lets Qt bubble the event up to the parent -- exactly the propagation a
    real panel's buttons/inputs don't exhibit."""

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 - Qt override
        event.accept()


def test_a_child_widget_above_the_scrim_absorbs_its_own_clicks(qtbot) -> None:
    scrim = FloatingPanelScrim()
    qtbot.addWidget(scrim)
    scrim.resize(200, 200)
    scrim.show()
    dismissed: list[int] = []
    scrim.set_dismiss_callback(lambda: dismissed.append(1))

    panel = _AcceptingWidget(scrim)
    panel.setGeometry(50, 50, 60, 60)
    panel.show()
    panel.raise_()

    qtbot.mouseClick(panel, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))

    assert dismissed == []


def test_scrim_has_no_focus_policy(qtbot) -> None:
    scrim = FloatingPanelScrim()
    qtbot.addWidget(scrim)

    assert scrim.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_dismiss_callback_can_be_cleared(qtbot) -> None:
    scrim = FloatingPanelScrim()
    qtbot.addWidget(scrim)
    scrim.resize(200, 200)
    scrim.show()
    dismissed: list[int] = []
    scrim.set_dismiss_callback(lambda: dismissed.append(1))
    scrim.set_dismiss_callback(None)

    qtbot.mouseClick(scrim, Qt.MouseButton.LeftButton)

    assert dismissed == []


def test_click_on_the_drag_handle_starts_a_window_drag_instead_of_dismissing(qtbot) -> None:
    scrim = FloatingPanelScrim()
    qtbot.addWidget(scrim)
    scrim.resize(200, 200)
    scrim.show()
    dismissed: list[int] = []
    scrim.set_dismiss_callback(lambda: dismissed.append(1))

    handle = QWidget(scrim)
    handle.setGeometry(0, 0, 200, 40)
    handle.show()
    scrim.set_drag_handle(handle)

    qtbot.mouseClick(scrim, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))

    assert dismissed == []


def test_click_off_the_drag_handle_still_dismisses(qtbot) -> None:
    scrim = FloatingPanelScrim()
    qtbot.addWidget(scrim)
    scrim.resize(200, 200)
    scrim.show()
    dismissed: list[int] = []
    scrim.set_dismiss_callback(lambda: dismissed.append(1))

    handle = QWidget(scrim)
    handle.setGeometry(0, 0, 200, 40)
    handle.show()
    scrim.set_drag_handle(handle)

    qtbot.mouseClick(scrim, Qt.MouseButton.LeftButton, pos=QPoint(10, 150))

    assert dismissed == [1]
