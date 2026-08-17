"""Pool usage has to survive the hop to the GUI thread at real pool sizes."""

from __future__ import annotations

from joyread.app.archive_pool_usage_bridge import ArchivePoolUsageBridge


GIB = 1024**3


class _FakePool:
    def __init__(self) -> None:
        self.listener = None

    def set_usage_listener(self, listener) -> None:  # noqa: ANN001
        self.listener = listener


def test_usage_above_two_gigabytes_survives_the_signal(qtbot) -> None:  # noqa: ANN001
    """Qt maps a Python ``int`` signal parameter to a C++ 32-bit ``int``, which
    overflows at 2.147 GB and raises out of the listener -- on the caching
    worker thread, under the pool lock. The budget defaults to 5 GB and reaches
    50 GB, so a default install hits this simply by filling its pool.
    """

    bridge = ArchivePoolUsageBridge()
    seen: list[int] = []
    bridge.usage_changed.connect(seen.append)

    reported = [3 * GIB, 5 * GIB, 50 * GIB, 4_608_413_625, 0]
    for usage in reported:
        bridge._on_usage_changed(usage)  # noqa: SLF001

    assert seen == reported


def test_the_bridge_detaches_from_a_retired_pool(qtbot) -> None:  # noqa: ANN001
    """A storage transition replaces the pool; the old one must stop
    reporting usage for a library that is no longer open."""

    bridge = ArchivePoolUsageBridge()
    first = _FakePool()
    second = _FakePool()

    bridge.attach(first)
    bridge.attach(second)

    assert first.listener is None
    assert second.listener is not None

    bridge.detach()

    assert second.listener is None
