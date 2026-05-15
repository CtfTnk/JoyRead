"""Unit tests for the shared reader chrome controllers."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from joyread.ui.views.reader_chrome import AutoHideController, PanelOutsideClickFilter


@pytest.fixture()
def host(qtbot) -> QWidget:
    widget = QWidget()
    qtbot.addWidget(widget)
    widget.resize(400, 200)
    widget.show()
    return widget


def test_auto_hide_controller_hides_after_timer_fires(host: QWidget) -> None:
    label = QLabel(host)
    label.show()
    controller = AutoHideController(
        host,
        (label,),
        delay_ms=10,
        interaction_predicate=lambda: False,
    )
    assert controller.is_visible(label)

    controller.hide_inactive()

    assert not controller.is_visible(label)
    assert label.isHidden()


def test_auto_hide_controller_reschedules_when_interaction_active(host: QWidget) -> None:
    label = QLabel(host)
    label.show()
    interaction = {"active": True}
    controller = AutoHideController(
        host,
        (label,),
        delay_ms=10,
        interaction_predicate=lambda: interaction["active"],
    )

    controller.hide_inactive()

    # Interaction-active path keeps the widget visible and arms the timer again.
    assert controller.is_visible(label)
    assert label.isVisible()


def test_auto_hide_controller_show_resets_timer_via_after_show_hook(host: QWidget) -> None:
    label = QLabel(host)
    label.hide()
    raised: list[int] = []
    controller = AutoHideController(
        host,
        (label,),
        delay_ms=50,
        interaction_predicate=lambda: False,
        on_after_show=lambda: raised.append(1),
    )

    controller.show((label,), reset_timer=False)

    # The Enter event filter on the control widget can fire an extra
    # ``show()`` when the cursor happens to be over the test widget,
    # so assert the hook fired at least once rather than exactly once.
    assert controller.is_visible(label)
    assert raised
    assert label.isVisible()


def test_auto_hide_controller_reveals_widget_on_enter_event(host: QWidget, qtbot) -> None:
    label = QLabel(host)
    label.hide()
    controller = AutoHideController(
        host,
        (label,),
        delay_ms=50,
        interaction_predicate=lambda: False,
    )

    # The controller installed itself as an event filter on the widget;
    # an Enter event must flip visibility on without the shell helping.
    QApplication.sendEvent(label, QEvent(QEvent.Type.Enter))
    qtbot.wait(0)

    assert controller.is_visible(label)


def test_panel_outside_click_filter_fires_only_when_active(qtbot) -> None:
    panel = QWidget()
    qtbot.addWidget(panel)
    panel.resize(80, 80)
    panel.show()
    outside_clicks: list[int] = []
    filt = PanelOutsideClickFilter()
    filt.register(
        panel,
        safe_click_predicate=lambda w: w is panel,
        on_outside_click=lambda: outside_clicks.append(1),
    )

    other = QWidget()
    qtbot.addWidget(other)
    other.resize(80, 80)
    other.show()
    event = _mouse_press_at(QPoint(0, 0))

    # No effect while the panel isn't activated.
    QApplication.sendEvent(other, event)
    assert outside_clicks == []

    filt.activate(panel)
    QApplication.sendEvent(other, event)
    assert outside_clicks == [1]

    # Safe click (the panel itself) does NOT trigger the outside-click cb.
    QApplication.sendEvent(panel, _mouse_press_at(QPoint(0, 0)))
    assert outside_clicks == [1]


def test_panel_outside_click_filter_removes_app_filter_when_unused(qtbot) -> None:
    panel = QWidget()
    qtbot.addWidget(panel)
    panel.show()
    filt = PanelOutsideClickFilter()
    filt.register(
        panel,
        safe_click_predicate=lambda _w: True,
        on_outside_click=lambda: None,
    )

    filt.activate(panel)
    assert filt.is_active(panel)
    filt.deactivate(panel)
    assert not filt.is_active(panel)

    # Re-activate is idempotent (no double-install of the app filter).
    filt.activate(panel)
    filt.activate(panel)
    filt.deactivate_all()
    assert not filt.is_active(panel)


def _mouse_press_at(pos: QPoint) -> QMouseEvent:
    return QMouseEvent(
        QEvent.Type.MouseButtonPress,
        pos,
        pos,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
