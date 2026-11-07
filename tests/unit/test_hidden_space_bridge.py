"""HiddenSpaceBridge: vault and sealing events reach the GUI thread, later and gathered."""

from __future__ import annotations

from collections.abc import Callable
import threading

from PySide6.QtWidgets import QApplication

from joyread.app.hidden_space_bridge import HiddenSpaceBridge
from joyread.core.vault import VaultKeyring, VaultPrivateKey


class _Sealing:
    """The listener half of SealingService, reporting when told to."""

    def __init__(self) -> None:
        self.listeners: list[Callable[[tuple[str, ...]], None]] = []

    def add_listener(self, listener: Callable[[tuple[str, ...]], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def report(self, *book_ids: str) -> None:
        for listener in tuple(self.listeners):
            listener(book_ids)


def _unlocked_keyring() -> VaultKeyring:
    keyring = VaultKeyring()
    keyring.unlock(VaultPrivateKey.generate())
    return keyring


def test_a_lock_on_a_worker_thread_is_reported_on_the_gui_thread(qtbot) -> None:  # noqa: ANN001
    keyring = _unlocked_keyring()
    bridge = HiddenSpaceBridge(keyring)
    threads: list[int] = []
    bridge.locked.connect(lambda: threads.append(threading.get_ident()))
    try:
        worker = threading.Thread(target=keyring.lock)
        worker.start()
        worker.join()

        qtbot.waitUntil(lambda: len(threads) == 1)
        assert threads == [threading.main_thread().ident]
    finally:
        bridge.close()


def test_a_gui_thread_lock_is_reported_only_after_the_lock_returns(qtbot) -> None:  # noqa: ANN001
    keyring = _unlocked_keyring()
    bridge = HiddenSpaceBridge(keyring)
    seen: list[bool] = []
    bridge.locked.connect(lambda: seen.append(keyring.is_unlocked))
    try:
        keyring.lock()
        # Closing windows from inside VaultKeyring.lock would re-enter
        # whatever command asked for the lock.
        assert seen == []
        qtbot.waitUntil(lambda: seen == [False])
    finally:
        bridge.close()


def test_a_closed_bridge_hears_no_more_locks(qtbot) -> None:  # noqa: ANN001
    keyring = _unlocked_keyring()
    bridge = HiddenSpaceBridge(keyring)
    seen: list[bool] = []
    bridge.locked.connect(lambda: seen.append(True))

    bridge.close()
    keyring.lock()
    qtbot.wait(20)

    assert seen == []


def test_sealing_reports_are_gathered_into_one_reload(qtbot) -> None:  # noqa: ANN001
    bridge = HiddenSpaceBridge(VaultKeyring())
    sealing = _Sealing()
    bridge.attach_sealing(sealing)  # type: ignore[arg-type]
    batches: list[tuple[str, ...]] = []
    bridge.books_changed.connect(batches.append)
    try:
        # One job reports queued, written and committed; a second book joins.
        sealing.report("b")
        sealing.report("b")
        sealing.report("a", "b")

        qtbot.waitUntil(lambda: batches != [])
        qtbot.wait(200)
        assert batches == [("a", "b")]
    finally:
        bridge.close()


def test_a_report_queued_by_the_previous_library_is_dropped(qtbot) -> None:  # noqa: ANN001
    bridge = HiddenSpaceBridge(VaultKeyring())
    first, second = _Sealing(), _Sealing()
    bridge.attach_sealing(first)  # type: ignore[arg-type]
    batches: list[tuple[str, ...]] = []
    bridge.books_changed.connect(batches.append)
    try:
        first.report("old")  # Queued, not yet delivered...
        bridge.attach_sealing(second)  # type: ignore[arg-type] # ...when a storage change swaps Libraries.
        assert first.listeners == []
        second.report("new")

        qtbot.waitUntil(lambda: batches != [])
        QApplication.processEvents()
        assert batches == [("new",)]
    finally:
        bridge.close()
    assert second.listeners == []
