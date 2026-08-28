from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
import subprocess
import sys

from PySide6.QtCore import QLockFile
import pytest

from joyread.app.launch.intent import (
    MAX_LAUNCH_MESSAGE_BYTES,
    LaunchAction,
    LaunchIntent,
    encode_launch_intent,
)
from joyread.app.launch.single_instance_broker import (
    InstanceRole,
    SingleInstanceBroker,
    SingleInstanceError,
)


_SECONDARY_SCRIPT = """
import base64
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from joyread.app.launch.intent import decode_launch_intent
from joyread.app.launch.single_instance_broker import SingleInstanceBroker

app = QCoreApplication([])
intent = decode_launch_intent(base64.b64decode(sys.argv[2]))
broker = SingleInstanceBroker(Path(sys.argv[1]))
try:
    print(broker.start(lambda: intent).value, flush=True)
finally:
    broker.dispose()
"""


def _launch_secondary(support_root: Path, intent: LaunchIntent) -> subprocess.Popen[str]:
    encoded = base64.b64encode(encode_launch_intent(intent)).decode("ascii")
    return subprocess.Popen(
        [sys.executable, "-c", _SECONDARY_SCRIPT, str(support_root), encoded],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _assert_secondary_finished(qtbot, process: subprocess.Popen[str]) -> None:  # noqa: ANN001
    qtbot.waitUntil(lambda: process.poll() is not None, timeout=5000)
    stdout, stderr = process.communicate(timeout=1)
    assert process.returncode == 0, stderr
    assert stdout.strip() == InstanceRole.SECONDARY.value


def _stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is not None and process.poll() is None:
        process.kill()
        process.wait(timeout=5)


def test_secondary_forwards_to_primary(qtbot, tmp_path: Path) -> None:
    primary = SingleInstanceBroker(tmp_path)
    secondary: subprocess.Popen[str] | None = None
    received: list[LaunchIntent] = []
    source = tmp_path / "book.cbz"

    try:
        assert primary.start(lambda: LaunchIntent.show_library()) == InstanceRole.PRIMARY
        primary.set_intent_handler(received.append)
        secondary = _launch_secondary(tmp_path, LaunchIntent.open_files((source,)))

        qtbot.waitUntil(lambda: len(received) == 1, timeout=5000)
        assert received == [LaunchIntent.open_files((source,))]
        _assert_secondary_finished(qtbot, secondary)
    finally:
        _stop_process(secondary)
        primary.dispose()


def test_primary_queues_intents_until_handler_is_ready(qtbot, tmp_path: Path) -> None:
    primary = SingleInstanceBroker(tmp_path)
    secondary: subprocess.Popen[str] | None = None
    intent = LaunchIntent.show_library()
    received: list[LaunchIntent] = []

    try:
        assert primary.start(lambda: intent) == InstanceRole.PRIMARY
        secondary = _launch_secondary(tmp_path, intent)
        qtbot.waitUntil(lambda: bool(primary._pending_intents), timeout=5000)

        primary.set_intent_handler(received.append)
        assert received == [intent]
        _assert_secondary_finished(qtbot, secondary)
    finally:
        _stop_process(secondary)
        primary.dispose()


def test_locked_profile_without_server_never_becomes_a_second_primary(tmp_path: Path) -> None:
    broker = SingleInstanceBroker(tmp_path, connect_timeout_ms=250)
    broker.lock_path.parent.mkdir(parents=True)
    foreign_lock = QLockFile(str(broker.lock_path))
    foreign_lock.setStaleLockTime(0)
    assert foreign_lock.tryLock(0)

    try:
        with pytest.raises(SingleInstanceError, match="could not receive"):
            broker.start(lambda: LaunchIntent.show_library())
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
        assert broker.start(lambda: LaunchIntent.show_library()) == InstanceRole.PRIMARY
    finally:
        broker.dispose()


def test_handler_failure_does_not_stop_later_launches(qtbot, tmp_path: Path, caplog) -> None:
    primary = SingleInstanceBroker(tmp_path)
    failing_secondary: subprocess.Popen[str] | None = None
    succeeding_secondary: subprocess.Popen[str] | None = None
    received: list[LaunchIntent] = []

    def fail_handler(_intent: LaunchIntent) -> None:
        raise RuntimeError("simulated window construction failure")

    try:
        assert primary.start(lambda: LaunchIntent.show_library()) == InstanceRole.PRIMARY
        primary.set_intent_handler(fail_handler)

        with caplog.at_level(logging.ERROR, logger="joyread.app.launch.single_instance_broker"):
            failing_secondary = _launch_secondary(tmp_path, LaunchIntent.show_library())
            qtbot.waitUntil(
                lambda: "Launch intent handler failed" in caplog.text,
                timeout=5000,
            )
            _assert_secondary_finished(qtbot, failing_secondary)

        primary.set_intent_handler(received.append)
        follow_up = LaunchIntent.open_files((tmp_path / "follow-up.cbz",))
        succeeding_secondary = _launch_secondary(tmp_path, follow_up)
        qtbot.waitUntil(lambda: received == [follow_up], timeout=5000)
        _assert_secondary_finished(qtbot, succeeding_secondary)
    finally:
        _stop_process(succeeding_secondary)
        _stop_process(failing_secondary)
        primary.dispose()


def test_secondary_rejects_oversized_outbound_intent(tmp_path: Path) -> None:
    primary = SingleInstanceBroker(tmp_path)
    secondary = SingleInstanceBroker(tmp_path)
    oversized = LaunchIntent(
        LaunchAction.OPEN_FILES,
        (Path(f"{'x' * MAX_LAUNCH_MESSAGE_BYTES}.cbz"),),
    )

    try:
        assert primary.start(lambda: LaunchIntent.show_library()) == InstanceRole.PRIMARY
        with pytest.raises(SingleInstanceError, match="cannot forward.*maximum IPC message size"):
            secondary.start(lambda: oversized)
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
        assert primary.start(lambda: LaunchIntent.show_library()) == InstanceRole.PRIMARY
        primary.set_intent_handler(received.append)
        assert secondary.start(lambda: intent) == InstanceRole.SECONDARY
        qtbot.waitUntil(lambda: received == [intent], timeout=1000)
    finally:
        secondary.dispose()
        primary.dispose()
