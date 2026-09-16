"""Shelf-order SQL executed by the existing database actor."""
import sqlite3

from joyread.core.models.shelf_order import ShelfOrder


def _read_shelf_orders(connection: sqlite3.Connection) -> dict[str, ShelfOrder]:
    positions: dict[str, list[str]] = {}
    for row in connection.execute("SELECT scope, book_id FROM shelf_book_order ORDER BY scope, position"):
        positions.setdefault(row["scope"], []).append(row["book_id"])
    return {
        row["scope"]: ShelfOrder(row["scope"], row["sort_field"], bool(row["ascending"]),
                                 bool(row["initialized"]), tuple(positions.get(row["scope"], ())))
        for row in connection.execute("SELECT * FROM shelf_sort_preferences")
    }



def _members(connection: sqlite3.Connection, scope: str) -> tuple[str, ...]:
    if scope.startswith("collection:"):
        rows = connection.execute("""SELECT books.book_id FROM books JOIN collection_books USING(book_id)
            WHERE collection_id = ? ORDER BY books.created_at DESC, books.book_id ASC""",
            (scope.removeprefix("collection:"),))
    else:
        condition = {"all": "1", "favourites": "is_favourite = 1", "hidden": "is_hidden = 1"}[scope]
        rows = connection.execute(f"SELECT book_id FROM books WHERE {condition} ORDER BY created_at DESC, book_id ASC")
    return tuple(row[0] for row in rows)


def list_shelf_orders(connection: sqlite3.Connection) -> dict[str, ShelfOrder]:
    """Reconcile new members as one batch, then return committed preferences.

    Persist the new prefix now: otherwise two successive additions would both
    remain unranked and could change order on the next restart. Service mutation
    batches call this once after their membership updates, never once per book.
    """
    from dataclasses import replace
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        states = _read_shelf_orders(connection)
        for scope, state in tuple(states.items()):
            if not state.initialized:
                continue
            members = _members(connection, scope)
            present, known = set(members), set(state.book_ids)
            merged = tuple(key for key in members if key not in known) + tuple(key for key in state.book_ids if key in present)
            if merged != state.book_ids:
                connection.execute("DELETE FROM shelf_book_order WHERE scope = ?", (scope,))
                connection.executemany("INSERT INTO shelf_book_order VALUES (?, ?, ?)",
                                       ((scope, key, i) for i, key in enumerate(merged)))
                states[scope] = replace(state, book_ids=merged)
        return states

def save_shelf_order(connection: sqlite3.Connection, state: ShelfOrder) -> None:
    collection_id = state.scope.removeprefix("collection:") if state.scope.startswith("collection:") else None
    # Preferences and positions are one commit. The existing connection's
    # transaction context rolls back the whole operation on any write failure.
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            INSERT INTO shelf_sort_preferences(scope, collection_id, sort_field, ascending, initialized)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET sort_field=excluded.sort_field,
                ascending=excluded.ascending, initialized=excluded.initialized
        """, (state.scope, collection_id, state.sort_field, state.ascending, state.initialized))
        connection.execute("DELETE FROM shelf_book_order WHERE scope = ?", (state.scope,))
        # An import may finish after drop: absent IDs remain unranked and are
        # prepended by merge_order. Deleted or removed members are not revived.
        if collection_id is not None:
            members = {row[0] for row in connection.execute(
                "SELECT book_id FROM collection_books WHERE collection_id = ?", (collection_id,))}
        else:
            condition = {"all": "1", "favourites": "is_favourite = 1", "hidden": "is_hidden = 1"}[state.scope]
            members = {row[0] for row in connection.execute(f"SELECT book_id FROM books WHERE {condition}")}
        connection.executemany(
            "INSERT INTO shelf_book_order(scope, book_id, position) VALUES (?, ?, ?)",
            ((state.scope, key, index) for index, key in enumerate(state.book_ids) if key in members),
        )
