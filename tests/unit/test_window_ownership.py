from __future__ import annotations

from joyread.app.windows.ownership import WindowOwnership, ordered_close_sequence


def test_children_close_with_their_owner_but_roots_do_not() -> None:
    ownership = WindowOwnership()
    ownership.add_root("main")
    ownership.add_root("external")
    ownership.add_child("owned", owner="main")

    assert ownership.is_root("external")
    assert not ownership.is_root("owned")
    assert ownership.owner_of("owned") == "main"
    assert ordered_close_sequence(ownership, "main") == ("owned",)


def test_promotion_detaches_a_child_from_its_owner() -> None:
    ownership = WindowOwnership()
    ownership.add_root("main")
    ownership.add_child("reader", owner="main")

    ownership.promote_to_root("reader")

    assert ownership.is_root("reader")
    assert ownership.owner_of("reader") is None
    assert ordered_close_sequence(ownership, "main") == ()


def test_promotion_is_one_way_so_the_library_cannot_recapture_a_root() -> None:
    ownership = WindowOwnership()
    ownership.add_root("main")
    ownership.add_child("reader", owner="main")
    ownership.promote_to_root("reader")

    ownership.add_child("reader", owner="main")

    assert ownership.is_root("reader")
    assert ordered_close_sequence(ownership, "main") == ()


def test_nested_ownership_closes_deepest_first() -> None:
    ownership = WindowOwnership()
    ownership.add_root("main")
    ownership.add_child("reader", owner="main")
    ownership.add_child("panel", owner="reader")

    assert ordered_close_sequence(ownership, "main") == ("panel", "reader")


def test_close_sequence_skips_windows_that_are_already_gone() -> None:
    ownership = WindowOwnership()
    ownership.add_root("main")
    ownership.add_child("reader", owner="main")
    ownership.add_child("panel", owner="reader")

    sequence = ordered_close_sequence(ownership, "main", known={"main", "reader"})

    assert sequence == ("reader",)


def test_removing_an_owner_leaves_its_children_as_roots() -> None:
    """A child must never keep pointing at an owner that no longer exists."""

    ownership = WindowOwnership()
    ownership.add_root("main")
    ownership.add_child("reader", owner="main")

    ownership.remove("main")

    assert ownership.is_root("reader")
    assert ownership.owner_of("reader") is None


def test_integer_keys_compare_by_value_not_identity() -> None:
    """``id()`` values are large ints and are not interned; keys must use ==."""

    owner = 10**18
    child = 10**18 + 1
    ownership = WindowOwnership()
    ownership.add_root(owner)
    ownership.add_child(child, owner=owner)

    ownership.promote_to_root(int(str(child)))

    assert ordered_close_sequence(ownership, int(str(owner))) == ()
