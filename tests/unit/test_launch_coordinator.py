from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QMainWindow

from joyread.app.launch.coordinator import LaunchCoordinator
from joyread.app.launch.intent import LaunchIntent
from joyread.app.launch.ready_gate import ImmediateLaunchGate


class _ManualGate:
    def __init__(self) -> None:
        self._callback = None
        self.calls = 0

    def when_ready(self, callback) -> None:  # noqa: ANN001
        self.calls += 1
        self._callback = callback

    def release(self) -> None:
        callback, self._callback = self._callback, None
        if callback is not None:
            callback()


class _Sink:
    def __init__(self, qtbot, *, reject: set[str] | None = None) -> None:  # noqa: ANN001
        self._qtbot = qtbot
        self._reject = reject or set()
        self.calls: list[str] = []
        self.opened: list[Path] = []

    def show_library(self) -> QMainWindow:
        self.calls.append("show_library")
        window = QMainWindow()
        self._qtbot.addWidget(window)
        return window

    def open_files(self, paths) -> tuple[QMainWindow, ...]:  # noqa: ANN001
        self.calls.append("open_files")
        windows: list[QMainWindow] = []
        for path in paths:
            if Path(path).name in self._reject:
                continue
            self.opened.append(Path(path))
            window = QMainWindow()
            self._qtbot.addWidget(window)
            windows.append(window)
        return tuple(windows)


def _cbz(tmp_path: Path, name: str) -> Path:
    source = tmp_path / name
    source.write_bytes(b"")
    return source


def test_argv_and_late_documents_merge_into_one_decision(qtbot, tmp_path: Path) -> None:
    """A Finder document arriving after argv must not produce a second window."""

    from_argv = _cbz(tmp_path, "argv.cbz")
    from_finder = _cbz(tmp_path, "finder.cbz")
    sink = _Sink(qtbot)
    gate = _ManualGate()
    coordinator = LaunchCoordinator(windows=sink, gate=gate)

    coordinator.start(LaunchIntent.open_files((from_argv,)))
    coordinator.submit(LaunchIntent.open_files((from_finder,)))
    assert sink.calls == []

    gate.release()

    assert sink.calls == ["open_files"]
    assert sorted(sink.opened) == sorted([from_argv, from_finder])
    coordinator.deleteLater()


def test_the_same_document_from_argv_and_the_os_opens_once(qtbot, tmp_path: Path) -> None:
    source = _cbz(tmp_path, "same.cbz")
    sink = _Sink(qtbot)
    gate = _ManualGate()
    coordinator = LaunchCoordinator(windows=sink, gate=gate)

    coordinator.start(LaunchIntent.open_files((source,)))
    coordinator.submit(LaunchIntent.open_files((source,)))
    gate.release()

    assert sink.opened == [source]
    coordinator.deleteLater()


def test_a_buffered_show_library_never_suppresses_a_document(qtbot, tmp_path: Path) -> None:
    """A forwarded plain launch must lose to a real document in the same window."""

    source = _cbz(tmp_path, "document.cbz")
    sink = _Sink(qtbot)
    gate = _ManualGate()
    coordinator = LaunchCoordinator(windows=sink, gate=gate)

    coordinator.start()
    coordinator.submit(LaunchIntent.show_library())
    coordinator.submit(LaunchIntent.open_files((source,)))
    gate.release()

    assert sink.calls == ["open_files"]
    assert sink.opened == [source]
    coordinator.deleteLater()


def test_library_is_shown_when_every_document_is_rejected(qtbot, tmp_path: Path) -> None:
    """Startup must still produce a usable window if the document cannot open."""

    source = _cbz(tmp_path, "broken.cbz")
    sink = _Sink(qtbot, reject={"broken.cbz"})
    gate = _ManualGate()
    coordinator = LaunchCoordinator(windows=sink, gate=gate)

    coordinator.start(LaunchIntent.open_files((source,)))
    gate.release()

    assert sink.calls == ["open_files", "show_library"]
    assert coordinator.initial_window is not None
    coordinator.deleteLater()


def test_requests_after_settling_dispatch_immediately(qtbot, tmp_path: Path) -> None:
    source = _cbz(tmp_path, "later.cbz")
    sink = _Sink(qtbot)
    gate = _ManualGate()
    coordinator = LaunchCoordinator(windows=sink, gate=gate)

    coordinator.start()
    gate.release()
    assert sink.calls == ["show_library"]

    windows = coordinator.submit(LaunchIntent.open_files((source,)))

    assert sink.calls == ["show_library", "open_files"]
    assert len(windows) == 1
    coordinator.deleteLater()


def test_an_immediate_gate_settles_inside_start(qtbot) -> None:
    sink = _Sink(qtbot)
    coordinator = LaunchCoordinator(windows=sink, gate=ImmediateLaunchGate())

    coordinator.start()

    assert coordinator.ready
    assert sink.calls == ["show_library"]
    assert coordinator.initial_window is not None
    coordinator.deleteLater()


def test_start_may_only_run_once(qtbot) -> None:
    coordinator = LaunchCoordinator(windows=_Sink(qtbot), gate=_ManualGate())
    coordinator.start()

    try:
        coordinator.start()
    except RuntimeError:
        pass
    else:  # pragma: no cover - guard against a silent regression
        raise AssertionError("LaunchCoordinator.start() must reject a second call.")
    coordinator.deleteLater()


def test_reentrant_submit_gets_its_own_operation(qtbot, tmp_path: Path) -> None:
    """A document arriving while the first window is still under construction
    (window construction pumps events) must not alias its operation onto the
    settle operation that is still ambient -- it needs its own identity,
    parented to it.
    """

    from joyread.core.operation_context import current_operation

    captured: dict[str, object] = {}
    late_doc = _cbz(tmp_path, "late.cbz")

    class _ReentrantSink:
        def show_library(self) -> QMainWindow:
            captured["outer"] = current_operation()
            coordinator.submit(LaunchIntent.open_files((late_doc,)))
            window = QMainWindow()
            qtbot.addWidget(window)
            return window

        def open_files(self, paths) -> tuple[QMainWindow, ...]:  # noqa: ANN001
            captured["inner"] = current_operation()
            window = QMainWindow()
            qtbot.addWidget(window)
            return (window,)

    coordinator = LaunchCoordinator(windows=_ReentrantSink(), gate=ImmediateLaunchGate())
    coordinator.start()

    outer = captured["outer"]
    inner = captured["inner"]
    assert outer is not None and inner is not None
    assert inner.operation_id != outer.operation_id
    assert inner.parent_operation_id == outer.operation_id
    coordinator.deleteLater()
