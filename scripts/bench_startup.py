#!/usr/bin/env python3
"""Measure packaged (or source) JoyRead startup, stage by stage, over N runs.

Three scenarios, matching the three ways a user actually starts JoyRead:

``library``
    Plain launch with no document. Reports ``origin`` through ``first_paint``.

``file``
    Cold file activation with no primary running: the process opens a Reader
    directly and never builds a Library.

``openwith``
    A primary is already running and a second process forwards a document to it
    and exits. This is the scenario the launch-path work targets, and the one
    whose milestones arrive on stderr rather than in the log file, because a
    secondary exits before file logging is configured.

The process cannot see its own PyInstaller-bootloader and interpreter-init time,
so this harness measures it from outside: it records the instant before spawning
and subtracts it from ``origin_epoch``, which the app reports on its first
milestone line. That difference is printed as ``spawn->origin``.

Examples::

    python scripts/bench_startup.py --runs 7
    python scripts/bench_startup.py --exe dist/JoyRead/JoyRead.exe --runs 7
    python scripts/bench_startup.py --scenario openwith --document book.cbz
    python scripts/bench_startup.py --runs 3 --cold   # tag as cold-cache
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_RELATIVE = Path(".joyread_support") / "Logs" / "joyread.log"

# `startup <name> at <elapsed> ms (+<stage> ms)` -- emitted by
# joyread.app.startup_trace.flush_to_log through the standard text formatter.
_MILESTONE_RE = re.compile(
    r"startup (?P<name>[a-z_]+) at (?P<elapsed>[0-9.]+) ms \(\+(?P<stage>[0-9.]+) ms\)"
)
_ORIGIN_EPOCH_RE = re.compile(r"origin_epoch[\"']?[:=]\s*(?P<epoch>[0-9.]+)")

# A launch that was asked to open a document but showed the Library instead is a
# *correctness* failure that still produces a perfectly healthy-looking set of
# milestones. Timing alone cannot tell the two apart, so every document-bearing
# scenario asserts on the launch decision the app logged.
_READER_OPENED_RE = re.compile(r"Reader window created")
_LIBRARY_FALLBACK_RE = re.compile(
    r"No document could be opened at launch|Launch settled with no document"
)
_SECONDARY_FORWARDED_RE = re.compile(r"Secondary process forwarded its launch intent")
_INTENT_DISPATCHED_RE = re.compile(r"Dispatching launch request")
_INTENT_DELIVERED_RE = re.compile(r"Launch intent delivered")
_READER_ACTIVATED_RE = re.compile(r"Reader window created|Existing Reader window focused")

MILESTONE_ORDER = (
    "origin",
    "bootstrap_imported",
    "qt_app_created",
    "role_resolved",
    "context_ready",
    "resources_ready",
    "window_constructed",
    "window_shown",
    "first_paint",
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--exe",
        type=Path,
        help="Packaged executable. Defaults to running the source tree with this interpreter.",
    )
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument(
        "--scenario",
        choices=("library", "file", "openwith"),
        default="library",
    )
    parser.add_argument("--document", type=Path, help="Required by the file/openwith scenarios.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Seconds to wait for a run to reach first_paint (or for a secondary to exit).",
    )
    parser.add_argument(
        "--cold",
        action="store_true",
        help="Tag the report as cold-cache. Does not itself clear any cache.",
    )
    parser.add_argument(
        "--keep-runtime",
        action="store_true",
        help="Keep the isolated runtime directory instead of deleting it.",
    )
    parser.add_argument("--json", type=Path, help="Write the raw per-run results here.")
    return parser.parse_args(argv)


def _launch_command(exe: Path | None, arguments: list[str]) -> list[str]:
    if exe is not None:
        return [str(exe), *arguments]
    return [sys.executable, "-m", "joyread.app.main", *arguments]


def _environment(runtime_dir: Path, exe: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    env["JOYREAD_RUNTIME_DIR"] = str(runtime_dir)
    env["JOYREAD_LOG_LEVEL"] = "INFO"
    if exe is None:
        source = str(REPO_ROOT / "src")
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{source}{os.pathsep}{existing}" if existing else source
    return env


def _parse_milestones(text: str) -> dict[str, float]:
    found: dict[str, float] = {}
    for match in _MILESTONE_RE.finditer(text):
        # First occurrence wins, mirroring startup_trace's own rule.
        found.setdefault(match.group("name"), float(match.group("elapsed")))
    return found


def _parse_origin_epoch(text: str) -> float | None:
    match = _ORIGIN_EPOCH_RE.search(text)
    return float(match.group("epoch")) if match is not None else None


def _read_log(runtime_dir: Path) -> str:
    log_file = runtime_dir / LOG_RELATIVE
    if not log_file.is_file():
        return ""
    return log_file.read_text(encoding="utf-8", errors="replace")


def _log_size(runtime_dir: Path) -> int:
    log_file = runtime_dir / LOG_RELATIVE
    return log_file.stat().st_size if log_file.is_file() else 0


def _read_log_since(runtime_dir: Path, offset: int) -> str:
    """Read records appended after ``offset`` in the primary log.

    Open-With runs share one primary process and therefore one cumulative log.
    Looking at the whole file lets a successful earlier run hide a failed later
    one. Taking the byte offset before each secondary launch makes every
    correctness assertion belong to that launch only.
    """

    log_file = runtime_dir / LOG_RELATIVE
    if not log_file.is_file():
        return ""
    with log_file.open("rb") as stream:
        stream.seek(offset)
        return stream.read().decode("utf-8", errors="replace")


def _primary_delivery_problems(text: str) -> list[str]:
    problems: list[str] = []
    if not _INTENT_DISPATCHED_RE.search(text):
        problems.append("primary never dispatched the forwarded intent")
    elif not _READER_ACTIVATED_RE.search(text):
        problems.append("primary dispatched the intent but opened or focused no Reader window")
    if not _INTENT_DELIVERED_RE.search(text):
        problems.append("primary did not finish delivering the forwarded intent")
    return problems


def _wait_for_primary_delivery(
    runtime_dir: Path,
    offset: int,
    timeout: float,
) -> tuple[str, list[str]]:
    """Wait for the primary to finish the launch requested by one secondary."""

    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = _read_log_since(runtime_dir, offset)
        if _INTENT_DELIVERED_RE.search(text):
            break
        time.sleep(0.02)
    return text, _primary_delivery_problems(text)


def _run_primary(
    args: argparse.Namespace,
    runtime_dir: Path,
    arguments: list[str],
) -> dict[str, object]:
    """Start a primary, wait for first_paint in its log, then stop it."""

    env = _environment(runtime_dir, args.exe)
    # Never a pipe. JoyRead mirrors every record to stderr through its early
    # handler, and a pipe nobody drains fills its ~64 KB buffer and blocks the
    # child mid-write -- the process then never reaches first_paint and the run
    # times out looking like a startup regression.
    console_path = runtime_dir / "console.txt"
    spawned_at = time.time()
    with console_path.open("w", encoding="utf-8", errors="replace") as console:
        process = subprocess.Popen(
            _launch_command(args.exe, arguments),
            env=env,
            stdout=console,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + args.timeout
        milestones: dict[str, float] = {}
        origin_epoch: float | None = None
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                text = _read_log(runtime_dir)
                milestones = _parse_milestones(text)
                origin_epoch = _parse_origin_epoch(text)
                if "first_paint" in milestones:
                    break
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    captured = console_path.read_text(encoding="utf-8", errors="replace")

    text = _read_log(runtime_dir) or captured
    milestones = milestones or _parse_milestones(text)
    origin_epoch = origin_epoch if origin_epoch is not None else _parse_origin_epoch(text)
    problems: list[str] = []
    if arguments:
        if _LIBRARY_FALLBACK_RE.search(text):
            problems.append("asked to open a document but fell back to the Library")
        elif not _READER_OPENED_RE.search(text):
            problems.append("asked to open a document but no Reader window was created")
    return {
        "milestones": milestones,
        "spawn_to_origin_ms": (origin_epoch - spawned_at) * 1000.0 if origin_epoch else None,
        "problems": problems,
        "output": captured,
    }


def _run_secondary(args: argparse.Namespace, runtime_dir: Path, document: Path) -> dict[str, object]:
    """Forward one document to an already-running primary and time the exit."""

    env = _environment(runtime_dir, args.exe)
    primary_log_offset = _log_size(runtime_dir)
    spawned_at = time.time()
    started = time.perf_counter()
    process = subprocess.run(
        _launch_command(args.exe, [str(document)]),
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=args.timeout,
    )
    wall_ms = (time.perf_counter() - started) * 1000.0
    text = f"{process.stdout or ''}\n{process.stderr or ''}"
    milestones = _parse_milestones(text)
    origin_epoch = _parse_origin_epoch(text)
    milestones["process_exit"] = wall_ms

    problems: list[str] = []
    if process.returncode != 0:
        problems.append(f"secondary exited {process.returncode}")
    if not _SECONDARY_FORWARDED_RE.search(text):
        # Without this, a secondary that quietly became a second *primary*
        # would still be timed, and would look fast for the wrong reason.
        problems.append("did not take the SECONDARY role")
    # Secondary exit and primary dispatch are asynchronous. The first forwarded
    # request may still be constructing its Reader for ~180 ms after the sender
    # exits, so wait for this request's terminal event. Inspect only bytes
    # appended after this launch; otherwise an earlier successful run can make
    # a later failure look healthy.
    primary_delivery, delivery_problems = _wait_for_primary_delivery(
        runtime_dir,
        primary_log_offset,
        args.timeout,
    )
    problems.extend(delivery_problems)
    return {
        "milestones": milestones,
        "spawn_to_origin_ms": (origin_epoch - spawned_at) * 1000.0 if origin_epoch else None,
        "returncode": process.returncode,
        "problems": problems,
        "output": f"{text}\n--- primary delivery ---\n{primary_delivery}",
    }


def _summarize(runs: list[dict[str, object]]) -> list[tuple[str, int, float, float, float]]:
    names: list[str] = []
    for run in runs:
        for name in run["milestones"]:  # type: ignore[index]
            if name not in names:
                names.append(name)
    names.sort(key=lambda name: (MILESTONE_ORDER.index(name) if name in MILESTONE_ORDER else 99, name))

    rows: list[tuple[str, int, float, float, float]] = []
    for name in names:
        values = [
            float(run["milestones"][name])  # type: ignore[index]
            for run in runs
            if name in run["milestones"]  # type: ignore[operator]
        ]
        if values:
            rows.append((name, len(values), statistics.median(values), min(values), max(values)))
    return rows


def _print_report(args: argparse.Namespace, runs: list[dict[str, object]]) -> None:
    label = "cold" if args.cold else "warm"
    target = str(args.exe) if args.exe else "source tree"
    print()
    print(f"JoyRead startup benchmark - scenario={args.scenario} cache={label} runs={len(runs)}")
    print(f"target: {target}")

    spawn_deltas = [
        float(run["spawn_to_origin_ms"])
        for run in runs
        if run.get("spawn_to_origin_ms") is not None
    ]
    if spawn_deltas:
        print(
            f"spawn->origin (loader + interpreter init): "
            f"median {statistics.median(spawn_deltas):8.1f} ms  "
            f"[{min(spawn_deltas):.1f} - {max(spawn_deltas):.1f}]"
        )
    else:
        print("spawn->origin: unavailable (no origin_epoch in output)")

    rows = _summarize(runs)
    if not rows:
        print("\nNo milestones captured. Check the run output below.\n")
        for index, run in enumerate(runs, start=1):
            print(f"--- run {index} output ---")
            print(str(run.get("output", ""))[-2000:])
        return

    print()
    print(f"{'milestone':<20}{'n':>3}{'median':>10}{'min':>10}{'max':>10}{'stage':>10}")
    print("-" * 63)
    previous_median = 0.0
    for name, count, median, low, high in rows:
        stage = median - previous_median
        previous_median = median
        print(f"{name:<20}{count:>3}{median:>10.1f}{low:>10.1f}{high:>10.1f}{stage:>10.1f}")
    print("-" * 63)
    print("all figures in ms from the trace origin; 'stage' is median-to-median")
    print()


def _report_problems(runs: list[dict[str, object]]) -> int:
    """Print any correctness failures and return how many runs had one.

    A timing harness that does not check what the app actually did will happily
    measure the wrong thing: a launch that showed the Library instead of the
    requested document produces a completely normal-looking milestone table.
    """

    failed = [(index, run) for index, run in enumerate(runs, start=1) if run.get("problems")]
    if not failed:
        return 0
    print("CORRECTNESS FAILURES -- the timings above are not measuring what you think:")
    for index, run in failed:
        for problem in run["problems"]:  # type: ignore[index]
            print(f"  run {index}: {problem}")
    print()
    return len(failed)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))

    if args.scenario in {"file", "openwith"}:
        if args.document is None:
            print(f"--document is required for the {args.scenario} scenario", file=sys.stderr)
            return 2
        document = args.document.expanduser().resolve()
        if not document.is_file():
            print(f"No such document: {document}", file=sys.stderr)
            return 2
    else:
        document = None

    if args.exe is not None and not args.exe.is_file():
        print(f"No such executable: {args.exe}", file=sys.stderr)
        return 2

    runtime_root = Path(tempfile.mkdtemp(prefix="joyread-bench-"))
    runs: list[dict[str, object]] = []
    primary: subprocess.Popen[bytes] | None = None
    primary_console = None
    try:
        if args.scenario == "openwith":
            # One primary, held for every run: the point is the secondary's
            # cost against a warm, already-running process.
            primary_dir = runtime_root / "primary"
            primary_dir.mkdir(parents=True, exist_ok=True)
            # Held for the whole session, so piping it would stall the primary
            # the moment its buffer filled. See the note in `_run_primary`.
            primary_console = (primary_dir / "console.txt").open(
                "w", encoding="utf-8", errors="replace"
            )
            primary = subprocess.Popen(
                _launch_command(args.exe, []),
                env=_environment(primary_dir, args.exe),
                stdout=primary_console,
                stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + args.timeout
            primary_ready = False
            while time.monotonic() < deadline:
                if "first_paint" in _parse_milestones(_read_log(primary_dir)):
                    primary_ready = True
                    break
                if primary.poll() is not None:
                    print("Primary exited before it was ready", file=sys.stderr)
                    return 1
                time.sleep(0.05)
            if not primary_ready:
                print("Primary did not reach first_paint before the timeout", file=sys.stderr)
                return 1
            for _ in range(args.runs):
                assert document is not None
                runs.append(_run_secondary(args, primary_dir, document))
        else:
            arguments = [str(document)] if document is not None else []
            for index in range(args.runs):
                # A fresh runtime per run keeps each launch a real first launch
                # for this profile, so no run inherits another's warm state.
                run_dir = runtime_root / f"run{index}"
                run_dir.mkdir(parents=True, exist_ok=True)
                runs.append(_run_primary(args, run_dir, arguments))
    finally:
        if primary is not None and primary.poll() is None:
            primary.terminate()
            try:
                primary.wait(timeout=10)
            except subprocess.TimeoutExpired:
                primary.kill()
        if primary_console is not None:
            primary_console.close()
        if args.keep_runtime:
            print(f"runtime kept at {runtime_root}")
        else:
            shutil.rmtree(runtime_root, ignore_errors=True)

    _print_report(args, runs)
    broken = _report_problems(runs)
    if args.json is not None:
        payload = {
            "scenario": args.scenario,
            "cache": "cold" if args.cold else "warm",
            "target": str(args.exe) if args.exe else "source",
            "runs": [
                {
                    "milestones": run["milestones"],
                    "spawn_to_origin_ms": run.get("spawn_to_origin_ms"),
                    "problems": run.get("problems", []),
                }
                for run in runs
            ],
        }
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"raw results written to {args.json}")
    # Non-zero on a correctness failure, so this is usable as a smoke test and
    # not only as a stopwatch.
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
