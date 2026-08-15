"""Policy for application-wide quiescence around a storage transition.

Moving, selecting, or resetting the library replaces the archive extraction
pool and the whole database stack. Anything still running against the retired
stack at that moment is a defect waiting to happen: a bulk conversion writing
into a pool that is about to be dropped, a Reader session whose document
outlives its services, a thumbnail job holding a lease nobody will release.

The ordering and the refusal rules live here, free of Qt, so they can be tested
without an event loop. The driver that owns the timer, the worker thread, and
the dialogs lives in the UI layer -- the same split as
:mod:`joyread.app.windows.activation`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class QuiesceStep(StrEnum):
    """The ordered phases of a transition.

    The order is not arbitrary. Reader writes are flushed while the task
    service still runs, because cancelling first would drop them. Producers are
    stopped before the drain so the drain has a chance to reach zero. The disk
    phase runs only once the drain has been proven.
    """

    CONFIRM = "confirm"
    CLOSE_EDITORS = "close_editors"
    FLUSH_READER_WRITES = "flush_reader_writes"
    CLOSE_READERS = "close_readers"
    STOP_BACKGROUND_WORK = "stop_background_work"
    DRAIN = "drain"
    MIGRATE = "migrate"
    REBUILD = "rebuild"


#: Canonical order. A driver must not reorder or skip a step.
QUIESCE_STEPS: tuple[QuiesceStep, ...] = tuple(QuiesceStep)


class QuiesceOutcome(StrEnum):
    READY = "ready"
    #: Work is still unwinding; poll again.
    WAITING = "waiting"
    #: The deadline passed with work still running. The transition is abandoned
    #: rather than forced, because migrating without proven quiescence is the
    #: defect this module exists to prevent.
    TIMED_OUT = "timed_out"


#: How long to wait for cancelled work to unwind before abandoning.
#:
#: Cancellation is cooperative but prompt: ``run_archive_file_command`` polls
#: ``is_cancelled`` on a 50 ms tick and then kills and reaps the child, so even
#: a multi-gigabyte solid extraction stops within a few ticks. The budget below
#: is set well above that so an ordinary slow unwind is never mistaken for a
#: wedge, while a genuinely stuck task still surfaces instead of hanging the
#: transition forever.
DRAIN_TIMEOUT_MS = 10_000

#: Reader progress and preference writes are small single-row updates already
#: queued to the database actor, so they either land quickly or are not going
#: to. This is deliberately shorter than the drain budget.
FLUSH_TIMEOUT_MS = 3_000


@dataclass(frozen=True, slots=True)
class QuiesceProgress:
    outcome: QuiesceOutcome
    pending_tasks: int
    elapsed_ms: int


def evaluate_drain(
    pending_tasks: int,
    elapsed_ms: int,
    *,
    timeout_ms: int = DRAIN_TIMEOUT_MS,
) -> QuiesceProgress:
    """Decide whether a drain may proceed, must keep waiting, or has failed.

    The elapsed check runs only when work is still outstanding, so a drain that
    completes on the same tick the deadline expires is a success rather than a
    timeout.
    """

    pending = max(0, int(pending_tasks))
    if pending == 0:
        return QuiesceProgress(QuiesceOutcome.READY, 0, elapsed_ms)
    if elapsed_ms >= timeout_ms:
        return QuiesceProgress(QuiesceOutcome.TIMED_OUT, pending, elapsed_ms)
    return QuiesceProgress(QuiesceOutcome.WAITING, pending, elapsed_ms)


@dataclass(frozen=True, slots=True)
class TransitionConsequences:
    """What confirming this transition will do to open UI state.

    Presented to the user before anything is closed. Discarding an unsaved
    cover crop is an accepted consequence of confirming, not a silent one.
    """

    reader_windows: int
    discards_cover_edit: bool

    @property
    def closes_anything(self) -> bool:
        return self.reader_windows > 0 or self.discards_cover_edit


def describe_consequences(
    reader_window_count: int,
    cover_editor_open: bool,
) -> TransitionConsequences:
    return TransitionConsequences(
        reader_windows=max(0, int(reader_window_count)),
        discards_cover_edit=bool(cover_editor_open),
    )


