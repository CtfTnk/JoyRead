"""Quiescing the application around a storage transition."""

from __future__ import annotations

from joyread.app.storage_transition import (
    QUIESCE_STEPS,
    QuiesceOutcome,
    QuiesceStep,
    describe_consequences,
    evaluate_drain,
)
from joyread.app.storage_transition_driver import StorageTransitionController
from joyread.app.tasking import TaskHandle, TaskStatus


def test_reader_writes_are_flushed_before_anything_is_cancelled() -> None:
    """The order is the whole point.

    Cancelling first would discard the progress write; closing Readers first
    would too, because `ReaderViewModel.cancel()` cancels the progress handle.
    Draining before producers are stopped would never reach zero.
    """

    order = list(QUIESCE_STEPS)
    assert order.index(QuiesceStep.FLUSH_READER_WRITES) < order.index(QuiesceStep.CLOSE_READERS)
    assert order.index(QuiesceStep.CLOSE_READERS) < order.index(QuiesceStep.STOP_BACKGROUND_WORK)
    assert order.index(QuiesceStep.STOP_BACKGROUND_WORK) < order.index(QuiesceStep.DRAIN)
    assert order.index(QuiesceStep.DRAIN) < order.index(QuiesceStep.MIGRATE)
    assert order.index(QuiesceStep.CONFIRM) == 0


def test_a_drain_with_no_work_left_is_ready() -> None:
    assert evaluate_drain(0, elapsed_ms=0).outcome is QuiesceOutcome.READY


def test_a_drain_still_unwinding_keeps_waiting() -> None:
    progress = evaluate_drain(3, elapsed_ms=100, timeout_ms=1000)
    assert progress.outcome is QuiesceOutcome.WAITING
    assert progress.pending_tasks == 3


def test_a_drain_that_finishes_on_the_deadline_is_not_a_timeout() -> None:
    """Elapsed time only matters while work is outstanding."""

    assert evaluate_drain(0, elapsed_ms=99_999, timeout_ms=1000).outcome is QuiesceOutcome.READY


def test_a_drain_that_will_not_stop_times_out_rather_than_forcing() -> None:
    progress = evaluate_drain(1, elapsed_ms=1000, timeout_ms=1000)
    assert progress.outcome is QuiesceOutcome.TIMED_OUT
    assert progress.pending_tasks == 1


def test_consequences_describe_what_confirming_will_close() -> None:
    quiet = describe_consequences(0, cover_editor_open=False)
    assert quiet.closes_anything is False

    loud = describe_consequences(2, cover_editor_open=True)
    assert (loud.reader_windows, loud.discards_cover_edit) == (2, True)
    assert loud.closes_anything is True


class _Context:
    def __init__(self, pending: list[int]) -> None:
        self.calls: list[str] = []
        self._pending = pending

    def quiesce_for_storage_transition(self) -> int:
        self.calls.append("quiesce")
        return self._pending[0] if self._pending else 0

    def storage_transition_pending_tasks(self) -> int:
        # Each poll consumes one reading, so a test can script an unwind.
        return self._pending.pop(0) if len(self._pending) > 1 else (self._pending[0] if self._pending else 0)

    def commit_storage_transition(self) -> None:
        self.calls.append("commit")

    def abandon_storage_transition(self) -> None:
        self.calls.append("abandon")

    def resume_after_storage_transition(self) -> None:
        self.calls.append("resume")


class _ReaderViewModel:
    def __init__(self, handles: tuple[TaskHandle, ...]) -> None:
        self._handles = handles
        self.flushed = 0

    def flush_pending_writes(self) -> tuple[TaskHandle, ...]:
        self.flushed += 1
        return self._handles


class _ReaderWindow:
    def __init__(self, handles: tuple[TaskHandle, ...] = ()) -> None:
        self.viewmodel = _ReaderViewModel(handles)


class _WindowManager:
    def __init__(self, windows: tuple[_ReaderWindow, ...] = ()) -> None:
        self.reader_windows = windows
        self.closed = 0

    def close_all_readers(self) -> int:
        self.closed += 1
        return len(self.reader_windows)


def _running_handle() -> TaskHandle:
    handle: TaskHandle = TaskHandle(task_id="pending-write")
    handle.status = TaskStatus.RUNNING
    return handle


def test_a_quiet_application_migrates_without_waiting(qtbot) -> None:  # noqa: ANN001, ARG001
    context = _Context([0])
    manager = _WindowManager()
    controller = StorageTransitionController(context, manager)
    done: list[str] = []
    controller.finished.connect(done.append)

    assert controller.start(lambda: "migrated") is True
    qtbot.waitUntil(lambda: bool(done), timeout=2000)

    assert done == ["migrated"]
    assert context.calls == ["quiesce", "commit"]
    assert manager.closed == 1


def test_readers_are_flushed_and_only_closed_once_their_writes_land(qtbot) -> None:  # noqa: ANN001, ARG001
    """A Reader closing early would cancel the write being waited on."""

    handle = _running_handle()
    window = _ReaderWindow((handle,))
    manager = _WindowManager((window,))
    context = _Context([0])
    controller = StorageTransitionController(context, manager, poll_interval_ms=5)
    done: list[str] = []
    controller.finished.connect(done.append)

    controller.start(lambda: "migrated")

    assert window.viewmodel.flushed == 1
    assert manager.closed == 0, "the reader must stay open while its write is in flight"
    assert context.calls == [], "nothing may be cancelled while a write is in flight"

    handle.status = TaskStatus.COMPLETED
    qtbot.waitUntil(lambda: bool(done), timeout=2000)

    assert manager.closed == 1
    assert context.calls == ["quiesce", "commit"]


def test_work_that_will_not_stop_abandons_instead_of_migrating(qtbot) -> None:  # noqa: ANN001, ARG001
    """Migrating without proven quiescence is the defect being prevented."""

    context = _Context([2])
    manager = _WindowManager()
    controller = StorageTransitionController(
        context, manager, poll_interval_ms=5, drain_timeout_ms=30
    )
    migrated: list[object] = []
    abandoned: list[int] = []
    controller.finished.connect(migrated.append)
    controller.abandoned.connect(abandoned.append)

    controller.start(lambda: migrated.append("migrated"))
    qtbot.waitUntil(lambda: bool(abandoned), timeout=2000)

    assert abandoned == [2]
    assert migrated == [], "storage must not be touched"
    assert "commit" not in context.calls, "nothing terminal may run on the abandon path"
    assert context.calls == ["quiesce", "abandon"]


def test_a_failing_disk_phase_reports_instead_of_finishing(qtbot) -> None:  # noqa: ANN001, ARG001
    context = _Context([0])
    controller = StorageTransitionController(context, _WindowManager())
    failures: list[Exception] = []
    controller.failed.connect(failures.append)

    def boom() -> object:
        raise OSError("the destination went away")

    controller.start(boom)
    qtbot.waitUntil(lambda: bool(failures), timeout=2000)

    assert isinstance(failures[0], OSError)
    assert context.calls == ["quiesce", "commit"]


def test_a_second_transition_is_refused_while_one_is_running(qtbot) -> None:  # noqa: ANN001, ARG001
    handle = _running_handle()
    manager = _WindowManager((_ReaderWindow((handle,)),))
    controller = StorageTransitionController(_Context([0]), manager, poll_interval_ms=5)

    assert controller.start(lambda: "first") is True
    assert controller.start(lambda: "second") is False, "two migrations must not overlap"

    handle.status = TaskStatus.COMPLETED
    qtbot.waitUntil(lambda: not controller.busy, timeout=2000)


def test_the_flush_waits_out_a_chained_write_it_could_not_see_at_the_start(qtbot) -> None:  # noqa: ANN001, ARG001
    """Reader settings saves are serialized: a queued one is only submitted
    once its predecessor finishes. Snapshotting handles once would stop waiting
    at the first write and close the Reader before the newest one was made."""

    class _Chained:
        """Reports one outstanding write, then a second, then nothing."""

        def __init__(self) -> None:
            self.asked = 0

        def flush_pending_writes(self) -> tuple[TaskHandle, ...]:
            self.asked += 1
            if self.asked >= 3:
                return ()
            return (_running_handle(),)

    window = _ReaderWindow()
    chained = _Chained()
    window.viewmodel = chained  # type: ignore[assignment]
    manager = _WindowManager((window,))
    context = _Context([0])
    controller = StorageTransitionController(context, manager, poll_interval_ms=5)
    done: list[str] = []
    controller.finished.connect(done.append)

    controller.start(lambda: "migrated")
    qtbot.waitUntil(lambda: bool(done), timeout=2000)

    assert chained.asked >= 3, "the flush must re-ask, not trust its first answer"
    assert manager.closed == 1


def test_a_reader_without_durable_state_contributes_no_flush(qtbot) -> None:  # noqa: ANN001, ARG001
    """EPUB Readers have no per-page write to push out."""

    class _Bare:
        viewmodel = object()

    context = _Context([0])
    controller = StorageTransitionController(context, _WindowManager((_Bare(),)))
    done: list[str] = []
    controller.finished.connect(done.append)

    controller.start(lambda: "migrated")
    qtbot.waitUntil(lambda: bool(done), timeout=2000)

    assert done == ["migrated"]
