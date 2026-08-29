"""Stage timings for the whole startup path, including what precedes logging.

``app/main.py`` imports this module as its first executable statement, so the
module's own import is the earliest instant JoyRead code can observe. Everything
before that -- the PyInstaller bootloader, interpreter initialization, and site
setup -- is invisible from inside the process. It is also, in a packaged build,
a large share of the wall clock. :func:`origin_epoch` exists so an external
benchmark harness that knows when it spawned the process can subtract and
measure that hidden window without the app shipping ``psutil``.

Two properties are load-bearing:

- **Stdlib only.** No Qt, no ``joyread`` imports, no work at import time beyond
  reading two clocks. Anything this module pulls in becomes cost paid before
  the origin it claims to measure.
- **Marks survive logging setup.** ``configure_early_logging()`` runs well after
  the first milestones, so marks are buffered and flushed once a logger exists.
  :func:`flush_to_log` emits only what it has not emitted before, which makes it
  safe to call at every stage boundary.
"""

from __future__ import annotations

import logging
import time
from threading import Lock
from typing import NamedTuple


__all__ = [
    "Milestone",
    "elapsed_ms",
    "flush_to_log",
    "mark",
    "milestones",
    "origin_epoch",
    "reset",
]


# Read once, at import. `perf_counter` is monotonic and is what every elapsed
# figure is derived from; `time.time` is wall-clock and is only ever reported,
# never differenced internally, because it can step backwards.
_ORIGIN_PERF = time.perf_counter()
_ORIGIN_EPOCH = time.time()

_LOCK = Lock()
_MILESTONES: list["Milestone"] = []
_FLUSHED = 0


class Milestone(NamedTuple):
    """One named instant on the startup path."""

    #: Stable identifier, e.g. ``"window_shown"``. Never a path or user value.
    name: str
    #: Milliseconds from the trace origin to this milestone.
    elapsed_ms: float
    #: Milliseconds from the previous milestone to this one.
    stage_ms: float


def mark(name: str) -> Milestone | None:
    """Record ``name`` at the current instant, or return ``None`` if repeated.

    Startup is a one-time sequence, so the first observation of a name wins.
    That matters because ``create_application()`` is re-entrant: tests and
    embedded callers invoke it repeatedly against one shared ``QApplication``,
    and a later re-entry must not overwrite the real startup timings.
    """

    now = time.perf_counter()
    with _LOCK:
        if any(existing.name == name for existing in _MILESTONES):
            return None
        elapsed = (now - _ORIGIN_PERF) * 1000.0
        previous = _MILESTONES[-1].elapsed_ms if _MILESTONES else 0.0
        milestone = Milestone(name, elapsed, elapsed - previous)
        _MILESTONES.append(milestone)
        return milestone


def milestones() -> tuple[Milestone, ...]:
    """Return every milestone recorded so far, in order."""

    with _LOCK:
        return tuple(_MILESTONES)


def elapsed_ms() -> float:
    """Milliseconds from the trace origin to now."""

    return (time.perf_counter() - _ORIGIN_PERF) * 1000.0


def origin_epoch() -> float:
    """Wall-clock time at the trace origin, as a Unix timestamp.

    Reported so a harness can measure process spawn to first JoyRead
    instruction. Never differenced against another epoch reading inside the
    process: use :func:`elapsed_ms` for durations.
    """

    return _ORIGIN_EPOCH


def flush_to_log(logger: logging.Logger, *, level: int = logging.INFO) -> int:
    """Emit milestones not yet logged, and return how many were emitted.

    Safe to call repeatedly and from any thread. Milestone names are module
    constants, so nothing here needs redaction.
    """

    global _FLUSHED
    with _LOCK:
        pending = _MILESTONES[_FLUSHED:]
        first_flush = _FLUSHED == 0
        _FLUSHED = len(_MILESTONES)
    for milestone in pending:
        extra: dict[str, object] = {
            "event": "startup.milestone",
            "category": "process",
            "milestone": milestone.name,
            "elapsed_ms": round(milestone.elapsed_ms, 1),
            "stage_ms": round(milestone.stage_ms, 1),
        }
        carries_origin = first_flush and milestone is pending[0]
        if carries_origin:
            # Carried once, on the earliest line, so a harness can pair the
            # in-process origin with its own spawn timestamp. It goes in the
            # rendered message as well as the structured field because a
            # secondary process exits before file logging exists, and the early
            # stderr handler formats text only -- that is the sole channel the
            # `Open With` measurement has.
            extra["origin_epoch"] = _ORIGIN_EPOCH
        logger.log(
            level,
            "startup %s at %.1f ms (+%.1f ms)%s",
            milestone.name,
            milestone.elapsed_ms,
            milestone.stage_ms,
            f" origin_epoch={_ORIGIN_EPOCH:.6f}" if carries_origin else "",
            extra=extra,
        )
    return len(pending)


def reset() -> None:
    """Drop every recorded milestone. For tests only."""

    global _FLUSHED
    with _LOCK:
        _MILESTONES.clear()
        _FLUSHED = 0


mark("origin")
