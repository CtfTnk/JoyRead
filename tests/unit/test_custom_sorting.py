from dataclasses import replace
from datetime import datetime, timedelta
import sqlite3

import pytest

from joyread.core.models.shelf_order import ShelfOrder, merge_order, move_group, newest_first
from joyread.core.repositories.shelf_order_sql import list_shelf_orders, save_shelf_order
from joyread.core.services.library_service import LibraryService
from joyread.infrastructure.config.settings_store import AppSettings
from joyread.infrastructure.database.migrations import apply_migrations
from joyread.ui.viewmodels.shelf_viewmodel import ShelfViewModel, SortField, FileFilter
from tests.support.in_memory_book_repository import InMemoryBookRepository


def sample_books():
    base = InMemoryBookRepository().list_books()[0]
    return [replace(base, uuid=str(i), title=f"Book {i}", added_at=datetime(2026, 1, 10) - timedelta(days=i),
                    is_hidden=False, is_favourite=True, collection_ids=("collection-a",)) for i in range(1, 6)]


def make_vm(repository=None, **kwargs):
    vm = ShelfViewModel(LibraryService(repository or InMemoryBookRepository(books=sample_books())), **kwargs)
    vm.load_books()
    return vm


def ids(vm):
    return tuple(book.uuid for book in vm.visible_books)


def test_group_order_is_source_order_not_click_order():
    assert move_group(("1", "2", "3", "4", "5"), {"3", "5", "1"}, "4") == ("2", "1", "3", "5", "4")
    assert move_group(("1", "2", "3"), {"1", "2", "3"}, None) == ("1", "2", "3")
    assert move_group(("1", "2", "3"), {"3"}, "1") == ("3", "1", "2")
    assert move_group(("1", "2", "3"), {"1"}, None) == ("2", "3", "1")
    assert move_group(("1", "2", "3"), {"2"}, "3") == ("1", "2", "3")


def test_newest_first_has_stable_ties_and_prepends_new_members():
    books = sample_books()
    tied = [replace(book, added_at=books[0].added_at) for book in reversed(books)]
    assert newest_first(tied) == ("1", "2", "3", "4", "5")
    assert merge_order(books, ("5", "1", "3")) == ("2", "4", "5", "1", "3")


def test_scoped_modes_order_and_restart():
    repo = InMemoryBookRepository(books=sample_books())
    vm = make_vm(repo)
    vm.set_sort("Custom", True)
    assert vm.sort_ascending is False and vm.can_reorder
    vm.set_selection({"3", "5", "1"})
    assert vm.drag_group("3") == ("1", "3", "5")
    vm.reorder_books(("3", "5", "1"), "4")
    assert ids(vm) == ("2", "1", "3", "5", "4")
    vm.set_current_shelf("favourites")
    assert vm.sort_field == SortField.ADD_TIME
    vm.set_sort("Custom")
    assert ids(vm) == ("1", "2", "3", "4", "5")
    vm.reorder_books(("5",), "1")
    vm.set_current_shelf("all")
    assert ids(vm) == ("2", "1", "3", "5", "4")
    restarted = make_vm(repo)
    assert restarted.sort_field == SortField.CUSTOM and ids(restarted) == ids(vm)
    restarted.set_sort("Title")
    restarted.set_sort("Custom")
    assert ids(restarted) == ids(vm)
    restarted.set_current_shelf("collection:collection-a")
    restarted.set_sort("Custom")
    assert ids(restarted) == ("1", "2", "3", "4", "5")


def test_filters_and_recent_disable_reorder_but_keep_selection():
    vm = make_vm()
    vm.set_sort("Custom")
    vm.set_search_query("Book")
    assert not vm.can_reorder
    vm.set_selection({"1", "2"})
    assert vm.selected_book_ids == {"1", "2"}
    vm.set_search_query("")
    vm.set_filter(FileFilter.PDF.value)
    assert not vm.can_reorder
    vm.set_filter(FileFilter.ALL.value)
    vm.set_current_shelf("recent")
    vm.set_sort("Custom")
    assert vm.sort_field != SortField.CUSTOM and not vm.can_reorder


def test_temporarily_hidden_books_keep_slots_and_new_books_prepend():
    repo = InMemoryBookRepository(books=sample_books())
    vm = make_vm(repo)
    vm.set_sort("Custom")
    repo._books[1] = replace(repo._books[1], is_hidden=True)
    vm.load_books()
    vm.reorder_books(("5",), "1")
    assert vm._shelf_orders["all"].book_ids == ("5", "2", "1", "3", "4")
    repo._books[1] = replace(repo._books[1], is_hidden=False)
    repo._books.append(replace(repo._books[0], uuid="new", added_at=datetime(2026, 2, 1)))
    vm.load_books()
    assert ids(vm) == ("new", "5", "2", "1", "3", "4")


class DeferredTasks:
    def __init__(self): self.pending = []
    def submit(self, name, callback, **kwargs):
        self.pending.append((callback, kwargs))
    def finish(self):
        callback, options = self.pending.pop(0)
        try:
            result = callback()
        except Exception as error:
            options["on_failure"](error)
        else:
            options["on_success"](result)


def test_async_save_disables_reorder_and_failure_restores_committed_order(monkeypatch):
    repo = InMemoryBookRepository(books=sample_books())
    vm = make_vm(repo)
    vm.set_sort("Custom")
    tasks = DeferredTasks()
    vm._task_service = tasks
    errors = []
    vm.sort_failed.connect(errors.append)
    vm.reorder_books(("5",), "1")
    assert ids(vm)[0] == "5" and vm.sort_saving and not vm.can_reorder
    monkeypatch.setattr(repo, "save_shelf_order", lambda state: (_ for _ in ()).throw(OSError("Disk full")))
    tasks.finish()
    assert ids(vm) == ("1", "2", "3", "4", "5")
    assert not vm.sort_saving and errors


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    apply_migrations(connection)
    for i in range(1, 6):
        connection.execute("""INSERT INTO book_files(file_id, original_path, original_file_name, storage_path, file_format, hash_algorithm,
            source_hash, stored_hash, storage_kind, state, created_at, updated_at) VALUES (?, '', '', ?, 'cbz', 'sha256', ?, 'hash', 'verbatim', 'healthy', '', '')""", (str(i), f"{i}.cbz", str(i)))
        connection.execute("""INSERT INTO books(book_id, file_id, title, author, book_type, is_favourite, is_hidden, created_at, updated_at)
            VALUES (?, ?, ?, '', 'manga', 1, 1, '', '')""", (str(i), str(i), str(i)))
    connection.execute("INSERT INTO collections(collection_id, name, created_at, updated_at) VALUES ('a', 'A', '', '')")
    connection.execute("INSERT INTO collection_books VALUES ('a','1','')")
    yield connection
    connection.close()


def test_sql_persistence_and_membership_cleanup(db):
    for scope in ("all", "favourites", "hidden", "collection:a"):
        save_shelf_order(db, ShelfOrder(scope, "Custom", False, True, ("1", "2", "3", "4", "5")))
    db.execute("UPDATE books SET is_favourite=0, is_hidden=0 WHERE book_id='1'")
    db.execute("DELETE FROM collection_books WHERE book_id='1'")
    states = list_shelf_orders(db)
    assert states["all"].book_ids == ("1", "2", "3", "4", "5")
    assert states["favourites"].book_ids == ("2", "3", "4", "5")
    assert states["hidden"].book_ids == ("2", "3", "4", "5")
    assert states["collection:a"].book_ids == ()
    db.execute("DELETE FROM collections WHERE collection_id='a'")
    assert "collection:a" not in list_shelf_orders(db)
    db.execute("DELETE FROM books WHERE book_id='1'")
    assert list_shelf_orders(db)["all"].book_ids == ("2", "3", "4", "5")


def test_sql_write_is_atomic(db):
    initial = ShelfOrder("all", "Custom", False, True, ("1", "2", "3", "4", "5"))
    save_shelf_order(db, initial)
    db.execute("""CREATE TRIGGER fail_order BEFORE INSERT ON shelf_book_order
        WHEN NEW.position=1 BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        save_shelf_order(db, replace(initial, sort_field="Title", book_ids=("3", "2", "1")))
    assert list_shelf_orders(db)["all"] == initial


def test_successive_membership_batches_prepend_without_reversing_older_batches(db):
    save_shelf_order(db, ShelfOrder("collection:a", "Custom", False, True, ("1",)))
    db.execute("INSERT INTO collection_books VALUES ('a','3','')")
    assert list_shelf_orders(db)["collection:a"].book_ids == ("3", "1")
    db.execute("INSERT INTO collection_books VALUES ('a','5','')")
    db.execute("INSERT INTO collection_books VALUES ('a','4','')")
    assert list_shelf_orders(db)["collection:a"].book_ids == ("4", "5", "3", "1")
    # Re-reading/restarting no longer treats old additions as a new batch.
    assert list_shelf_orders(db)["collection:a"].book_ids == ("4", "5", "3", "1")
    db.execute("DELETE FROM collection_books WHERE collection_id='a' AND book_id='1'")
    db.execute("INSERT INTO collection_books VALUES ('a','1','')")
    assert list_shelf_orders(db)["collection:a"].book_ids == ("1", "4", "5", "3")


def test_upgrade_from_13_retains_data_and_has_no_new_preferences(monkeypatch):
    import joyread.infrastructure.database.migrations as migrations
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    with monkeypatch.context() as context:
        context.setattr(migrations, "MIGRATIONS", tuple(item for item in migrations.MIGRATIONS if item[0] <= 13))
        migrations.apply_migrations(connection)
    connection.execute("INSERT INTO collections(collection_id,name,created_at,updated_at) VALUES ('old','Old collection','','')")
    migrations.apply_migrations(connection)
    assert connection.execute("SELECT name FROM collections WHERE collection_id='old'").fetchone()[0] == "Old collection"
    assert list_shelf_orders(connection) == {}
    connection.close()
