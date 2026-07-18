from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import sys

from PySide6.QtCore import QLockFile
import pytest

from joyread.app.launch_intent import MAX_LAUNCH_MESSAGE_BYTES, LaunchAction, LaunchIntent
from joyread.app.single_instance_broker import (
    InstanceRole,
    SingleInstanceBroker,
    SingleInstanceError,
)


def test_secondary_forwards_to_primary(qtbot, tmp_path: Path) -> None:
    primary = SingleInstanceBroker(tmp_path)
    secondary = SingleInstanceBroker(tmp_path)
    received: list[LaunchIntent] = []
    source = tmp_path / "book.cbz"

    try:
        assert primary.start(LaunchIntent.show_library()) == InstanceRole.PRIMARY
        primary.set_intent_handler(received.append)
        assert secondary.start(LaunchIntent.open_files((source,))) == InstanceRole.SECONDARY

        qtbot.waitUntil(lambda: len(received) == 1, timeout=1000)
        assert received == [LaunchIntent.open_files((source,))]
    finally:
        secondary.dispose()
        primary.dispose()


def test_primary_queues_intents_until_handler_is_ready(qtbot, tmp_path: Path) -> None:
    primary = SingleInstanceBroker(tmp_path)
    secondary = SingleInstanceBroker(tmp_path)
    intent = LaunchIntent.show_library()
    received: list[LaunchIntent] = []

    try:
        assert primary.start(intent) == InstanceRole.PRIMARY
        assert secondary.start(intent) == InstanceRole.SECONDARY
        qtbot.waitUntil(lambda: bool(primary._pending_intents), timeout=1000)

        primary.set_intent_handler(received.append)
        assert received == [intent]
    finally:
        secondary.dispose()
        primary.dispose()


def test_locked_profile_without_server_never_becomes_a_second_primary(tmp_path: Path) -> None:
    broker = SingleInstanceBroker(tmp_path, connect_timeout_ms=250)
    broker.lock_path.parent.mkdir(parents=True)
    foreign_lock = QLockFile(str(broker.lock_path))
    foreign_lock.setStaleLockTime(0)
    assert foreign_lock.tryLock(0)

    try:
        with pytest.raises(SingleInstanceError, match="could not receive"):
            broker.start(LaunchIntent.show_library())
        assert broker.role is None
    finally:
        broker.dispose()
        foreign_lock.unlock()


def test_crashed_process_lock_is_recovered(tmp_path: Path) -> None:
    broker = SingleInstanceBroker(tmp_path)
    broker.lock_path.parent.mkdir(parents=True)
    script = """
import os
import sys
from PySide6.QtCore import QLockFile

lock = QLockFile(sys.argv[1])
lock.setStaleLockTime(0)
if not lock.tryLock(1000):
    raise SystemExit(2)
os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-c", script, str(broker.lock_path)],
        check=True,
    )
    assert broker.lock_path.exists()

    try:
        assert broker.start(LaunchIntent.show_library()) == InstanceRole.PRIMARY
    finally:
        broker.dispose()


def test_handler_failure_does_not_stop_later_launches(qtbot, tmp_path: Path, caplog) -> None:
    primary = SingleInstanceBroker(tmp_path)
    failing_secondary = SingleInstanceBroker(tmp_path)
    succeeding_secondary = SingleInstanceBroker(tmp_path)
    received: list[LaunchIntent] = []

    def fail_handler(_intent: LaunchIntent) -> None:
        raise RuntimeError("simulated window construction failure")

    try:
        assert primary.start(LaunchIntent.show_library()) == InstanceRole.PRIMARY
        primary.set_intent_handler(fail_handler)

        with caplog.at_level(logging.ERROR, logger="joyread.app.single_instance_broker"):
            assert failing_secondary.start(LaunchIntent.show_library()) == InstanceRole.SECONDARY
            qtbot.waitUntil(
                lambda: "Launch intent handler failed" in caplog.text,
                timeout=1000,
            )

        primary.set_intent_handler(received.append)
        follow_up = LaunchIntent.open_files((tmp_path / "follow-up.cbz",))
        assert succeeding_secondary.start(follow_up) == InstanceRole.SECONDARY
        qtbot.waitUntil(lambda: received == [follow_up], timeout=1000)
    finally:
        succeeding_secondary.dispose()
        failing_secondary.dispose()
        primary.dispose()


def test_secondary_rejects_oversized_outbound_intent(tmp_path: Path) -> None:
    primary = SingleInstanceBroker(tmp_path)
    secondary = SingleInstanceBroker(tmp_path)
    oversized = LaunchIntent(
        LaunchAction.OPEN_FILES,
        (Path(f"{'x' * MAX_LAUNCH_MESSAGE_BYTES}.cbz"),),
    )

    try:
        assert primary.start(LaunchIntent.show_library()) == InstanceRole.PRIMARY
        with pytest.raises(SingleInstanceError, match="cannot forward.*maximum IPC message size"):
            secondary.start(oversized)
        assert secondary.role is None
    finally:
        secondary.dispose()
        primary.dispose()


@pytest.mark.skipif(os.name == "nt", reason="Windows paths cannot contain surrogate escapes.")
def test_secondary_forwards_surrogateescaped_filename(qtbot, tmp_path: Path) -> None:
    primary = SingleInstanceBroker(tmp_path)
    secondary = SingleInstanceBroker(tmp_path)
    received: list[LaunchIntent] = []
    intent = LaunchIntent.open_files((tmp_path / "chapter-\udcff.cbz",))

    try:
        assert primary.start(LaunchIntent.show_library()) == InstanceRole.PRIMARY
        primary.set_intent_handler(received.append)
        assert secondary.start(intent) == InstanceRole.SECONDARY
        qtbot.waitUntil(lambda: received == [intent], timeout=1000)
    finally:
        secondary.dispose()
        primary.dispose()
