from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QScrollArea, QWidget

from joyread.app.app_context import create_app_context
from joyread.core.models.book import Book
from joyread.core.models.collection import Collection
from joyread.core.models.tag import Tag
from joyread.core.services.import_service import ImportPreflightResult
from joyread.core.services.library_service import LibraryService
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.settings_viewmodel import SettingsSectionKey
from joyread.ui.viewmodels.shelf_viewmodel import ShelfKey
from joyread.ui.views.main_window import MainWindow
from joyread.ui.widgets.dialogs import (
    DialogCollectionSelectContent,
    DialogInputContent,
    DialogInputFieldWithHeader,
    DialogPasswordContent,
    DialogTagFilterContent,
    DialogTextButton,
    JoyReadDialogOverlay,
    JoyReadDialogPanel,
)
from joyread.ui.widgets.tag_chip import TagChipWidget
from joyread.ui.widgets.tag_management_page import TagManagementPage
from tests.support.in_memory_book_repository import InMemoryBookRepository


def apply_theme() -> None:
    app = QApplication.instance()
    assert app is not None
    app.setStyleSheet(ResourceLoader().load_stylesheet())


class _DialogFakeTagService:
    def __init__(self, tags: tuple[Tag, ...], links: dict[str, tuple[str, ...]]) -> None:
        self._tags = tags
        self._links = dict(links)

    def list_tags(self) -> list[Tag]:
        return sorted(self._tags, key=lambda tag: tag.name_normalized)

    def list_tag_ids_for_books(self, book_ids: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
        return {book_id: self._links.get(book_id, ()) for book_id in book_ids}

    def set_book_tag_ids(self, book_id: str, tag_ids: tuple[str, ...]) -> None:
        self._links[book_id] = tuple(dict.fromkeys(tag_id for tag_id in tag_ids if tag_id))


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
    for button in buttons:
        effect = button.graphicsEffect()
        assert effect is not None
        assert effect.offset().x() == 0
        assert effect.offset().y() == Theme.dialog_button_shadow_offset
    assert buttons[1].x() > buttons[0].x()
    assert buttons[0].y() == buttons[1].y()
    button_group_left = min(button.x() for button in buttons)
    button_group_right = max(button.geometry().right() for button in buttons)
    button_group_center = (button_group_left + button_group_right) // 2
    assert abs(button_group_center - option_area.rect().center().x()) <= 1


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


def test_tag_filter_dialog_allows_empty_tag_panel_and_confirm_off_state(qtbot) -> None:
    apply_theme()
    overlay = JoyReadDialogOverlay()
    qtbot.addWidget(overlay)
    overlay.resize(Theme.window_width, Theme.window_height)
    confirmed: list[tuple[str, ...]] = []

    overlay.show_tag_filter("Tag Filter", [], (), confirmed.append)
    QApplication.processEvents()

    content = overlay.panel.findChild(DialogTagFilterContent, "DialogTagFilterContent")
    buttons = overlay.panel.findChildren(DialogTextButton)
    scroll_panel = overlay.panel.findChild(QWidget, "DialogTagFilterScrollPanel")

    assert content is not None
    assert scroll_panel is not None
    assert scroll_panel.width() == Theme.dialog_collection_scroll_width
    assert scroll_panel.height() == Theme.dialog_tag_filter_panel_height
    assert [button.text for button in buttons] == ["Reset", "Confirm"]

    qtbot.mouseClick(buttons[1], Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    assert overlay.isHidden()
    assert confirmed == [()]


def test_tag_filter_dialog_uses_chip_selection_and_reset_stays_open(qtbot) -> None:
    apply_theme()
    overlay = JoyReadDialogOverlay()
    qtbot.addWidget(overlay)
    overlay.resize(Theme.window_width, Theme.window_height)
    tags = [Tag("tag-action", "Action"), Tag("tag-comedy", "Comedy")]
    confirmed: list[tuple[str, ...]] = []

    overlay.show_tag_filter("Tag Filter", tags, ("tag-action",), confirmed.append)
    QApplication.processEvents()

    content = overlay.panel.findChild(DialogTagFilterContent, "DialogTagFilterContent")
    chips = [chip for chip in overlay.panel.findChildren(TagChipWidget) if not chip.is_add_chip]
    buttons = overlay.panel.findChildren(DialogTextButton)

    assert content is not None
    assert [chip.tag_id for chip in chips if chip.selected] == ["tag-action"]
    qtbot.mouseClick(chips[1], Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
    QApplication.processEvents()
    assert content.selected_tag_ids == ("tag-action", "tag-comedy")

    qtbot.mouseClick(buttons[0], Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert overlay.isVisible()
    assert content.selected_tag_ids == ()
    assert all(not chip.selected for chip in chips)

    qtbot.mouseClick(buttons[1], Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert confirmed == [()]


def test_tag_allocation_dialog_uses_replace_set_selection_rules(qtbot) -> None:
    apply_theme()
    overlay = JoyReadDialogOverlay()
    qtbot.addWidget(overlay)
    overlay.resize(Theme.window_width, Theme.window_height)
    tags = [Tag("tag-action", "Action"), Tag("tag-comedy", "Comedy")]
    confirmed: list[tuple[str, ...]] = []

    overlay.show_tag_allocation("Assign Tags", tags, ("tag-action",), confirmed.append)
    QApplication.processEvents()

    content = overlay.panel.findChild(DialogTagFilterContent, "DialogTagFilterContent")
    chips = [chip for chip in overlay.panel.findChildren(TagChipWidget) if not chip.is_add_chip]
    buttons = overlay.panel.findChildren(DialogTextButton)
    flow = overlay.panel.findChild(QWidget, "DialogTagFilterListHost")

    assert content is not None
    assert flow is not None
    assert [button.text for button in buttons] == ["Cancel", "Reset", "Confirm"]
    assert [chip.tag_id for chip in chips if chip.selected] == ["tag-action"]

    qtbot.mouseClick(chips[1], Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert content.selected_tag_ids == ("tag-action", "tag-comedy")

    qtbot.mouseClick(chips[0], Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert content.selected_tag_ids == ("tag-comedy",)

    qtbot.mouseClick(chips[0], Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert content.selected_tag_ids == ("tag-action", "tag-comedy")

    qtbot.mouseClick(chips[1], Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
    QApplication.processEvents()
    assert content.selected_tag_ids == ("tag-comedy",)

    qtbot.mouseClick(
        flow,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ShiftModifier,
        pos=QPoint(flow.width() - 2, flow.height() - 2),
    )
    QApplication.processEvents()
    assert content.selected_tag_ids == ()

    qtbot.mouseClick(buttons[2], Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    assert overlay.isHidden()
    assert confirmed == [()]


def test_tag_allocation_cancel_discards_selection_and_no_tag_hint(qtbot) -> None:
    apply_theme()
    overlay = JoyReadDialogOverlay()
    qtbot.addWidget(overlay)
    overlay.resize(Theme.window_width, Theme.window_height)
    confirmed: list[tuple[str, ...]] = []

    overlay.show_tag_allocation("Assign Tags", [Tag("tag-action", "Action")], (), confirmed.append)
    QApplication.processEvents()
    chips = [chip for chip in overlay.panel.findChildren(TagChipWidget) if not chip.is_add_chip]
    buttons = overlay.panel.findChildren(DialogTextButton)

    qtbot.mouseClick(chips[0], Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    qtbot.mouseClick(buttons[0], Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    assert overlay.isHidden()
    assert confirmed == []

    overlay.show_tag_allocation("Assign Tags", [], (), confirmed.append)
    QApplication.processEvents()
    hint = overlay.panel.findChild(QLabel, "DialogTagEmptyHint")

    assert hint is not None
    assert hint.text() == "No tags yet. Add or edit tags in Settings > Tags."


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


def test_dialog_password_input_cancel_callback_and_unicode_text(qtbot) -> None:
    apply_theme()
    root = QWidget()
    qtbot.addWidget(root)
    root.resize(Theme.window_width, Theme.window_height)
    overlay = JoyReadDialogOverlay(root)
    overlay.setGeometry(0, 0, root.width(), root.height())
    root.show()

    result: list[str] = []
    overlay.show_password_input(
        "Archive Password",
        "Password",
        on_confirm=lambda value: result.append(value),
        on_cancel=lambda: result.append("cancel"),
    )
    QApplication.processEvents()
    line_edit = overlay.panel.findChild(QLineEdit)
    assert line_edit is not None
    line_edit.setText("秘密")
    qtbot.mouseClick(overlay.panel.findChildren(DialogTextButton)[0], Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    assert overlay.isHidden()
    assert result == ["cancel"]


def test_dialog_password_input_forwards_first_key_when_overlay_has_focus(qtbot) -> None:
    apply_theme()
    root = QWidget()
    qtbot.addWidget(root)
    root.resize(Theme.window_width, Theme.window_height)
    overlay = JoyReadDialogOverlay(root)
    overlay.setGeometry(0, 0, root.width(), root.height())
    root.show()

    result: list[str] = []
    overlay.show_password_input("Archive Password", "Password", on_confirm=result.append)
    QApplication.processEvents()
    line_edit = overlay.panel.findChild(QLineEdit)
    assert line_edit is not None

    overlay.setFocus(Qt.FocusReason.PopupFocusReason)
    qtbot.keyClicks(overlay, "tan'ke")
    QApplication.processEvents()
    qtbot.mouseClick(overlay.panel.findChildren(DialogTextButton)[1], Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    assert result == ["tan'ke"]


def test_dialog_password_input_shows_failure_prompt(qtbot) -> None:
    apply_theme()
    root = QWidget()
    qtbot.addWidget(root)
    root.resize(Theme.window_width, Theme.window_height)
    overlay = JoyReadDialogOverlay(root)
    overlay.setGeometry(0, 0, root.width(), root.height())
    root.show()

    overlay.show_password_input(
        "Archive Password",
        "Password",
        on_confirm=lambda _value: None,
        state_prompt="Incorrect password. Please try again.",
    )
    QApplication.processEvents()

    prompt = overlay.panel.findChild(QLabel, "DialogStatePrompt")
    assert prompt is not None
    assert prompt.text() == "Incorrect password. Please try again."


def test_dialog_password_input_supports_skip_button_without_validation(qtbot) -> None:
    apply_theme()
    root = QWidget()
    qtbot.addWidget(root)
    root.resize(Theme.window_width, Theme.window_height)
    overlay = JoyReadDialogOverlay(root)
    overlay.setGeometry(0, 0, root.width(), root.height())
    root.show()

    result: list[str] = []
    overlay.show_password_input(
        "Archive Password",
        "Password for: outer.cbz::nested.cbz",
        on_confirm=lambda value: result.append(f"open:{value}"),
        on_cancel=lambda: result.append("cancel"),
        on_skip=lambda: result.append("skip"),
        skip_text="Skip",
        validator=lambda value: None if value else "Password cannot be empty.",
    )
    QApplication.processEvents()
    buttons = overlay.panel.findChildren(DialogTextButton)

    assert [button.text for button in buttons] == ["Cancel", "Skip", "Confirm"]
    qtbot.mouseClick(buttons[1], Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    assert overlay.isHidden()
    assert result == ["skip"]


def test_dialog_password_input_elides_header_and_scrolls_long_detail(qtbot) -> None:
    apply_theme()
    root = QWidget()
    qtbot.addWidget(root)
    root.resize(Theme.window_width, Theme.window_height)
    overlay = JoyReadDialogOverlay(root)
    overlay.setGeometry(0, 0, root.width(), root.height())
    root.show()
    archive_name = (
        "Password for: 完整战车娘2022.10-2024.5.rar::EX/Code/"
        "坦克世界是一款非常长非常长的嵌套压缩包名称.rar"
    )
    detail_text = "完整路径: " + ("完整战车娘2022.10-2024.5.rar::EX/Code/坦克世界/" * 20)

    overlay.show_password_input(
        "Archive Password",
        archive_name,
        on_confirm=lambda _value: None,
        detail_text=detail_text,
        state_prompt="Incorrect password. Please try again.",
    )
    QApplication.processEvents()

    header = [
        label
        for label in overlay.panel.findChildren(QLabel)
        if label.property("class") == "DialogInputHeader"
    ][0]
    detail = overlay.panel.findChild(QLabel, "DialogDetailPrompt")
    content_scroll = overlay.panel.findChild(QScrollArea, "JoyReadDialogContentScrollArea")

    assert header.text() != archive_name
    assert header.toolTip() == archive_name
    assert detail is not None
    assert detail.text() == detail_text
    assert detail.width() <= content_scroll.width()
    assert content_scroll.height() == Theme.dialog_content_max_height
    assert content_scroll.verticalScrollBar().maximum() > 0


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


def test_main_window_uses_global_confirm_dialog_for_delete(qtbot) -> None:
    apply_theme()
    context = create_app_context()
    window = MainWindow(context)
    qtbot.addWidget(window)
    window.resize(Theme.window_width, Theme.window_height)
    window.show()
    QApplication.processEvents()

    book = Book(
        uuid="book-1",
        title="Delete Me",
        author=None,
        language_tag="en",
        book_type="Comic",
        file_format="CBZ",
        file_path="/tmp/delete-me.cbz",
        progress=0.0,
        cover_thumbnail_path=None,
        added_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 1),
        last_read_at=None,
        is_favourite=False,
    )
    context.shelf_viewmodel.books = [book]
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


def test_main_window_confirms_tag_delete_before_deleting(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    apply_theme()
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    context = create_app_context()
    window = MainWindow(context)
    qtbot.addWidget(window)
    window.resize(Theme.window_width, Theme.window_height)
    window.show()
    QApplication.processEvents()

    context.tag_service.create("Comedy")
    context.tag_management_viewmodel.refresh()
    window._handle_navigation("settings")
    context.settings_viewmodel.set_section(SettingsSectionKey.TAGS)
    QApplication.processEvents()

    tag_page = window.settings_view.page.findChild(TagManagementPage)
    assert tag_page is not None
    chip = next(chip for chip in tag_page.findChildren(TagChipWidget) if not chip.is_add_chip)
    qtbot.mouseClick(chip, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    qtbot.mouseClick(tag_page.delete_button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    title_labels = [
        label.text()
        for label in window.dialog_overlay.panel.findChildren(QLabel)
        if label.property("class") == "JoyReadDialogTitle"
    ]
    buttons = [button.text for button in window.dialog_overlay.panel.findChildren(DialogTextButton)]

    assert window.dialog_overlay.isVisible()
    assert title_labels == ["Delete Tag"]
    assert buttons == ["Cancel", "Delete"]
    assert [tag.name for tag in context.tag_service.list_tags()] == ["Comedy"]

    qtbot.mouseClick(window.dialog_overlay.panel.findChildren(DialogTextButton)[0], Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert [tag.name for tag in context.tag_service.list_tags()] == ["Comedy"]

    qtbot.mouseClick(tag_page.delete_button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    qtbot.mouseClick(window.dialog_overlay.panel.findChildren(DialogTextButton)[1], Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    assert context.tag_service.list_tags() == []
    context.close()


def test_detail_tag_click_activates_all_shelf_filter_and_closes_detail(qtbot, monkeypatch, tmp_path: Path) -> None:
    apply_theme()
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    context = create_app_context()
    try:
        window = MainWindow(context)
        qtbot.addWidget(window)
        repo = InMemoryBookRepository()
        tag_service = _DialogFakeTagService(
            (Tag("tag-action", "Action"),),
            {"mock-book-01": ("tag-action",)},
        )
        context.shelf_viewmodel.replace_services(
            LibraryService(repo),
            context.thumbnail_service,
            tag_service,  # type: ignore[arg-type]
        )
        context.shelf_viewmodel.load_books()
        window.resize(Theme.window_width, Theme.window_height)
        window.show()
        context.shelf_viewmodel.show_detail("mock-book-01")
        QApplication.processEvents()

        chip = next(
            chip
            for chip in window.shelf_view.detail_panel.findChildren(TagChipWidget)
            if chip.tag_id == "tag-action"
        )
        qtbot.mouseClick(chip, Qt.MouseButton.LeftButton)
        QApplication.processEvents()

        assert context.shelf_viewmodel.current_shelf == ShelfKey.ALL.value
        assert context.shelf_viewmodel.tag_filter_ids == ("tag-action",)
        assert context.shelf_viewmodel.detail_book_uuid is None
        assert window.shelf_view.detail_panel.isHidden()
    finally:
        context.close()


def test_detail_tag_click_activates_hidden_shelf_for_hidden_book(qtbot, monkeypatch, tmp_path: Path) -> None:
    apply_theme()
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    context = create_app_context()
    try:
        window = MainWindow(context)
        qtbot.addWidget(window)
        repo = InMemoryBookRepository()
        repo.set_book_hidden("mock-book-03", True)
        tag_service = _DialogFakeTagService(
            (Tag("tag-action", "Action"),),
            {"mock-book-03": ("tag-action",)},
        )
        context.shelf_viewmodel.replace_services(
            LibraryService(repo),
            context.thumbnail_service,
            tag_service,  # type: ignore[arg-type]
        )
        context.shelf_viewmodel.load_books()
        window.resize(Theme.window_width, Theme.window_height)
        window.show()
        context.shelf_viewmodel.set_current_shelf(ShelfKey.HIDDEN.value)
        context.shelf_viewmodel.show_detail("mock-book-03")
        QApplication.processEvents()

        chip = next(
            chip
            for chip in window.shelf_view.detail_panel.findChildren(TagChipWidget)
            if chip.tag_id == "tag-action"
        )
        qtbot.mouseClick(chip, Qt.MouseButton.LeftButton)
        QApplication.processEvents()

        assert context.shelf_viewmodel.current_shelf == ShelfKey.HIDDEN.value
        assert context.shelf_viewmodel.tag_filter_ids == ("tag-action",)
        assert context.shelf_viewmodel.detail_book_uuid is None
        assert window.shelf_view.detail_panel.isHidden()
    finally:
        context.close()


def test_detail_plus_opens_allocation_and_confirm_updates_detail_tags(qtbot, monkeypatch, tmp_path: Path) -> None:
    apply_theme()
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    context = create_app_context()
    try:
        window = MainWindow(context)
        qtbot.addWidget(window)
        repo = InMemoryBookRepository()
        tag_service = _DialogFakeTagService(
            (Tag("tag-action", "Action"), Tag("tag-comedy", "Comedy")),
            {"mock-book-01": ("tag-action",)},
        )
        context.tag_service = tag_service  # type: ignore[assignment]
        context.shelf_viewmodel.replace_services(
            LibraryService(repo),
            context.thumbnail_service,
            tag_service,  # type: ignore[arg-type]
        )
        context.shelf_viewmodel.load_books()
        window.resize(Theme.window_width, Theme.window_height)
        window.show()
        context.shelf_viewmodel.show_detail("mock-book-01")
        QApplication.processEvents()

        add_chip = next(
            chip
            for chip in window.shelf_view.detail_panel.findChildren(TagChipWidget)
            if chip.is_add_chip
        )
        qtbot.mouseClick(add_chip, Qt.MouseButton.LeftButton)
        QApplication.processEvents()

        dialog_chips = [
            chip
            for chip in window.dialog_overlay.panel.findChildren(TagChipWidget)
            if not chip.is_add_chip
        ]
        buttons = window.dialog_overlay.panel.findChildren(DialogTextButton)
        assert window.dialog_overlay.isVisible()
        assert [chip.tag_id for chip in dialog_chips if chip.selected] == ["tag-action"]

        comedy_chip = next(chip for chip in dialog_chips if chip.tag_id == "tag-comedy")
        qtbot.mouseClick(comedy_chip, Qt.MouseButton.LeftButton)
        QApplication.processEvents()
        qtbot.mouseClick(buttons[2], Qt.MouseButton.LeftButton)

        qtbot.waitUntil(
            lambda: context.shelf_viewmodel.tag_ids_for_book("mock-book-01") == (
                "tag-action",
                "tag-comedy",
            ),
            timeout=2000,
        )
        QApplication.processEvents()

        detail_tag_ids = {
            chip.tag_id
            for chip in window.shelf_view.detail_panel.findChildren(TagChipWidget)
            if not chip.is_add_chip
        }
        assert detail_tag_ids == {"tag-action", "tag-comedy"}
    finally:
        context.close()


def test_open_import_skipped_file_prompts_read_only_reader(qtbot, monkeypatch, tmp_path: Path) -> None:
    apply_theme()
    context = create_app_context()
    window = MainWindow(context)
    qtbot.addWidget(window)
    window.resize(Theme.window_width, Theme.window_height)
    window.show()
    QApplication.processEvents()

    opened: list[Path] = []
    import_started: list[Path] = []
    source = tmp_path / "encrypted.cbz"
    monkeypatch.setattr(window, "_show_reader_window", lambda path, **_kwargs: opened.append(Path(path)))
    monkeypatch.setattr(window, "_start_open_and_import", lambda path, _settings: import_started.append(Path(path)))

    window._handle_open_import_preflight(
        source,
        object(),
        ImportPreflightResult(
            source_path=str(source),
            can_import=False,
            status="skipped",
            message="Skipped encrypted archive.",
        ),
    )
    QApplication.processEvents()

    buttons = window.dialog_overlay.panel.findChildren(DialogTextButton)
    assert window.dialog_overlay.isVisible()
    assert [button.text for button in buttons] == ["Cancel", "Read Only"]
    assert opened == []
    assert import_started == []

    qtbot.mouseClick(buttons[1], Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    assert window.dialog_overlay.isHidden()
    assert opened == [source]
    assert import_started == []
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
