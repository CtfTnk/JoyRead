from dataclasses import replace
from time import monotonic

import pytest
from PySide6.QtCore import QObject, QPoint, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from joyread.core.services.library_service import LibraryService
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.views.shelf_view import ShelfView
from joyread.ui.viewmodels.shelf_viewmodel import ShelfViewModel
from joyread.ui.resources.styles.theme import Theme
from tests.support.in_memory_book_repository import InMemoryBookRepository
from tests.unit.test_custom_sorting import sample_books


@pytest.fixture(params=["grid", "list"])
def shelf(request, qtbot):
    vm = ShelfViewModel(LibraryService(InMemoryBookRepository(books=sample_books())))
    vm.load_books()
    vm.set_sort("Custom")
    vm.set_view_mode(request.param)
    widget = ShelfView(vm, ResourceLoader())
    qtbot.addWidget(widget)
    widget.resize(1050, 760)
    widget.show()
    widget.render()
    qtbot.wait(10)
    return widget, vm, widget.grid if request.param == "grid" else widget.list_view


def start_drag(shelf, key="3"):
    widget, vm, surface = shelf
    control = surface.book_controls[key]
    origin = control.mapToGlobal(QPoint(40, 40))
    widget.gestures.begin(surface, key, origin)
    widget.gestures.move(origin + QPoint(QApplication.startDragDistance() + 1, 0))
    assert widget.gestures._mode == "drag"
    return widget.gestures


def test_group_drag_preserves_widgets_and_commits_example(shelf, qtbot):
    widget, vm, surface = shelf
    vm.set_selection({"3", "5", "1"})
    original_widgets = dict(surface.book_controls)
    controller = start_drag(shelf)
    assert controller._moving == ("1", "3", "5")
    assert len(controller._preview._snapshots) == 3
    assert controller._preview._count == 3
    assert all(not surface.book_controls[key].isVisible() for key in controller._moving)
    assert sum(surface._layout.itemAt(i).widget() is controller._placeholder for i in range(surface._layout.count())) == 1
    target = surface.book_controls["4"].mapToGlobal(QPoint(1, 20))
    controller.move(target)
    controller.release(target)
    assert tuple(book.uuid for book in vm.visible_books) == ("2", "1", "3", "5", "4")
    assert surface.book_controls == original_widgets
    assert all(control.isVisible() for control in surface.book_controls.values())
    assert not controller.active


def test_outside_return_then_escape_and_outside_release_restore(shelf):
    widget, vm, surface = shelf
    vm.set_selection({"1", "3"})
    original = tuple(book.uuid for book in vm.visible_books)
    controller = start_drag(shelf)
    outside = widget.mapToGlobal(QPoint(-50, -50))
    controller.move(outside)
    assert controller.active and not controller._preview.isVisible()
    controller.move(surface.viewport().mapToGlobal(QPoint(80, 80)))
    assert controller._preview.isVisible()
    controller.release(outside)
    assert not controller.active
    assert tuple(book.uuid for book in vm.visible_books) == original
    assert vm.selected_book_ids == {"1", "3"}


def test_escape_and_right_button_cancel_without_menu(shelf, qtbot):
    widget, vm, surface = shelf
    vm.set_selection({"1", "3"})
    menus = []
    surface.menu_requested.connect(lambda *args: menus.append(args))
    for cancel in ("escape", "right"):
        controller = start_drag(shelf)
        if cancel == "escape":
            qtbot.keyClick(surface.viewport(), Qt.Key.Key_Escape)
        else:
            qtbot.mousePress(surface.viewport(), Qt.MouseButton.RightButton)
            qtbot.mouseRelease(surface.viewport(), Qt.MouseButton.RightButton)
        assert not controller.active
        qtbot.mouseRelease(surface.viewport(), Qt.MouseButton.LeftButton)
        assert not controller._suppressed
        assert vm.selected_book_ids == {"1", "3"}
    assert not menus


def test_click_rules_are_delayed_until_release(shelf, qtbot):
    widget, vm, surface = shelf
    vm.set_selection({"1", "3"})
    card = surface.book_controls["3"]
    qtbot.mousePress(card, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
    assert vm.selected_book_ids == {"1", "3"}
    qtbot.mouseRelease(card, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
    assert vm.selected_book_ids == {"3"}
    qtbot.mouseClick(card, Qt.MouseButton.LeftButton, stateKey=Qt.KeyboardModifier.ShiftModifier, pos=QPoint(30, 30))
    assert not vm.selected_book_ids


def test_rubber_band_replace_add_shrink_and_cancel(shelf):
    widget, vm, surface = shelf
    vm.set_selection({"5"})
    controller = widget.gestures
    first = surface.book_controls["1"].geometry()
    start = surface._content.mapToGlobal(first.topLeft() - QPoint(4, 4))
    end = surface._content.mapToGlobal(first.center())
    controller.begin(surface, None, start)
    controller.move(end)
    assert vm.selected_book_ids == {"1"}
    controller.cancel()
    assert vm.selected_book_ids == {"5"}
    controller.begin(surface, None, start, additive=True)
    controller.move(end)
    assert vm.selected_book_ids == {"1", "5"}
    controller.move(start + QPoint(1, 1))
    assert vm.selected_book_ids == {"5"}
    controller.release(end)
    assert vm.selected_book_ids == {"1", "5"}


def test_changing_shelf_or_data_cancels_without_saving(shelf):
    widget, vm, surface = shelf
    controller = start_drag(shelf)
    vm.set_current_shelf("favourites")
    assert not controller.active
    assert not vm.sort_saving
    # End the swallowed physical gesture before starting a new one.
    controller._suppressed = False
    controller._release_tracking()


def test_auto_scroll_and_wheel_work_during_drag(shelf):
    widget, vm, surface = shelf
    base = sample_books()[0]
    vm.books = [replace(base, uuid=str(i), title=str(i)) for i in range(1, 41)]
    vm._emit_state()
    QApplication.processEvents()
    vm.set_selection({"1"})
    controller = start_drag(shelf, "1")
    QApplication.processEvents()
    bar = surface.verticalScrollBar()
    assert bar.maximum() > 0
    bottom = surface.viewport().mapToGlobal(QPoint(70, surface.viewport().height() - 10))
    controller.move(bottom)
    controller._last_tick = monotonic() - 0.1
    controller._auto_scroll()
    assert bar.value() > 0
    previous = bar.value()
    wheel = QWheelEvent(surface.viewport().mapFromGlobal(bottom), bottom, QPoint(), QPoint(0, -120),
                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(surface.viewport(), wheel)
    assert bar.value() > previous
    previous = bar.value()
    controller._last_tick = monotonic() - 0.1
    controller._auto_scroll()
    assert bar.value() == previous  # wheel temporarily takes precedence
    controller.cancel()


def test_scroll_edge_zones_and_visible_preview_anchor(shelf, monkeypatch):
    widget, vm, surface = shelf
    base = sample_books()[0]
    vm.books = [replace(base, uuid=str(i), title=str(i)) for i in range(1, 41)]
    vm._emit_state()
    QApplication.processEvents()
    controller = start_drag(shelf, "1")
    QApplication.processEvents()
    pointer = surface.viewport().mapToGlobal(QPoint(70, 80))
    controller.move(pointer)
    preview = controller._preview
    assert preview.mapToGlobal(preview.card_origin) == pointer + QPoint(5, 5)
    assert preview._card_width == Theme.book_card_width * 0.40

    monkeypatch.setattr("joyread.ui.views.shelf_gestures.monotonic", lambda: 100.0)
    bar = surface.verticalScrollBar()
    deltas = {}
    for ratio in (0.0, 0.05, 0.10, 0.20, 0.50, 0.80, 0.90, 0.95, 1.0):
        bar.setValue(bar.maximum() // 2)
        previous = bar.value()
        controller._last_global = surface.viewport().mapToGlobal(
            QPoint(70, round(surface.viewport().rect().bottom() * ratio)))
        controller._last_tick = 99.0  # production code caps elapsed at 100 ms
        controller._scroll_fraction = 0.0
        controller._auto_scroll()
        deltas[ratio] = bar.value() - previous
    assert deltas[0.20] == deltas[0.50] == deltas[0.80] == 0
    assert deltas[0.0] < deltas[0.05] < deltas[0.10] < 0
    assert 0 < deltas[0.90] < deltas[0.95] < deltas[1.0]
    assert deltas[1.0] == -deltas[0.0] == 12
    controller.cancel()


def test_card_buttons_and_double_click_keep_existing_actions(shelf, qtbot):
    widget, vm, surface = shelf
    details, opened = [], []
    # Test signals separately from filesystem opening and floating panel policy.
    surface.detail_requested.disconnect()
    surface.book_opened.disconnect()
    surface.detail_requested.connect(details.append)
    surface.book_opened.connect(opened.append)
    card = surface.book_controls["1"]
    qtbot.mouseClick(card._detail_button, Qt.MouseButton.LeftButton)
    assert details == ["1"] and not widget.gestures.active
    qtbot.mouseDClick(card, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
    assert opened == ["1"]


def test_all_selected_preview_is_cached_and_cancel_does_not_save(shelf, monkeypatch):
    import joyread.ui.widgets.shelf_drag_visuals as visuals
    widget, vm, surface = shelf
    vm.set_selection({"1", "2", "3", "4", "5"})
    calls = []
    blur = visuals.blurred_pixmap
    monkeypatch.setattr(visuals, "blurred_pixmap", lambda *args: (calls.append(1), blur(*args))[1])
    saved = dict(vm._shelf_orders)
    controller = start_drag(shelf)
    assert len(controller._preview._snapshots) == 3
    assert controller._preview._count == 5
    for offset in (10, 40, 80):
        controller.move(surface.viewport().mapToGlobal(QPoint(offset, 50)))
    assert calls == [1]
    QApplication.sendEvent(widget.window(), QEvent(QEvent.Type.WindowDeactivate))
    assert not controller.active
    assert vm._shelf_orders == saved and vm.selected_book_ids == {"1", "2", "3", "4", "5"}


def test_membership_change_cancels_preview_and_shows_new_data(shelf):
    widget, vm, surface = shelf
    controller = start_drag(shelf)
    vm.books = [book for book in vm.books if book.uuid != "2"]
    vm._emit_state()
    assert not controller.active and not vm.sort_saving
    assert "2" not in surface.book_controls
    assert all(control.isVisible() for control in surface.book_controls.values())
    controller._suppressed = False
    controller._release_tracking()


@pytest.mark.parametrize("key", ["1", "3"])
@pytest.mark.parametrize("sort", ["Custom", "Title"])
def test_shift_drag_from_book_adds_rectangle_selection_without_reordering(shelf, key, sort):
    widget, vm, surface = shelf
    vm.set_sort(sort)
    vm.set_selection({"5", "1"})
    saved = dict(vm._shelf_orders)
    origin = surface.book_controls[key].mapToGlobal(QPoint(40, 40))
    controller = widget.gestures
    controller.begin(surface, key, origin, additive=True)
    end = origin + QPoint(QApplication.startDragDistance() + 1, 10)
    controller.move(end)
    assert controller._mode == "rubber"
    assert controller._preview is None and controller._placeholder is None
    assert vm.selected_book_ids == {"1", "5", key}
    controller.cancel()
    assert vm.selected_book_ids == {"1", "5"}
    controller.begin(surface, key, origin, additive=True)
    controller.move(end)
    controller.release(end)
    assert vm.selected_book_ids == {"1", "5", key}
    assert vm._shelf_orders == saved and not vm.sort_saving


def test_plain_click_does_not_take_or_reset_viewport_capture(shelf, qtbot, monkeypatch):
    widget, vm, surface = shelf
    def unexpected(*args):
        pytest.fail('An ordinary click must leave native mouse capture/cursor tracking alone')
    for method in ('grabMouse', 'releaseMouse', 'unsetCursor'):
        monkeypatch.setattr(surface.viewport(), method, unexpected)
    card = surface.book_controls['3']
    qtbot.mousePress(card, Qt.MouseButton.LeftButton, pos=QPoint(30,30))
    assert widget.gestures._mode == 'pressed'
    assert widget.gestures._grabbed_viewport is None
    qtbot.mouseRelease(card, Qt.MouseButton.LeftButton, pos=QPoint(30,30))
    assert vm.selected_book_ids == {'3'}
    assert not widget.gestures.active and not widget.gestures._suppressed


def test_window_release_reaches_qt_before_widget_gesture_consumes_it(shelf):
    widget, vm, surface = shelf
    releases = []

    class WindowEvents(QObject):
        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.MouseButtonRelease:
                releases.append(event.button())
            return False

    # QWidget-only tests bypass QWidgetWindow's implicit-grab bookkeeping.
    # Send through QWindow as the platform does: Qt must receive the release
    # before the controller consumes the translated widget event.
    window = widget.windowHandle()
    observer = WindowEvents(window)
    window.installEventFilter(observer)
    point = widget.mapFromGlobal(surface.book_controls['3'].mapToGlobal(QPoint(30, 30)))
    QTest.mousePress(window, Qt.MouseButton.LeftButton, pos=point)
    assert widget.gestures.active
    QTest.mouseRelease(window, Qt.MouseButton.LeftButton, pos=point)
    assert releases == [Qt.MouseButton.LeftButton]
    assert vm.selected_book_ids == {'3'}
    assert not widget.gestures.active


@pytest.mark.parametrize('cancel', ['escape', 'right'])
def test_cancel_releases_capture_before_trailing_left_release(shelf, qtbot, cancel):
    from PySide6.QtWidgets import QWidget
    widget, vm, surface = shelf
    controller = start_drag(shelf)
    assert controller._grabbed_viewport is surface.viewport()
    if cancel == 'escape':
        qtbot.keyClick(surface.viewport(), Qt.Key.Key_Escape)
    else:
        qtbot.mousePress(surface.viewport(), Qt.MouseButton.RightButton)
        qtbot.mouseRelease(surface.viewport(), Qt.MouseButton.RightButton)
    assert controller._suppressed and not controller.active
    assert controller._grabbed_viewport is None
    assert QWidget.mouseGrabber() is not surface.viewport()
    assert not controller._cursor_overridden
    qtbot.mouseRelease(surface.viewport(), Qt.MouseButton.LeftButton)
    assert not controller._suppressed


def test_fresh_click_after_missing_cancel_release_is_not_swallowed(shelf, qtbot):
    widget, vm, surface = shelf
    controller = start_drag(shelf)
    controller.cancel(suppress_release=True)
    # Simulate a release lost outside the application: the next click must work
    # immediately rather than merely clearing stale cancellation state.
    card = surface.book_controls['5']
    qtbot.mouseClick(card, Qt.MouseButton.LeftButton, pos=QPoint(30,30))
    assert vm.selected_book_ids == {'5'}
    assert not controller.active and not controller._suppressed


def test_new_right_press_after_missing_release_is_not_swallowed(shelf, qtbot):
    widget, vm, surface = shelf
    controller = start_drag(shelf)
    controller.cancel(suppress_release=True)
    qtbot.mousePress(surface.viewport(), Qt.MouseButton.RightButton)
    assert not controller._suppressed and not controller.active
    qtbot.mouseRelease(surface.viewport(), Qt.MouseButton.RightButton)


def test_toolbar_blank_and_title_clear_selection_but_shift_preserves(shelf, qtbot):
    widget, vm, surface = shelf
    toolbar = widget.toolbar
    blank = QPoint((toolbar._title.geometry().right()+toolbar._search_panel.x())//2,
                   toolbar.height()//2)
    vm.set_selection({'1','3'})
    qtbot.mouseClick(toolbar, Qt.MouseButton.LeftButton, pos=blank)
    assert not vm.selected_book_ids
    vm.set_selection({'1','3'})
    qtbot.mouseClick(toolbar, Qt.MouseButton.LeftButton, stateKey=Qt.KeyboardModifier.ShiftModifier, pos=blank)
    assert vm.selected_book_ids == {'1','3'}
    qtbot.mouseClick(toolbar._title, Qt.MouseButton.LeftButton)
    assert not vm.selected_book_ids


def test_toolbar_search_and_tag_button_preserve_selection(shelf, qtbot):
    widget, vm, surface = shelf
    vm.set_selection({'1','3'})
    search = widget.toolbar._search_panel
    qtbot.mouseClick(search._expand_button, Qt.MouseButton.LeftButton)
    assert vm.selected_book_ids == {'1','3'}
    qtbot.mouseClick(search._input, Qt.MouseButton.LeftButton)
    qtbot.keyClicks(search._input, 'test')
    assert search.query == 'test' and vm.selected_book_ids == {'1','3'}
    requested = []
    widget.tag_filter_requested.connect(lambda: requested.append(True))
    qtbot.mouseClick(widget.toolbar._tag_filter_button, Qt.MouseButton.LeftButton)
    assert requested == [True] and vm.selected_book_ids == {'1','3'}
    assert not widget.gestures.active
