from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFrame, QLabel

from joyread.core.models.collection import Collection
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.shelf_viewmodel import ShelfKey, collection_shelf_key
from joyread.ui.widgets.sidebar import SidebarWidget


def apply_theme() -> None:
    app = QApplication.instance()
    assert app is not None
    app.setStyleSheet(ResourceLoader().load_stylesheet())


def sidebar_items(sidebar: SidebarWidget) -> list[QFrame]:
    return [
        frame
        for frame in sidebar.findChildren(QFrame)
        if frame.property("class") == "SidebarItem"
    ]


def test_sidebar_matches_figma_root_and_section_geometry(qtbot) -> None:
    apply_theme()
    sidebar = SidebarWidget(ResourceLoader())
    qtbot.addWidget(sidebar)

    root_margins = sidebar.layout().contentsMargins()
    assert sidebar.width() == Theme.sidebar_width
    assert (root_margins.left(), root_margins.top(), root_margins.right(), root_margins.bottom()) == (
        Theme.sidebar_margin_horizontal,
        Theme.sidebar_margin_vertical,
        Theme.sidebar_margin_horizontal,
        Theme.sidebar_margin_vertical,
    )

    banners = sidebar.findChildren(QFrame, "SidebarSectionBanner")
    assert len(banners) == 2
    for banner in banners:
        margins = banner.layout().contentsMargins()
        assert banner.height() == Theme.sidebar_section_height
        assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (
            Theme.sidebar_section_padding_left,
            Theme.sidebar_section_padding_top,
            Theme.sidebar_section_padding_right,
            Theme.sidebar_section_padding_bottom,
        )
        arrow = banner.findChild(QLabel, "SidebarSectionArrow")
        assert arrow is not None
        assert arrow.size().width() == Theme.sidebar_section_arrow_size
        assert arrow.size().height() == Theme.sidebar_section_arrow_size


def test_sidebar_items_keep_figma_label_group_spacing_and_selection(qtbot) -> None:
    apply_theme()
    sidebar = SidebarWidget(ResourceLoader())
    qtbot.addWidget(sidebar)

    items = sidebar_items(sidebar)
    assert len(items) == 6

    for item in items:
        margins = item.layout().contentsMargins()
        assert item.height() == Theme.sidebar_item_height
        assert item.cursor().shape() == Qt.CursorShape.PointingHandCursor
        assert item.layout().spacing() == Theme.sidebar_item_icon_text_gap
        assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (
            Theme.sidebar_item_padding_left,
            Theme.sidebar_item_padding_vertical,
            Theme.sidebar_item_padding_right,
            Theme.sidebar_item_padding_vertical,
        )

    assert items[0].property("selected") == "true"
    sidebar.set_active(ShelfKey.RECENT.value)
    assert items[0].property("selected") == "false"
    assert items[1].property("selected") == "true"


def test_sidebar_collection_slot_remaps_to_viewmodel_collection_key(qtbot) -> None:
    apply_theme()
    sidebar = SidebarWidget(ResourceLoader())
    qtbot.addWidget(sidebar)

    emitted: list[str] = []
    sidebar.navigation_requested.connect(emitted.append)

    now = datetime(2026, 1, 1)
    collection = Collection(
        uuid="custom-1",
        name="Reading Queue",
        is_private=False,
        created_at=now,
        updated_at=now,
    )
    sidebar.set_collections([collection])

    labels = [label.text() for label in sidebar.findChildren(QLabel, "SidebarItemLabel")]
    assert "Reading Queue" in labels

    sidebar.set_active(collection_shelf_key(collection.uuid))
    collection_item = [item for item in sidebar_items(sidebar) if item.property("selected") == "true"]
    assert len(collection_item) == 1

    qtbot.mouseClick(collection_item[0], Qt.MouseButton.LeftButton)
    assert emitted == [collection_shelf_key(collection.uuid)]


def test_stylesheet_resolves_sidebar_tokens() -> None:
    stylesheet = ResourceLoader().load_stylesheet()

    assert "__SIDEBAR_SECTION_COLOR__" not in stylesheet
    assert "__SIDEBAR_ITEM_RADIUS__" not in stylesheet
    assert "__SIDEBAR_ITEM_HOVER__" not in stylesheet
    assert "QFrame[class=\"SidebarItem\"]" in stylesheet
    assert f"font-size: {Theme.sidebar_item_font_size}px;" in stylesheet
