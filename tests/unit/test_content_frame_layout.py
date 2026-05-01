from PySide6.QtWidgets import QApplication, QWidget

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.book_grid import BookGridWidget
from joyread.ui.widgets.book_list import BookListWidget
from joyread.ui.widgets.top_toolbar import TopToolbarWidget


def apply_theme() -> None:
    app = QApplication.instance()
    assert app is not None
    app.setStyleSheet(ResourceLoader().load_stylesheet())


def test_toolbar_uses_figma_content_frame_padding(qtbot) -> None:
    apply_theme()
    toolbar = TopToolbarWidget(ResourceLoader())
    qtbot.addWidget(toolbar)

    margins = toolbar.layout().contentsMargins()
    expected = Theme.content_horizontal_padding + Theme.banner_horizontal_padding

    assert toolbar.height() == Theme.toolbar_height
    assert (margins.left(), margins.right()) == (expected, expected)


def test_shelf_scroll_areas_keep_scrollbar_at_outer_edge(qtbot) -> None:
    apply_theme()

    for widget in (BookGridWidget(ResourceLoader()), BookListWidget(ResourceLoader())):
        qtbot.addWidget(widget)
        assert widget.property("class") == "ShelfScrollArea"

        content = widget.findChild(QWidget, "BookGridContent") or widget.findChild(QWidget, "BookListContent")
        assert content is not None
        margins = content.layout().contentsMargins()
        assert margins.left() == Theme.content_horizontal_padding
        assert margins.right() == Theme.content_scrollbar_adjusted_right_padding
        assert margins.top() == Theme.grid_top_padding
        assert margins.bottom() == Theme.grid_bottom_padding


def test_stylesheet_resolves_content_and_scrollbar_tokens() -> None:
    stylesheet = ResourceLoader().load_stylesheet()

    assert "__CONTENT_COLOR__" not in stylesheet
    assert "__SHELF_SCROLLBAR_WIDTH__" not in stylesheet
    assert "QScrollArea[class=\"ShelfScrollArea\"] QScrollBar:vertical" in stylesheet
