from datetime import datetime

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QScrollArea, QWidget

from joyread.app.app_context import create_app_context
from joyread.core.models.collection import Collection
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.views.main_window import MainWindow
from joyread.ui.widgets.dialogs import (
    DialogCollectionSelectContent,
    DialogInputContent,
    DialogInputFieldWithHeader,
    DialogPasswordContent,
    DialogTextButton,
    JoyReadDialogOverlay,
    JoyReadDialogPanel,
)


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
    assert panel.width() == Theme.dialog_width
    assert panel.height() == panel.sizeHint().height()
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
        Theme.dialog_content_outer_padding,
        Theme.dialog_content_outer_padding,
        Theme.dialog_content_outer_padding,
        Theme.dialog_content_outer_padding,
    )
    content_scroll = panel.findChild(QScrollArea, "JoyReadDialogContentScrollArea")
    assert content_scroll is not None
    assert content_scroll.height() <= Theme.dialog_content_max_height
    assert option_area.layout().spacing() == Theme.dialog_option_gap
    assert [button.text for button in buttons] == ["Cancel", "Confirm"]
    assert {(button.width(), button.height()) for button in buttons} == {
        (Theme.dialog_button_width, Theme.dialog_button_height)
    }
    assert buttons[1].x() > buttons[0].x()
    assert buttons[0].y() == buttons[1].y()


def test_dialog_content_grows_to_max_viewport_then_scrolls(qtbot) -> None:
    apply_theme()
    panel = JoyReadDialogPanel()
    qtbot.addWidget(panel)
    panel.set_confirm("Delete Book", "\n".join(f"Line {index}" for index in range(40)), "Cancel", "Delete")
    panel.show()
    QApplication.processEvents()

    content_scroll = panel.findChild(QScrollArea, "JoyReadDialogContentScrollArea")

    assert content_scroll is not None
    assert panel.height() == panel.sizeHint().height()
    assert content_scroll.height() == Theme.dialog_content_max_height
    assert content_scroll.verticalScrollBar().maximum() > 0


def test_delete_dialog_messages_measure_wrapped_height_without_clipping(qtbot) -> None:
    apply_theme()
    titles = (
        "Miss Kobayashi's Dragon Maid v01 (2016) (Goldenagato)",
        "Delicious in Dungeon v14 (Ryōko Kui) (z-library.sk, 1lib.sk, z-lib.sk)",
    )

    for title in titles:
        panel = JoyReadDialogPanel()
        qtbot.addWidget(panel)
        panel.set_confirm(
            "Delete Book",
            (
                f"Delete '{title}' from JoyRead?\n\n"
                "This removes its library record, collections, progress, bookmarks, "
                "recent history, and the app-managed copied file."
            ),
            "Cancel",
            "Delete",
        )
        panel.show()
        QApplication.processEvents()

        content_scroll = panel.findChild(QScrollArea, "JoyReadDialogContentScrollArea")
        label = [
            candidate
            for candidate in panel.findChildren(QLabel)
            if candidate.property("class") == "JoyReadDialogContent"
        ][0]

        assert content_scroll is not None
        assert content_scroll.verticalScrollBar().maximum() == 0
        assert label.height() >= label.heightForWidth(label.width())
        assert panel.height() == panel.sizeHint().height()


def test_dialog_one_field_input_matches_figma_geometry(qtbot) -> None:
    apply_theme()
    overlay = JoyReadDialogOverlay()
    qtbot.addWidget(overlay)
    overlay.resize(Theme.window_width, Theme.window_height)
    overlay.show_input("New Collection", "Collection Name", lambda _value: None, initial_text="Reading")
    QApplication.processEvents()

    content = overlay.panel.findChild(DialogInputContent, "DialogInputContent")
    group = overlay.panel.findChild(DialogInputFieldWithHeader, "DialogInputFieldWithHeader")
    line_edit = overlay.panel.findChild(QLineEdit)

    assert content is not None
    assert group is not None
    assert line_edit is not None
    assert content.width() == (
        Theme.dialog_width
        - (Theme.dialog_layout_margin * 2)
        - (Theme.dialog_content_outer_padding * 2)
    )
    assert group.width() == Theme.dialog_input_group_width
    assert line_edit.width() == (
        Theme.dialog_input_group_width - (Theme.dialog_input_group_padding * 2)
    )
    assert line_edit.height() == Theme.dialog_input_field_height
    assert line_edit.text() == "Reading"


def test_dialog_input_validator_keeps_overlay_open(qtbot) -> None:
    apply_theme()
    overlay = JoyReadDialogOverlay()
    qtbot.addWidget(overlay)
    overlay.resize(Theme.window_width, Theme.window_height)
    submitted: list[str] = []
    overlay.show_input(
        "New Collection",
        "Collection Name",
        submitted.append,
        validator=lambda value: None if value.strip() else "Collection name cannot be empty.",
    )
    QApplication.processEvents()

    button = overlay.panel.findChildren(DialogTextButton)[1]
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    assert overlay.isVisible()
    assert submitted == []
    assert overlay.panel.findChild(QLabel, "DialogStatePrompt").text() == "Collection name cannot be empty."


def test_dialog_password_and_collection_select_content_geometry(qtbot) -> None:
    apply_theme()
    now = datetime(2026, 1, 1)
    collections = [
        Collection(f"collection-{index}", f"Collection {index}", False, now, now)
        for index in range(3)
    ]

    password_content = DialogPasswordContent()
    select_content = DialogCollectionSelectContent(collections, ResourceLoader())
    qtbot.addWidget(password_content)
    qtbot.addWidget(select_content)
    password_content.show()
    select_content.show()
    QApplication.processEvents()

    fields = password_content.findChildren(DialogInputFieldWithHeader)
    scroll_panel = select_content.findChild(QWidget, "DialogCollectionScrollPanel")
    inner_scroll = select_content.findChild(QScrollArea, "DialogCollectionInnerScrollArea")

    assert len(fields) == 3
    assert all(field.width() == Theme.dialog_input_group_width for field in fields)
    assert scroll_panel is not None
    assert inner_scroll is not None
    assert scroll_panel.width() == Theme.dialog_collection_scroll_width
    assert scroll_panel.height() == (
        Theme.dialog_collection_scroll_layout_margin * 2
        + len(collections) * Theme.sidebar_item_height
        + ((len(collections) - 1) * Theme.dialog_collection_item_gap)
    )
    assert inner_scroll.verticalScrollBar().maximum() == 0
    assert select_content.selected_collection_uuid == "collection-0"


def test_collection_select_dialog_hugs_few_rows_and_scrolls_many_rows(qtbot) -> None:
    apply_theme()
    now = datetime(2026, 1, 1)
    overlay = JoyReadDialogOverlay(resources=ResourceLoader())
    qtbot.addWidget(overlay)
    overlay.resize(Theme.window_width, Theme.window_height)

    few_collections = [Collection("one", "One", False, now, now)]
    overlay.show_collection_select("Add to Collection", few_collections, lambda _uuid: None)
    QApplication.processEvents()

    content_scroll = overlay.panel.findChild(QScrollArea, "JoyReadDialogContentScrollArea")
    scroll_panel = overlay.panel.findChild(QWidget, "DialogCollectionScrollPanel")
    inner_scroll = overlay.panel.findChild(QScrollArea, "DialogCollectionInnerScrollArea")

    assert content_scroll is not None
    assert scroll_panel is not None
    assert inner_scroll is not None
    assert scroll_panel.height() < Theme.dialog_content_max_height
    assert content_scroll.verticalScrollBar().maximum() == 0
    assert inner_scroll.verticalScrollBar().maximum() == 0

    many_collections = [
        Collection(f"collection-{index}", f"Collection {index}", False, now, now)
        for index in range(12)
    ]
    overlay.show_collection_select("Add to Collection", many_collections, lambda _uuid: None)
    QApplication.processEvents()

    content_scroll = overlay.panel.findChild(QScrollArea, "JoyReadDialogContentScrollArea")
    scroll_panel = overlay.panel.findChild(QWidget, "DialogCollectionScrollPanel")
    inner_scroll = overlay.panel.findChild(QScrollArea, "DialogCollectionInnerScrollArea")

    assert content_scroll is not None
    assert scroll_panel is not None
    assert inner_scroll is not None
    assert content_scroll.height() == Theme.dialog_content_max_height
    assert scroll_panel.width() == Theme.dialog_collection_scroll_width
    assert content_scroll.verticalScrollBar().maximum() == 0
    assert inner_scroll.verticalScrollBar().maximum() > 0
    assert inner_scroll.geometry().right() <= scroll_panel.contentsRect().right()


def test_dialog_openings_reset_collection_scroll_state(qtbot) -> None:
    apply_theme()
    now = datetime(2026, 1, 1)
    overlay = JoyReadDialogOverlay(resources=ResourceLoader())
    qtbot.addWidget(overlay)
    overlay.resize(Theme.window_width, Theme.window_height)

    many_collections = [
        Collection(f"collection-{index}", f"Collection {index}", False, now, now)
        for index in range(12)
    ]
    overlay.show_collection_select("Add to Collection", many_collections, lambda _uuid: None)
    QApplication.processEvents()
    inner_scroll = overlay.panel.findChild(QScrollArea, "DialogCollectionInnerScrollArea")
    assert inner_scroll is not None
    inner_scroll.verticalScrollBar().setValue(inner_scroll.verticalScrollBar().maximum())
    tall_panel_height = overlay.panel.height()

    overlay.show_input("New Collection", "Collection Name", lambda _value: None)
    QApplication.processEvents()
    input_panel_height = overlay.panel.height()
    assert overlay.panel.findChild(QScrollArea, "DialogCollectionInnerScrollArea") is None
    assert input_panel_height != tall_panel_height

    overlay.show_collection_select(
        "Add to Collection",
        [Collection("one", "One", False, now, now)],
        lambda _uuid: None,
    )
    QApplication.processEvents()
    inner_scroll = overlay.panel.findChild(QScrollArea, "DialogCollectionInnerScrollArea")
    assert inner_scroll is not None
    assert inner_scroll.verticalScrollBar().value() == 0
    assert overlay.panel.height() < tall_panel_height


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
    context = create_app_context()
    window = MainWindow(context)
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
    context.close()


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
    assert "__DIALOG_PANEL_BACKGROUND__" not in stylesheet
    assert "__DIALOG_CONTENT_SCROLLBAR_MARGIN__" not in stylesheet
    assert "__DIALOG_BUTTON_RADIUS__" not in stylesheet
    assert "__TOOLTIP_RADIUS__" not in stylesheet
    assert "QFrame[class=\"JoyReadDialogPanel\"]" in stylesheet
    assert "QScrollArea#JoyReadDialogContentScrollArea QWidget" not in stylesheet
    assert "QWidget#JoyReadDialogContentViewport" in stylesheet
    assert "QWidget#DialogCollectionSelectContent" in stylesheet
    assert Theme._rgba_qss(Theme.color_dialog_panel_background_rgba) in stylesheet
    assert (
        "QWidget#DialogInputFieldWithHeader {\n"
        "    background: transparent;\n"
        "    border: none;\n"
        "}"
    ) in stylesheet
    assert (
        "margin: "
        f"{Theme.dialog_content_scrollbar_margin}px 2px "
        f"{Theme.dialog_content_scrollbar_margin}px 0;"
    ) in stylesheet
