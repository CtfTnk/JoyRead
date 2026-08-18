from __future__ import annotations

from joyread.app.windows.activation import (
    ReopenDecision,
    WindowActivationRegistry,
    decide_reopen,
)


def test_reopen_shows_the_library_when_nothing_is_open() -> None:
    registry = WindowActivationRegistry()

    assert decide_reopen(registry) == ReopenDecision.SHOW_LIBRARY


def test_reopen_activates_the_most_recently_used_window() -> None:
    registry = WindowActivationRegistry()
    registry.record_activation("main")
    registry.record_activation("reader")

    assert decide_reopen(registry) == ReopenDecision.ACTIVATE_RECENT
    assert registry.most_recent() == "reader"


def test_reactivation_moves_a_known_window_to_the_front_without_duplicating() -> None:
    registry = WindowActivationRegistry()
    registry.record_activation("main")
    registry.record_activation("reader")
    registry.record_activation("main")

    assert registry.ordered() == ("main", "reader")
    assert len(registry) == 2


def test_forgetting_the_front_window_falls_back_to_the_next_one() -> None:
    registry = WindowActivationRegistry()
    registry.record_activation("main")
    registry.record_activation("reader")

    registry.forget("reader")

    assert registry.most_recent() == "main"
    assert decide_reopen(registry) == ReopenDecision.ACTIVATE_RECENT

    registry.forget("main")

    assert decide_reopen(registry) == ReopenDecision.SHOW_LIBRARY


def test_integer_keys_compare_by_value_not_identity() -> None:
    registry = WindowActivationRegistry()
    key = 10**18
    registry.record_activation(key)

    registry.forget(int(str(key)))

    assert len(registry) == 0
