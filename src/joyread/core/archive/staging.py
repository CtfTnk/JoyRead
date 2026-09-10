"""Process-scoped staging directories for archive extraction, and their sweep.

Every temporary tree the archive core creates lives under the system temp
directory behind a ``joyread-`` prefix. Most are ``TemporaryDirectory`` objects
that remove themselves; the spill directory a reader session holds for its
nested archives lives as long as the document is open, so it is created with
``mkdtemp`` and removed when the session closes.

None of that survives a crash, a kill, or a power loss, and nothing collected
what those left behind -- one machine had accumulated 886 orphaned spill
directories over five days, several still holding archive bytes.
:func:`sweep_orphaned_staging` is that collector. It removes only trees that
cannot belong to a running JoyRead: a spill directory stamped with a process id
that is no longer alive, or any other staging tree untouched for a day.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from shutil import rmtree
import tempfile
from threading import Lock, Thread
import time


logger = logging.getLogger(__name__)

_SPILL_PREFIX = "joyread-nested-"
#: Every staging tree the archive core creates, named explicitly rather than
#: matched on a bare ``joyread-`` prefix: the app puts other things in the
#: system temp directory (the test runtime root among them), and a sweep is
#: not allowed to guess at which of those it owns.
STAGING_PREFIXES = (
    _SPILL_PREFIX,
    "joyread-7z-",
    "joyread-rar-",
    "joyread-convert-",
    "joyread-canonical-",
)
#: Long enough that no extraction, conversion or import could still be using a
#: tree this old, short enough that debris does not sit around for a week.
_STALE_AFTER_SECONDS = 24 * 60 * 60

_sweep_lock = Lock()
_sweep_started = False


def create_spill_directory() -> Path:
    """A directory for nested archives, stamped with the process that owns it.

    The process id is what lets the sweep tell a tree abandoned by a crashed
    launch from one a running JoyRead is still reading out of.
    """

    return Path(tempfile.mkdtemp(prefix=f"{_SPILL_PREFIX}{os.getpid()}-"))


def sweep_orphaned_staging(
    root: Path | None = None,
    *,
    now: float | None = None,
    stale_after_seconds: float = _STALE_AFTER_SECONDS,
) -> int:
    """Remove staging trees no live JoyRead can still own. Returns the count."""

    directory = Path(tempfile.gettempdir()) if root is None else Path(root)
    own_pid = os.getpid()
    moment = time.time() if now is None else now
    try:
        entries = tuple(directory.iterdir())
    except OSError as exc:
        logger.debug("Archive staging sweep could not list %s: %s", directory, exc)
        return 0
    removed = 0
    for entry in entries:
        if not entry.name.startswith(STAGING_PREFIXES):
            continue
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
        except OSError:
            continue
        if not _is_abandoned(entry, own_pid, moment, stale_after_seconds):
            continue
        # Best-effort: a tree we cannot finish removing is left for the next
        # launch rather than turning startup housekeeping into an error.
        rmtree(entry, ignore_errors=True)
        if not entry.exists():
            removed += 1
    if removed:
        logger.info(
            "Archive staging sweep reclaimed abandoned extraction directories",
            extra={
                "event": "archive.staging.swept",
                "category": "archive",
                "status": "finished",
                "count": removed,
            },
        )
    return removed


def sweep_orphaned_staging_in_background() -> None:
    """Run the sweep once per process, off whatever thread starts the app."""

    global _sweep_started
    with _sweep_lock:
        if _sweep_started:
            return
        _sweep_started = True
    Thread(
        target=_sweep_quietly,
        name="joyread-staging-sweep",
        daemon=True,
    ).start()


def _sweep_quietly() -> None:
    try:
        sweep_orphaned_staging()
    except Exception:
        # Housekeeping must never be the reason a launch reports a problem.
        logger.warning("Archive staging sweep failed", exc_info=True)


def _is_abandoned(
    entry: Path,
    own_pid: int,
    now: float,
    stale_after_seconds: float,
) -> bool:
    pid = _owner_pid(entry.name)
    if pid == own_pid:
        return False
    if pid is not None and os.name != "nt":
        # On POSIX the owner stamp is authoritative while that PID is live.
        # Age cannot prove abandonment: a Reader may legitimately stay open
        # for days, and another JoyRead/dev process can share this temp root.
        return not _process_is_running(pid)
    # Unstamped staging predates owner PIDs. Windows also uses age alone because
    # it has no safe equivalent of the POSIX liveness probe used above.
    try:
        return now - entry.stat().st_mtime >= stale_after_seconds
    except OSError:
        return False


def _owner_pid(name: str) -> int | None:
    if not name.startswith(_SPILL_PREFIX):
        return None
    stamp = name[len(_SPILL_PREFIX):].partition("-")[0]
    return int(stamp) if stamp.isdigit() else None


def _process_is_running(pid: int) -> bool:
    """Whether a process id is live, answering "yes" when it cannot be known."""

    if pid <= 0:
        return False
    if os.name == "nt":
        # ``os.kill`` on Windows *terminates* the target for any signal other
        # than the two console events, so there is no probe to make here.
        # Windows falls back to the age rule, which is the safe answer.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # Alive but owned by another user, or an errno we cannot interpret.
        return True
    return True
