"""Tests for the shared searchable, A-Z grouped tag browser (design 1a)."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFrame, QLabel

from joyread.core.models.tag import Tag
from joyread.infrastructure.i18n import locale_service
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.tag_browser import TagBrowserWidget
from joyread.ui.widgets.tag_chip import TagChipWidget


NAMES = (
    "Action",
    "Adventure",
    "Comedy",
    "Drama",
    "Horror",
    "Isekai",
    "Shounen",
    "Zombies",
    "ホラー",
    "日常",
)


def _tags() -> list[Tag]:
    return [Tag(name.lower(), name) for name in NAMES]


@pytest.fixture()
def browser(qtbot):
    locale_service.init(ResourceLoader().locale_dir(), None, "English")

    def _make(**kwargs) -> TagBrowserWidget:
        widget = TagBrowserWidget(ResourceLoader(), **kwargs)
        qtbot.addWidget(widget)
        widget.resize(Theme.tag_browser_width, Theme.tag_browser_height)
        widget.show()
        return widget

    return _make


def _search(qtbot, widget: TagBrowserWidget, text: str) -> None:
    """Type *text* and commit it. The pool rebuilds on Enter, not per
    keystroke, so a test that only calls setText is testing nothing."""

    widget.search_field.setText(text)
    qtbot.keyClick(widget.search_field, Qt.Key.Key_Return)


def _pool_chips(widget: TagBrowserWidget) -> list[TagChipWidget]:
    return [chip for chip in widget.chip_widgets if not chip.is_add_chip and not chip.is_removable]


def _tray_chips(widget: TagBrowserWidget) -> list[TagChipWidget]:
    return [chip for chip in widget.findChildren(TagChipWidget) if chip.is_removable]


def test_tags_are_grouped_and_the_rail_lists_only_present_letters(browser) -> None:
    widget = browser()
    widget.set_tags(_tags())

    # Horror and ホラー share H; 日常 romanizes to nichijou and lands on N.
    assert widget.rail_letters == ("A", "C", "D", "H", "I", "N", "S", "Z")
    assert len(_pool_chips(widget)) == len(NAMES)


def test_search_narrows_the_pool_and_clearing_restores_it(browser, qtbot) -> None:
    widget = browser()
    widget.set_tags(_tags())

    _search(qtbot, widget, "adv")
    assert [chip.tag_id for chip in _pool_chips(widget)] == ["adventure"]

    _search(qtbot, widget, "")
    assert len(_pool_chips(widget)) == len(NAMES)


def test_search_matches_a_romanized_reading(browser, qtbot) -> None:
    """Typing Latin text reaches a CJK tag, since the reading is already
    computed to place the tag on the rail."""

    widget = browser()
    widget.set_tags(_tags())

    _search(qtbot, widget, "hora")

    assert [chip.tag_id for chip in _pool_chips(widget)] == ["ホラー"]


def test_search_with_no_match_shows_the_empty_state(browser, qtbot) -> None:
    widget = browser()
    widget.set_tags(_tags())

    _search(qtbot, widget, "zzzzz")

    hint = widget.findChild(QLabel, "TagBrowserEmptyHint")
    assert hint is not None
    assert hint.text() == "No tags match that search."
    assert _pool_chips(widget) == []


def test_the_rail_stays_stable_while_a_search_narrows_the_pool(browser, qtbot) -> None:
    """The rail is built from every tag, not the filtered set, so it does not
    reshuffle under the cursor as the user types."""

    widget = browser()
    widget.set_tags(_tags())
    before = widget.rail_letters

    _search(qtbot, widget, "adv")

    assert widget.rail_letters == before


def test_rail_click_scrolls_that_group_to_the_top(browser, qtbot) -> None:
    widget = browser()
    widget.set_tags(_tags())
    # The rail reads laid-out positions, which a real click always has.
    qtbot.waitUntil(lambda: widget._scroll.verticalScrollBar().maximum() > 0, timeout=2000)

    widget.jump_to_letter("Z")
    scrolled = widget._scroll.verticalScrollBar().value()
    assert scrolled > 0

    widget.jump_to_letter("A")
    assert widget._scroll.verticalScrollBar().value() == 0


def test_tray_lists_the_selection_and_is_hidden_when_not_requested(browser) -> None:
    with_tray = browser(show_tray=True)
    with_tray.set_tags(_tags(), ["shounen", "isekai"])

    assert {chip.tag_id for chip in _tray_chips(with_tray)} == {"shounen", "isekai"}

    without_tray = browser(show_tray=False)
    without_tray.set_tags(_tags(), ["shounen"])

    assert _tray_chips(without_tray) == []


def test_tray_shows_a_hint_when_nothing_is_selected(browser) -> None:
    widget = browser(show_tray=True)
    widget.set_tags(_tags())

    hint = widget.findChild(QLabel, "TagBrowserTrayEmptyHint")
    assert hint is not None
    assert hint.text() == "Nothing yet — pick tags below."


def test_tray_tracks_selection_changes(browser) -> None:
    widget = browser(show_tray=True)
    widget.set_tags(_tags(), ["shounen"])

    widget.set_selected_tag_ids(["shounen", "drama"])
    assert {chip.tag_id for chip in _tray_chips(widget)} == {"shounen", "drama"}

    widget.clear_selection()
    assert _tray_chips(widget) == []


def test_tray_chip_emits_remove_intent_without_emitting_pool_selection(browser, qtbot) -> None:
    widget = browser(show_tray=True)
    widget.set_tags(_tags(), ["action", "comedy"])
    selected: list[tuple[str, bool]] = []
    removed: list[str] = []
    widget.tag_clicked.connect(lambda tag_id, additive: selected.append((tag_id, additive)))
    widget.tag_remove_clicked.connect(removed.append)

    action = next(chip for chip in _tray_chips(widget) if chip.tag_id == "action")
    qtbot.mouseClick(action, Qt.MouseButton.LeftButton)

    assert removed == ["action"]
    assert selected == []
    # The presentation-only browser still waits for its owner to hand the
    # updated selection back.
    assert widget.selected_tag_ids == ("action", "comedy")


def test_pool_height_is_fixed_as_the_tray_fills(browser) -> None:
    """The panel must not grow as the tray fills: the tray takes its rows out
    of the group list below it, not out of the dialog's height."""

    widget = browser(show_tray=True)
    # Deliberately taller than the browser needs: the pool must hold its own
    # height rather than stretch to whatever space it is given.
    widget.resize(Theme.tag_browser_width, Theme.tag_browser_height + 200)
    widget.set_tags(_tags())
    QApplication.processEvents()
    pool = widget.findChild(QFrame, "TagBrowserPool")
    assert pool is not None
    assert pool.height() == Theme.tag_browser_pool_height

    widget.set_selected_tag_ids([tag.tag_id for tag in _tags()])
    QApplication.processEvents()

    assert len(_tray_chips(widget)) == len(NAMES)
    assert pool.height() == Theme.tag_browser_pool_height
    # The tray is capped so a large selection scrolls instead of pushing the
    # group list out of the pool entirely.
    assert widget._tray_scroll.height() <= Theme.tag_browser_tray_max_height


def test_clicking_a_chip_reports_the_tag_without_changing_selection(browser, qtbot) -> None:
    """Selection belongs to the caller: the browser reports the click and
    repaints only what it is handed back."""

    widget = browser(show_tray=True)
    widget.set_tags(_tags())
    seen: list[tuple[str, bool]] = []
    widget.tag_clicked.connect(lambda tag_id, additive: seen.append((tag_id, additive)))

    chip = _pool_chips(widget)[0]
    qtbot.mouseClick(chip, Qt.MouseButton.LeftButton)

    assert seen == [(chip.tag_id, False)]
    assert widget.selected_tag_ids == ()


def test_add_chip_appears_only_when_requested_and_not_while_searching(browser, qtbot) -> None:
    widget = browser(include_add_chip=True)
    widget.set_tags(_tags())

    assert any(chip.is_add_chip for chip in widget.chip_widgets)

    # "+" is a command, not a tag; it would be meaningless in search results.
    _search(qtbot, widget, "adv")
    assert not any(chip.is_add_chip for chip in widget.chip_widgets)

    plain = browser()
    plain.set_tags(_tags())
    assert not any(chip.is_add_chip for chip in plain.chip_widgets)


def test_switching_han_language_rebuckets_shared_han_tags(browser) -> None:
    widget = browser()
    widget.set_tags([Tag("renai", "恋愛")])

    assert widget.rail_letters == ("R",)  # ren'ai

    widget.set_han_language("zh")

    assert widget.rail_letters == ("L",)  # lian'ai


def test_unpinned_han_language_follows_active_locale_on_refresh(browser) -> None:
    try:
        locale_service.load_language("Chinese")
        widget = browser()
        widget.set_tags([Tag("renai", "恋愛")])

        assert widget.rail_letters == ("L",)

        locale_service.load_language("Japanese")
        widget.refresh_labels()

        assert widget.rail_letters == ("R",)
    finally:
        locale_service.load_language("English")


def test_typing_does_not_rebuild_until_the_query_is_committed(browser, qtbot) -> None:
    """The whole point of Enter-to-search: keystrokes are free.

    Rebuilding per character costs the entire library each time (54ms at
    1,000 tags, 282ms at the 5,000-tag cap), for a query the user has not
    finished describing.
    """

    widget = browser()
    widget.set_tags(_tags())
    before = [chip.tag_id for chip in _pool_chips(widget)]

    widget.search_field.setText("a")
    widget.search_field.setText("ad")
    widget.search_field.setText("adv")

    assert [chip.tag_id for chip in _pool_chips(widget)] == before

    qtbot.keyClick(widget.search_field, Qt.Key.Key_Return)

    assert [chip.tag_id for chip in _pool_chips(widget)] == ["adventure"]


def test_committing_the_same_query_twice_does_not_rebuild(browser, qtbot) -> None:
    widget = browser()
    widget.set_tags(_tags())
    _search(qtbot, widget, "adv")
    chip = _pool_chips(widget)[0]

    qtbot.keyClick(widget.search_field, Qt.Key.Key_Return)

    # Same chip object: pressing Enter again rebuilt nothing.
    assert _pool_chips(widget)[0] is chip


def test_emptying_the_field_restores_the_full_list_without_enter(browser) -> None:
    """Clearing is a cancel, not a search. Leaving stale results behind an
    empty box would read as a bug."""

    widget = browser()
    widget.set_tags(_tags())
    widget.search_field.setText("adv")
    widget.search_field.returnPressed.emit()
    assert len(_pool_chips(widget)) == 1

    widget.search_field.setText("")

    assert len(_pool_chips(widget)) == len(NAMES)


def test_the_clear_button_applies_immediately(browser, qtbot) -> None:
    widget = browser()
    widget.set_tags(_tags())
    _search(qtbot, widget, "adv")
    assert len(_pool_chips(widget)) == 1

    qtbot.mouseClick(widget._clear_button, Qt.MouseButton.LeftButton)

    assert widget.search_field.text() == ""
    assert len(_pool_chips(widget)) == len(NAMES)
