from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from joyread.app.app_context import create_app_context
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.views.main_window import MainWindow
from joyread.ui.widgets.dialogs import DialogTextButton, JoyReadDialogOverlay, JoyReadDialogPanel


def apply_theme() -> None:
    app = QApplication.instance()
    assert app is not None
    app.setStyleSheet(ResourceLoader().load_stylesheet())


def test_dialog_panel_matches_figma_frame_and_button_geometry(qtbot) -> None:
    apply_theme()
    panel = JoyReadDialogPanel()
    qtbot.addWidget(panel)
    panel.set_confirm("Title", "content", "Cancel", "Confirm")
    panel.show()
    QApplication.processEvents()

    margins = panel.layout().contentsMargins()
    title_area = panel.findChild(QWidget, "JoyReadDialogTitleArea")
    content_area = panel.findChild(QWidget, "JoyReadDialogContentArea")
    option_area = panel.findChild(QWidget, "JoyReadDialogOptionArea")
    buttons = panel.findChildren(DialogTextButton)

    assert panel.sizeHint().width() == Theme.dialog_width
    assert panel.sizeHint().height() == Theme.dialog_height
    assert panel.width() == Theme.dialog_width
    assert panel.height() == Theme.dialog_height
    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (
        Theme.dialog_layout_margin,
        Theme.dialog_layout_margin,
        Theme.dialog_layout_margin,
        Theme.dialog_layout_margin,
    )
    assert panel.layout().spacing() == Theme.dialog_gap

    assert title_area is not None
    assert content_area is not None
    assert option_area is not None
    content_margins = content_area.layout().contentsMargins()
    assert (content_margins.left(), content_margins.top(), content_margins.right(), content_margins.bottom()) == (
        Theme.dialog_content_padding,
        Theme.dialog_content_padding,
        Theme.dialog_content_padding,
        Theme.dialog_content_padding,
    )
    assert option_area.layout().spacing() == Theme.dialog_option_gap
    assert [button.text for button in buttons] == ["Cancel", "Confirm"]
    assert {(button.width(), button.height()) for button in buttons} == {
        (Theme.dialog_button_width, Theme.dialog_button_height)
    }
    assert buttons[1].x() > buttons[0].x()
    assert buttons[0].y() == buttons[1].y()


def test_dialog_overlay_centers_panel_and_tracks_resize(qtbot) -> None:
    apply_theme()
    root = QWidget()
    qtbot.addWidget(root)
    root.resize(Theme.window_width, Theme.window_height)
    overlay = JoyReadDialogOverlay(root)
    overlay.setGeometry(0, 0, root.width(), root.height())
    root.show()
    overlay.show_info("Title", "content")
    QApplication.processEvents()

    assert overlay.isVisible()
    assert overlay.panel.geometry().center() == overlay.rect().center()

    overlay.resize(900, 600)
    QApplication.processEvents()

    assert overlay.panel.geometry().center() == overlay.rect().center()


def test_dialog_overlay_only_closes_from_buttons(qtbot) -> None:
    apply_theme()
    root = QWidget()
    qtbot.addWidget(root)
    root.resize(Theme.window_width, Theme.window_height)
    overlay = JoyReadDialogOverlay(root)
    overlay.setGeometry(0, 0, root.width(), root.height())
    root.show()
    overlay.show_info("Title", "content")
    QApplication.processEvents()

    qtbot.keyClick(overlay, Qt.Key.Key_Escape)
    QApplication.processEvents()
    assert overlay.isVisible()

    qtbot.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
    QApplication.processEvents()
    assert overlay.isVisible()

    button = overlay.panel.findChildren(DialogTextButton)[0]
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert overlay.isHidden()


def test_dialog_confirm_cancel_callbacks_close_overlay(qtbot) -> None:
    apply_theme()
    root = QWidget()
    qtbot.addWidget(root)
    root.resize(Theme.window_width, Theme.window_height)
    overlay = JoyReadDialogOverlay(root)
    overlay.setGeometry(0, 0, root.width(), root.height())
    root.show()

    result: list[str] = []
    overlay.show_confirm(
        "Title",
        "content",
        on_confirm=lambda: result.append("confirm"),
        on_cancel=lambda: result.append("cancel"),
    )
    QApplication.processEvents()
    qtbot.mouseClick(overlay.panel.findChildren(DialogTextButton)[0], Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert overlay.isHidden()
    assert result == ["cancel"]

    overlay.show_confirm(
        "Title",
        "content",
        on_confirm=lambda: result.append("confirm"),
        on_cancel=lambda: result.append("cancel"),
    )
    QApplication.processEvents()
    qtbot.mouseClick(overlay.panel.findChildren(DialogTextButton)[1], Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert overlay.isHidden()
    assert result == ["cancel", "confirm"]


def test_main_window_uses_global_dialog_for_placeholder_messages(qtbot) -> None:
    apply_theme()
    window = MainWindow(create_app_context())
    qtbot.addWidget(window)
    window.resize(Theme.window_width, Theme.window_height)
    window.show()
    QApplication.processEvents()

    window._handle_navigation("new_collection")
    QApplication.processEvents()

    title_labels = [
        label.text()
        for label in window.dialog_overlay.panel.findChildren(QLabel)
        if label.property("class") == "JoyReadDialogTitle"
    ]
    assert window.dialog_overlay.isVisible()
    assert window.dialog_overlay.geometry().getRect() == (
        0,
        0,
        window.centralWidget().width(),
        window.centralWidget().height(),
    )
    assert window.dialog_overlay.panel.geometry().center() == window.dialog_overlay.rect().center()
    assert title_labels == ["New Collection"]


def test_main_window_uses_global_confirm_dialog_for_delete(qtbot, monkeypatch) -> None:
    apply_theme()
    monkeypatch.setenv("JOYREAD_USE_MOCK_REPOSITORY", "1")
    context = create_app_context()
    window = MainWindow(context)
    qtbot.addWidget(window)
    window.resize(Theme.window_width, Theme.window_height)
    window.show()
    QApplication.processEvents()

    book = context.shelf_viewmodel.books[0]
    window._confirm_delete_books((book.uuid,))
    QApplication.processEvents()

    title_labels = [
        label.text()
        for label in window.dialog_overlay.panel.findChildren(QLabel)
        if label.property("class") == "JoyReadDialogTitle"
    ]
    buttons = [button.text for button in window.dialog_overlay.panel.findChildren(DialogTextButton)]

    assert window.dialog_overlay.isVisible()
    assert title_labels == ["Delete Book"]
    assert buttons == ["Cancel", "Delete"]
    context.close()


def test_stylesheet_resolves_dialog_tokens() -> None:
    stylesheet = ResourceLoader().load_stylesheet()

    assert "__DIALOG_PANEL_BORDER_WIDTH__" not in stylesheet
    assert "__DIALOG_BUTTON_RADIUS__" not in stylesheet
    assert "__TOOLTIP_RADIUS__" not in stylesheet
    assert "QFrame[class=\"JoyReadDialogPanel\"]" in stylesheet
