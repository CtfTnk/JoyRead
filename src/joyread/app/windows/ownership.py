"""Qt-free lifetime graph for top-level windows.

JoyRead has two kinds of top-level window, and they must not share a lifetime:

* Windows the Library opened on the user's behalf are **owned** by the Library.
  Closing Main closes them, because the user perceives them as part of the
  Library session they just dismissed.
* Windows the operating system asked for ("Open With", a forwarded launch
  request) are **roots**. They outlive Main, because the user never opened a
  Library to begin with.

The same document can be requested through both routes. When that happens the
window is promoted to a root: an OS request is an explicit statement that this
window should stand on its own, and silently destroying it later with Main
would lose a window the user believes they opened independently.
"""

from __future__ import annotations

from collections.abc import Iterable


class WindowOwnership:
    """Track which windows die with their owner and which stand alone.

    Keys are opaque and caller-supplied; this class never touches Qt so the
    policy can be tested without constructing widgets.
    """

    def __init__(self) -> None:
        self._parents: dict[object, object] = {}
        self._children: dict[object, list[object]] = {}
        self._roots: set[object] = set()

    def add_root(self, key: object) -> None:
        """Register a window with no owner, or detach one that already exists."""

        if key in self._parents:
            self.promote_to_root(key)
            return
        self._roots.add(key)
        self._children.setdefault(key, [])

    def add_child(self, key: object, *, owner: object) -> None:
        """Register ``key`` as owned by ``owner``.

        Registering an existing root as a child is ignored: promotion to root is
        deliberately one-way, so an OS-requested window can never be recaptured
        by the Library and closed out from under the user.
        """

        if key in self._roots:
            return
        existing_owner = self._parents.get(key)
        if existing_owner == owner:
            return
        if existing_owner is not None:
            self._detach(key, existing_owner)
        self._parents[key] = owner
        self._children.setdefault(owner, []).append(key)
        self._children.setdefault(key, [])

    def promote_to_root(self, key: object) -> None:
        """Detach ``key`` from its owner so the owner's close no longer takes it."""

        owner = self._parents.pop(key, None)
        if owner is not None:
            self._detach(key, owner)
        self._roots.add(key)
        self._children.setdefault(key, [])

    def is_root(self, key: object) -> bool:
        return key in self._roots

    def owner_of(self, key: object) -> object | None:
        return self._parents.get(key)

    def children_of(self, key: object) -> tuple[object, ...]:
        return tuple(self._children.get(key, ()))

    def descendants_of(self, key: object) -> tuple[object, ...]:
        """Return owned windows depth-first, deepest last.

        Ownership is one level deep today, but a Reader that learns to spawn its
        own windows should not silently leak them when its owner closes.
        """

        ordered: list[object] = []
        seen: set[object] = {key}
        pending: list[object] = list(self._children.get(key, ()))
        while pending:
            current = pending.pop(0)
            if current in seen:
                continue
            seen.add(current)
            ordered.append(current)
            pending.extend(self._children.get(current, ()))
        return tuple(ordered)

    def remove(self, key: object) -> None:
        """Forget ``key``; any windows it owned become roots rather than orphans."""

        owner = self._parents.pop(key, None)
        if owner is not None:
            self._detach(key, owner)
        self._roots.discard(key)
        for child in self._children.pop(key, []):
            if self._parents.get(child) == key:
                self._parents.pop(child, None)
                self._roots.add(child)

    def keys(self) -> tuple[object, ...]:
        return tuple(self._children)

    def _detach(self, key: object, owner: object) -> None:
        siblings = self._children.get(owner)
        if siblings is None:
            return
        self._children[owner] = [child for child in siblings if child != key]


def ordered_close_sequence(
    ownership: WindowOwnership,
    owner: object,
    *,
    known: Iterable[object] | None = None,
) -> tuple[object, ...]:
    """Return the owned windows to close, deepest first.

    Closing deepest-first means a nested owner never re-enters this function
    with children that were already destroyed.
    """

    live = None if known is None else set(known)
    descendants = ownership.descendants_of(owner)
    if live is not None:
        descendants = tuple(key for key in descendants if key in live)
    return tuple(reversed(descendants))
