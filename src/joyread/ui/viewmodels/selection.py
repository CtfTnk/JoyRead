"""Reusable UI selection rules for pill/list style interactions."""

from __future__ import annotations


def toggle_selection(selected_ids: set[str], target_id: str, *, additive: bool) -> set[str]:
    """Return the next selection for a normal click or Shift-click.

    Normal click behaves like a single-selection pill group with deselect:
    clicking the only selected item clears the group; every other normal
    click selects only the target. Shift-click toggles membership.
    """

    next_selected = set(selected_ids)
    if additive:
        if target_id in next_selected:
            next_selected.remove(target_id)
        else:
            next_selected.add(target_id)
        return next_selected
    if next_selected == {target_id}:
        return set()
    return {target_id}
