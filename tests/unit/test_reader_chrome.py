"""Unit tests for the shared reader chrome controller."""

from __future__ import annotations

import pytest
from types import SimpleNamespace
from PySide6.QtCore import QEvent, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from joyread.ui.views.reader_chrome import AutoHideController
from joyread.ui.views.reader_shell import ReaderShellWidget


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


@pytest.fixture()
def grouped_chrome(host, monkeypatch):
    header, footer, left, right = [QWidget(host) for _ in range(4)]
    controls = (header, footer, left, right)
    for index, control in enumerate(controls):
        control.setGeometry(index * 80, 80, 70, 30)
        control.show()
    slider = {"active": False}
    footer.is_slider_active = lambda: slider["active"]
    shell = SimpleNamespace(header=header, footer=footer, left_arrow=left, right_arrow=right)
    hover = {"widget": None}
    monkeypatch.setattr(QApplication, "widgetAt", lambda _point: hover["widget"])
    controller = AutoHideController(
        host, controls, delay_ms=40, interaction_predicate=lambda: False,
        retained_controls=lambda: ReaderShellWidget._retained_controls(shell),
    )
    return controller, controls, hover, slider


@pytest.mark.parametrize("hover_index, kept", [(0, {0, 1}), (1, {0, 1}), (2, {2}), (3, {3})])
def test_hover_retains_only_the_relevant_group(grouped_chrome, hover_index, kept):
    controller, controls, hover, _slider = grouped_chrome
    # Hit-testing a child button/label must resolve to its containing bar.
    child = QLabel(controls[hover_index])
    child.show()
    hover["widget"] = child
    controller.hide_inactive()
    assert {i for i, control in enumerate(controls) if controller.is_visible(control)} == kept
    assert {i for i, control in enumerate(controls) if control.isVisible()} == kept


@pytest.mark.parametrize("bar_index", [0, 1])
def test_hover_never_wakes_a_hidden_bar(grouped_chrome, bar_index):
    controller, controls, hover, _slider = grouped_chrome
    controller.hide_inactive()
    controller.show((controls[bar_index],), reset_timer=False)
    hover["widget"] = controls[bar_index]
    controller.hide_inactive()
    assert [controller.is_visible(control) for control in controls] == [
        i == bar_index for i in range(4)
    ]


def test_slider_drag_retains_bars_without_retaining_paddles(grouped_chrome):
    controller, controls, _hover, slider = grouped_chrome
    slider["active"] = True
    controller.hide_inactive()
    assert [controller.is_visible(control) for control in controls] == [True, True, False, False]


def test_repeated_activity_on_one_control_does_not_postpone_other_timers(grouped_chrome, qtbot):
    controller, controls, hover, _slider = grouped_chrome
    hover["widget"] = controls[2]
    controller.start()
    receiver = SimpleNamespace(
        auto_hide=controller,
        dialog_overlay=SimpleNamespace(isVisible=lambda: False),
        _start_hide_timer_if_allowed=controller.restart,
    )
    pulse = QTimer(controller)
    pulse.setInterval(5)
    pulse.timeout.connect(lambda: ReaderShellWidget._show_controls(
        receiver, (controls[2],), reset_timer=True
    ))
    pulse.start()
    try:
        qtbot.waitUntil(lambda: all(not controller.is_visible(controls[i]) for i in (0, 1, 3)), timeout=1000)
        assert controller.is_visible(controls[2])
    finally:
        pulse.stop()
    hover["widget"] = None
    qtbot.waitUntil(lambda: not controller.is_visible(controls[2]), timeout=1000)
