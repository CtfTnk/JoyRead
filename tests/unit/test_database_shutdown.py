"""Closing the database is a resource-release barrier for storage operations."""
from threading import Event, Thread

import pytest

from joyread.infrastructure.database import database_interpreter as module
from joyread.infrastructure.database.database_interpreter import DatabaseInterpreter


def test_close_waits_for_connection_release_and_can_retry_timeout(monkeypatch, tmp_path):
    entered = Event()
    release = Event()
    closed = Event()
    original = module.open_sqlite_connection

    class SlowClosingConnection:
        def __init__(self, path):
            self.connection = original(path)

        def execute(self, *args):
            return self.connection.execute(*args)

        def close(self):
            entered.set()
            try:
                assert release.wait(5), "Test did not release the simulated SQLite close"
            finally:
                self.connection.close()
                closed.set()

    monkeypatch.setattr(module, "open_sqlite_connection", SlowClosingConnection)
    database = DatabaseInterpreter(tmp_path / "library.sqlite3")
    assert database.execute(lambda conn: conn.execute("SELECT 1").fetchone()[0]) == 1
    try:
        with pytest.raises(TimeoutError, match="still owns"):
            database.close(timeout=0.01)
        assert entered.wait(1)
        assert not closed.is_set()
        with pytest.raises(RuntimeError, match="closed"):
            database.submit(lambda conn: None)
        finished = Event()
        waiter = Thread(target=lambda: (database.close(), finished.set()))
        waiter.start()
        assert not finished.wait(0.05), "A repeated close must still wait for release"
        release.set()
        waiter.join(2)
        assert finished.is_set() and closed.is_set()
        assert not database._thread.is_alive()
        database.database_path.unlink()  # Windows can now remove the file too.
    finally:
        release.set()
        database.close()


def test_close_drains_accepted_requests_before_return(tmp_path):
    database = DatabaseInterpreter(tmp_path / "library.sqlite3", autostart=False)
    completed = []
    futures = [database.submit(lambda conn, i=i: completed.append(i)) for i in range(3)]
    database.start()
    database.close()
    assert completed == [0, 1, 2]
    assert all(future.done() for future in futures)
    database.close()
