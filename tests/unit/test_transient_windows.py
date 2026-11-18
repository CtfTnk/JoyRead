"""Embedded controls must stay children, including during queued layout updates."""

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from joyread.core.models.language import Language
from joyread.core.models.tag import Tag
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.viewmodels.reader_items import ReaderBookmarkItem, ReaderContentsItem
from joyread.ui.viewmodels.shelf_filter import BookFilterPreview
from joyread.ui.widgets.dialogs import JoyReadDialogOverlay
from joyread.ui.widgets.hidden_space_lock import HiddenSpaceLockOverlay
from joyread.ui.widgets.reader_controls import ReaderHeader
from joyread.ui.widgets.reader_topic_panel import ReaderTopicMode, ReaderTopicPanel
from joyread.ui.widgets.tag_browser import TagBrowserWidget
from joyread.ui.widgets.tag_selection_panel import TagChipFlowWidget
from tests.support.window_events import record_top_level_shows


@pytest.fixture
def host(qtbot):
    widget = QWidget()
    qtbot.addWidget(widget)
    widget.show()
    QApplication.processEvents()
    return widget


def test_embedded_dialog_and_header_controls_never_open_windows(host):
    with record_top_level_shows() as shown:
        HiddenSpaceLockOverlay(host).show()
        ReaderHeader(ResourceLoader(), host, show_custom_button=True).show()
        overlay = JoyReadDialogOverlay(host)
        overlay.show_book_filter(
            "Filter", [], [Language("English", "en")], (), (),
            lambda tags, languages: BookFilterPreview(0, {}), lambda tags, languages: None,
        )
        overlay.show_info("Books", "Done")
        overlay.show_info("Books", "Ready")
        QApplication.processEvents()
        assert not shown


def test_rapid_tag_rebuilds_never_show_retired_chips_or_groups(host):
    browser = TagBrowserWidget(ResourceLoader(), host, show_tray=True)
    chips = TagChipFlowWidget(host)
    browser.show()
    chips.show()
    with record_top_level_shows() as shown:
        for name in ("Alpha", "Beta"):
            tags = [Tag(name, name)]
            browser.set_tags(tags, (name,))
            chips.set_tags(tags, (name,))
        QApplication.processEvents()
        assert not shown


@pytest.mark.parametrize("mode", [ReaderTopicMode.CONTENTS, ReaderTopicMode.BOOKMARKS])
def test_reader_topic_refresh_never_shows_retired_rows(host, mode):
    panel = ReaderTopicPanel(ResourceLoader(), host)
    panel.set_mode(mode)
    panel.show()
    with record_top_level_shows() as shown:
        for page in (1, 2):
            panel.set_contents((ReaderContentsItem("Chapter", page),))
            panel.set_bookmarks((ReaderBookmarkItem("bookmark", "Here", page),))
        QApplication.processEvents()
        assert not shown
