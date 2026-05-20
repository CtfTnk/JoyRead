from joyread.ui.viewmodels.selection import toggle_selection


def test_normal_click_selects_unselected_item() -> None:
    selected = set[str]()

    result = toggle_selection(selected, "tag-1", additive=False)

    assert result == {"tag-1"}
    assert selected == set()


def test_normal_click_on_only_selected_item_clears_selection() -> None:
    result = toggle_selection({"tag-1"}, "tag-1", additive=False)

    assert result == set()


def test_normal_click_on_selected_item_among_many_keeps_only_that_item() -> None:
    result = toggle_selection({"tag-1", "tag-2"}, "tag-1", additive=False)

    assert result == {"tag-1"}


def test_normal_click_on_unselected_item_among_many_keeps_only_target() -> None:
    result = toggle_selection({"tag-1", "tag-2"}, "tag-3", additive=False)

    assert result == {"tag-3"}


def test_shift_click_toggles_selected_membership() -> None:
    result = toggle_selection({"tag-1", "tag-2"}, "tag-2", additive=True)

    assert result == {"tag-1"}


def test_shift_click_toggles_unselected_membership() -> None:
    result = toggle_selection({"tag-1"}, "tag-2", additive=True)

    assert result == {"tag-1", "tag-2"}
