import pytest
from PySide6.QtCore import QPoint, QRect, QSize, QTimer, Qt
from PySide6.QtWidgets import QApplication, QWidget

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.core.models.language import Language
from joyread.ui.widgets.menus import FigmaMenu, LanguageDropdownMenu
from joyread.ui.widgets.popup_geometry import popup_position


@pytest.mark.parametrize("point,expected", [
    ((20, 20), (20, 20)), ((490, 20), (360, 20)),
    ((20, 390), (20, 190)), ((490, 390), (360, 190)),
])
def test_context_menu_flips_each_axis(point, expected):
    assert popup_position(QPoint(*point), QSize(130, 200), QRect(0, 0, 500, 400)) == QPoint(*expected)


def test_negative_screen_coordinates_anchor_and_oversized_clamping():
    bounds = QRect(-1000, -200, 800, 600)
    anchor = QRect(-240, 340, 30, 30)
    assert popup_position(anchor.bottomLeft(), QSize(130, 100), bounds, anchor) == QPoint(-340, 240)
    assert popup_position(QPoint(-999, -199), QSize(900, 700), bounds) == bounds.topLeft()


def test_bounds_intersect_partially_offscreen_owner(qtbot, monkeypatch):
    owner = QWidget()
    qtbot.addWidget(owner)
    owner.resize(400, 300)
    owner.show()
    origin = owner.mapToGlobal(QPoint())
    available = QRect(origin + QPoint(100, 50), QSize(800, 600))
    class Screen:
        def availableGeometry(self):
            return available
    monkeypatch.setattr(QApplication, "screenAt", lambda _: Screen())
    menu = FigmaMenu(owner)
    qtbot.addWidget(menu)
    assert menu._available_bounds(origin) == QRect(origin + QPoint(106, 56), QSize(288, 238))


@pytest.mark.parametrize("text", ["A very long action description", "这是一个很长的菜单项目", "とても長いメニュー項目の説明"])
def test_narrow_menu_elides_text_but_preserves_tooltip(qtbot, text):
    QApplication.instance().setStyleSheet(ResourceLoader().load_stylesheet())
    owner = QWidget()
    qtbot.addWidget(owner)
    menu = FigmaMenu(owner)
    qtbot.addWidget(menu)
    row = menu.add_item(text, lambda: None)
    menu.ensurePolished()
    menu._fit_to_bounds(QRect(0, 0, 85, 180))
    menu.show()
    qtbot.wait(1)
    from joyread.ui.widgets.elided_label import ElidedLabel
    label = row.findChild(ElidedLabel)
    assert menu.width() == 85 and label.toolTip() == text
    assert label.text() != text
    menu.close()


@pytest.mark.parametrize("kind", ["normal", "language"])
def test_menu_fits_window_and_last_item_remains_reachable(qtbot, kind):
    QApplication.instance().setStyleSheet(ResourceLoader().load_stylesheet())
    owner = QWidget()
    qtbot.addWidget(owner)
    owner.resize(300, 150)
    owner.show()
    if kind == "normal":
        menu = FigmaMenu(owner)
        for index in range(30):
            menu.add_item(f"Action {index}", lambda: None)
    else:
        menu = LanguageDropdownMenu(owner, ResourceLoader())
        for index in range(30):
            menu.add_language(Language(f"Language {index}", f"x{index}"), lambda _: None)
    observed = []
    def inspect():
        bounds = menu._available_bounds(owner.mapToGlobal(QPoint(290, 140)))
        observed.append(bounds.contains(menu.geometry()))
        bar = menu._scroll_area.verticalScrollBar()
        bar.setValue(bar.maximum())
        last = menu._option_layout.itemAt(29).widget()
        last_rect = QRect(last.mapTo(menu._scroll_area.viewport(), QPoint()), last.size())
        observed.append(menu._scroll_area.viewport().rect().contains(last_rect))
        observed.append(bar.maximum() > 0 and bar.isVisible())
        menu.close()
    QTimer.singleShot(20, inspect)
    menu.exec(owner.mapToGlobal(QPoint(290, 140)))
    assert observed == [True, True, True]


@pytest.mark.parametrize("action", ["move", "resize", "escape"])
def test_owner_geometry_change_or_escape_closes_menu(qtbot, action):
    owner = QWidget()
    qtbot.addWidget(owner)
    owner.resize(400, 300)
    owner.show()
    menu = FigmaMenu(owner)
    menu.add_item("Read", lambda: None)
    closed = []
    menu.closed.connect(lambda: closed.append(True))
    def change():
        if action == "move":
            owner.move(owner.pos() + QPoint(10, 10))
        elif action == "resize":
            owner.resize(420, 320)
        else:
            qtbot.keyClick(menu, Qt.Key.Key_Escape)
    QTimer.singleShot(20, change)
    # A broken close must fail the assertion rather than hang the suite.
    watchdog = QTimer(owner)
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(menu.close)
    watchdog.start(1000)
    menu.exec(owner.mapToGlobal(QPoint(20, 20)))
    assert watchdog.isActive() and closed == [True]
    watchdog.stop()
