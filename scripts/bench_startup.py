#!/usr/bin/env python3
"""Measure JoyRead startup on a native desktop with a disposable profile.

``file`` and ``openwith`` pass a path on the command line. They do not emulate
macOS Finder's native QFileOpenEvent. Run the native behavior checklist too.
The OS file cache is never cleared or described as cold by this tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

try:
    import psutil
except ImportError as exc:  # pragma: no cover - developer environment prerequisite.
    raise SystemExit("Install JoyRead's dev dependencies (psutil) before benchmarking.") from exc


REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_RELATIVE = Path(".joyread_support") / "Logs" / "joyread.log"
SETTLE_SECONDS = 2.0
POLL_SECONDS = 0.02
_MEMORY_SCOPE = "process_tree"

_MILESTONE_RE = re.compile(
    r"startup (?P<name>[a-z_]+) at (?P<elapsed>[0-9.]+) ms \(\+(?P<stage>[0-9.]+) ms\)"
)
_ORIGIN_EPOCH_RE = re.compile(r"origin_epoch[\"']?[:=]\s*(?P<epoch>[0-9.]+)")
_READER_OPENED_RE = re.compile(r"Reader window created")
_LIBRARY_FALLBACK_RE = re.compile(r"No document could be opened at launch|Launch settled with no document")
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
    "library_first_book_paint",
    "library_ready_empty",
    "reader_first_page_paint",
    "secondary_process_exit",
    "primary_reader_first_page_paint",
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--exe", type=Path, help="Packaged executable; source tree is the default.")
    parser.add_argument("--fixture-dir", type=Path, help="Output of prepare_startup_fixture.py.")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--scenario", choices=("library", "file", "openwith"), default="library")
    parser.add_argument("--document", type=Path, help="Required by file/openwith; normally a fixture sample.")
    parser.add_argument("--app-cache", choices=("clean", "warm"), default="clean")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--keep-runtime", action="store_true")
    parser.add_argument("--json", type=Path, help="Write per-run results and environment metadata.")
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


def _parse_milestones(output: str) -> dict[str, float]:
    found: dict[str, float] = {}
    for match in _MILESTONE_RE.finditer(output):
        found.setdefault(match.group("name"), float(match.group("elapsed")))
    return found


def _parse_origin_epoch(output: str) -> float | None:
    match = _ORIGIN_EPOCH_RE.search(output)
    return float(match.group("epoch")) if match is not None else None


def _read_log(runtime_dir: Path) -> str:
    path = runtime_dir / LOG_RELATIVE
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _log_size(runtime_dir: Path) -> int:
    path = runtime_dir / LOG_RELATIVE
    return path.stat().st_size if path.is_file() else 0


def _read_log_since(runtime_dir: Path, offset: int) -> str:
    path = runtime_dir / LOG_RELATIVE
    if not path.is_file():
        return ""
    with path.open("rb") as stream:
        stream.seek(offset)
        return stream.read().decode("utf-8", errors="replace")


def _primary_delivery_problems(output: str) -> list[str]:
    problems: list[str] = []
    if not _INTENT_DISPATCHED_RE.search(output):
        problems.append("primary never dispatched the forwarded intent")
    elif not _READER_ACTIVATED_RE.search(output):
        problems.append("primary dispatched the intent but opened or focused no Reader window")
    if not _INTENT_DELIVERED_RE.search(output):
        problems.append("primary did not finish delivering the forwarded intent")
    return problems


def _wait_for_primary_delivery(runtime_dir: Path, offset: int, timeout: float) -> tuple[str, list[str]]:
    """Compatibility seam for the existing delivery-correlation tests."""

    deadline = time.monotonic() + timeout
    output = ""
    while time.monotonic() < deadline:
        output = _read_log_since(runtime_dir, offset)
        if _INTENT_DELIVERED_RE.search(output):
            break
        time.sleep(POLL_SECONDS)
    return output, _primary_delivery_problems(output)


def _rss_tree(pid: int) -> int:
    global _MEMORY_SCOPE
    try:
        process = psutil.Process(pid)
        total = process.memory_info().rss
        if _MEMORY_SCOPE == "parent_only":
            return total
        try:
            children = process.children(recursive=True)
        except (psutil.Error, OSError):
            # Some desktop automation sandboxes deny the system-wide process
            # listing that psutil uses for children(). Never label this a
            # process-tree peak when only the parent was observable.
            _MEMORY_SCOPE = "parent_only"
            return total
        for child in children:
            try:
                total += child.memory_info().rss
            except (psutil.Error, OSError):
                continue
        return total
    except (psutil.Error, OSError):
        return 0


def _target_milestone(scenario: str, populated: bool) -> str:
    if scenario == "library":
        return "library_first_book_paint" if populated else "library_ready_empty"
    return "reader_first_page_paint"


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _wait_for_content(
    process: subprocess.Popen[bytes],
    runtime_dir: Path,
    target: str,
    timeout: float,
) -> tuple[dict[str, float], int, list[str]]:
    deadline = time.monotonic() + timeout
    settle_until: float | None = None
    peak = 0
    milestones: dict[str, float] = {}
    problems: list[str] = []
    while time.monotonic() < deadline:
        peak = max(peak, _rss_tree(process.pid))
        milestones = _parse_milestones(_read_log(runtime_dir))
        if "library_error_visible" in milestones or "reader_error_visible" in milestones:
            problems.append("an error surface was shown before usable content")
            break
        if target in milestones and "first_paint" in milestones:
            if settle_until is None:
                settle_until = time.monotonic() + SETTLE_SECONDS
            elif time.monotonic() >= settle_until:
                break
        if process.poll() is not None:
            problems.append(f"process exited before usable content ({process.returncode})")
            break
        time.sleep(POLL_SECONDS)
    else:
        problems.append(f"timed out waiting for {target} and first_paint")
    if target not in milestones and not problems:
        problems.append(f"missing {target}")
    if "first_paint" not in milestones and not problems:
        problems.append("missing first_paint")
    return milestones, peak, problems


def _run_primary(
    args: argparse.Namespace,
    runtime_dir: Path,
    arguments: list[str],
    target: str,
) -> dict[str, object]:
    console_path = runtime_dir / "console.txt"
    spawned_at = time.time()
    with console_path.open("w", encoding="utf-8", errors="replace") as console:
        process = subprocess.Popen(
            _launch_command(args.exe, arguments),
            env=_environment(runtime_dir, args.exe),
            stdout=console,
            stderr=subprocess.STDOUT,
        )
        try:
            milestones, peak, problems = _wait_for_content(process, runtime_dir, target, args.timeout)
        finally:
            _stop_process(process)
    output = _read_log(runtime_dir) or console_path.read_text(encoding="utf-8", errors="replace")
    milestones = milestones or _parse_milestones(output)
    if arguments:
        if _LIBRARY_FALLBACK_RE.search(output):
            problems.append("document launch fell back to Library")
        elif not _READER_OPENED_RE.search(output):
            problems.append("document launch created no Reader window")
    elif target == "library_first_book_paint" and "library_ready_empty" in milestones:
        problems.append("populated fixture appeared empty")
    origin_epoch = _parse_origin_epoch(output)
    return {
        "milestones": milestones,
        "spawn_to_origin_ms": (origin_epoch - spawned_at) * 1000 if origin_epoch else None,
        "peak_rss_bytes": peak,
        "problems": problems,
        "output": output[-4000:],
    }


def _run_forwarded(
    args: argparse.Namespace,
    runtime_dir: Path,
    document: Path,
    *,
    populated: bool,
) -> dict[str, object]:
    primary_console_path = runtime_dir / "primary_console.txt"
    with primary_console_path.open("w", encoding="utf-8", errors="replace") as primary_console:
        primary = subprocess.Popen(
            _launch_command(args.exe, []),
            env=_environment(runtime_dir, args.exe),
            stdout=primary_console,
            stderr=subprocess.STDOUT,
        )
        try:
            ready_target = _target_milestone("library", populated)
            _ready, idle_peak, ready_problems = _wait_for_content(primary, runtime_dir, ready_target, args.timeout)
            if ready_problems:
                return {
                    "milestones": {},
                    "peak_rss_bytes": idle_peak,
                    "problems": [f"primary not ready: {problem}" for problem in ready_problems],
                    "output": _read_log(runtime_dir)[-4000:],
                }

            offset = _log_size(runtime_dir)
            primary_origin_epoch = _parse_origin_epoch(_read_log(runtime_dir))
            secondary_console_path = runtime_dir / "secondary_console.txt"
            spawned_at = time.time()
            started = time.perf_counter()
            with secondary_console_path.open("w", encoding="utf-8", errors="replace") as secondary_console:
                secondary = subprocess.Popen(
                    _launch_command(args.exe, [str(document)]),
                    env=_environment(runtime_dir, args.exe),
                    stdout=secondary_console,
                    stderr=subprocess.STDOUT,
                )
                settle_until: float | None = None
                settled = False
                deadline = time.monotonic() + args.timeout
                peak = 0
                primary_peak = 0
                process_exit_ms: float | None = None
                delivery = ""
                while time.monotonic() < deadline:
                    primary_rss = _rss_tree(primary.pid)
                    primary_peak = max(primary_peak, primary_rss)
                    peak = max(peak, primary_rss + _rss_tree(secondary.pid))
                    delivery = _read_log_since(runtime_dir, offset)
                    marks = _parse_milestones(delivery)
                    if "reader_error_visible" in marks or "library_error_visible" in marks:
                        break
                    if secondary.poll() is not None and process_exit_ms is None:
                        process_exit_ms = (time.perf_counter() - started) * 1000
                    if "reader_first_page_paint" in marks and _INTENT_DELIVERED_RE.search(delivery) and secondary.poll() is not None:
                        if settle_until is None:
                            settle_until = time.monotonic() + SETTLE_SECONDS
                        elif time.monotonic() >= settle_until:
                            settled = True
                            break
                    if primary.poll() is not None:
                        break
                    time.sleep(POLL_SECONDS)
                _stop_process(secondary)
            secondary_output = secondary_console_path.read_text(encoding="utf-8", errors="replace")
            delivery = _read_log_since(runtime_dir, offset)
            marks = _parse_milestones(delivery)
            if process_exit_ms is None:
                process_exit_ms = (time.perf_counter() - started) * 1000
            page_ms = (
                (primary_origin_epoch + marks["reader_first_page_paint"] / 1000 - spawned_at) * 1000
                if primary_origin_epoch is not None and "reader_first_page_paint" in marks
                else None
            )
            metrics = {"secondary_process_exit": process_exit_ms}
            if page_ms is not None:
                metrics["primary_reader_first_page_paint"] = page_ms
            problems = _primary_delivery_problems(delivery)
            if secondary.returncode != 0:
                problems.append(f"secondary exited {secondary.returncode}")
            if not _SECONDARY_FORWARDED_RE.search(secondary_output):
                problems.append("secondary did not forward its intent")
            if not _READER_OPENED_RE.search(delivery):
                problems.append("forwarded request did not create a new Reader")
            if "reader_first_page_paint" not in marks:
                problems.append("primary never painted a readable page")
            if "reader_error_visible" in marks or "library_error_visible" in marks:
                problems.append("an error surface was shown")
            if not settled:
                problems.append("timed out before forwarded content settled")
            return {
                "milestones": metrics,
                "primary_ready_milestones": _ready,
                "secondary_milestones": _parse_milestones(secondary_output),
                "spawn_to_origin_ms": (
                    (_parse_origin_epoch(secondary_output) - spawned_at) * 1000
                    if _parse_origin_epoch(secondary_output) is not None else None
                ),
                "peak_rss_bytes": peak,
                "primary_peak_rss_bytes": primary_peak,
                "primary_idle_peak_rss_bytes": idle_peak,
                "problems": problems,
                "output": (secondary_output + "\n--- primary delivery ---\n" + delivery)[-4000:],
            }
        finally:
            _stop_process(primary)


def _summarize(runs: list[dict[str, object]]) -> list[tuple[str, int, float, float, float]]:
    names = {name for run in runs for name in run["milestones"]}  # type: ignore[union-attr]
    rows: list[tuple[str, int, float, float, float]] = []
    for name in sorted(names, key=lambda value: (MILESTONE_ORDER.index(value) if value in MILESTONE_ORDER else 99, value)):
        values = [float(run["milestones"][name]) for run in runs if name in run["milestones"]]  # type: ignore[index,operator]
        rows.append((name, len(values), statistics.median(values), min(values), max(values)))
    return rows


def _print_report(args: argparse.Namespace, runs: list[dict[str, object]]) -> None:
    print(f"JoyRead startup: {args.scenario}, app cache={args.app_cache}, OS file cache=uncontrolled, runs={len(runs)}")
    print(f"target: {args.exe or 'source tree'}")
    for name, count, median, low, high in _summarize(runs):
        print(f"{name:<34} n={count:<2} median={median:9.1f} ms  range={low:9.1f}..{high:9.1f}")
    rss = [int(run["peak_rss_bytes"]) / (1024 * 1024) for run in runs if run.get("peak_rss_bytes")]
    if rss:
        print(f"peak RSS ({_MEMORY_SCOPE}): median={statistics.median(rss):.1f} MiB, range={min(rss):.1f}..{max(rss):.1f}")
    failed = [(index, run) for index, run in enumerate(runs, 1) if run.get("problems")]
    for index, run in failed:
        print(f"run {index} failed: {', '.join(run['problems'])}")  # type: ignore[arg-type]
    print(f"correctness failures: {len(failed)}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_revision() -> str | None:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _git_dirty() -> bool | None:
    result = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True)
    return bool(result.stdout.strip()) if result.returncode == 0 else None


def _reset_logs(runtime_dir: Path) -> None:
    logs = runtime_dir / LOG_RELATIVE.parent
    shutil.rmtree(logs, ignore_errors=True)
    logs.mkdir(parents=True, exist_ok=True)
    for path in runtime_dir.glob("*console.txt"):
        path.unlink()


def _prepare_run_dir(args: argparse.Namespace, run_dir: Path, document: Path | None, populated: bool) -> None:
    if args.fixture_dir is not None:
        shutil.copytree(args.fixture_dir / "profile_template", run_dir)
        settings_path = run_dir / ".joyread_support" / "Config" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        # The settings store saves an absolute Library root. Without rebasing
        # both fields, a copied run silently reads the template's database and
        # cache, defeating isolation and corrupting every cold/warm label.
        storage = str(run_dir / "JoyRead-Library")
        settings["storage_location"] = storage
        settings["last_good_storage_location"] = storage
        settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True), encoding="utf-8")
    else:
        run_dir.mkdir(parents=True)
    if args.app_cache == "warm":
        warm_arguments = [str(document)] if document is not None else []
        target = _target_milestone("file" if document is not None else "library", populated)
        warmup = _run_primary(args, run_dir, warm_arguments, target)
        if warmup["problems"]:
            raise RuntimeError(f"Warmup failed: {warmup['problems']}\n{warmup['output']}")
        _reset_logs(run_dir)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.runs < 1 or args.timeout <= SETTLE_SECONDS:
        raise SystemExit("--runs must be positive and --timeout must exceed the 2-second settle window")
    if args.exe is not None:
        args.exe = args.exe.expanduser().resolve()
        if not args.exe.is_file():
            raise SystemExit(f"No executable: {args.exe}")
    fixture_manifest = None
    if args.fixture_dir is not None:
        args.fixture_dir = args.fixture_dir.expanduser().resolve()
        fixture_manifest = json.loads((args.fixture_dir / "manifest.json").read_text(encoding="utf-8"))
        if fixture_manifest.get("version") != 1 or not (args.fixture_dir / "profile_template").is_dir():
            raise SystemExit("Unsupported or incomplete fixture directory")
        for relative, expected_hash in fixture_manifest["files"].items():
            sample = args.fixture_dir / relative
            if not sample.is_file() or _sha256(sample) != expected_hash:
                raise SystemExit(f"Fixture file is missing or changed: {sample}")
    if args.scenario in ("file", "openwith"):
        if args.document is None:
            raise SystemExit("--document is required for file/openwith")
        document = args.document.expanduser().resolve()
        if not document.is_file():
            raise SystemExit(f"No document: {document}")
    else:
        document = None
    populated = fixture_manifest is not None and int(fixture_manifest["book_count"]) > 0
    root = Path(tempfile.mkdtemp(prefix="joyread-bench-"))
    runs: list[dict[str, object]] = []
    try:
        for index in range(args.runs):
            run_dir = root / f"run-{index + 1:02d}"
            _prepare_run_dir(args, run_dir, document, populated)
            if args.scenario == "openwith":
                assert document is not None
                run = _run_forwarded(args, run_dir, document, populated=populated)
            else:
                target = _target_milestone(args.scenario, populated)
                run = _run_primary(args, run_dir, [str(document)] if document else [], target)
            runs.append(run)
            print(f"run {index + 1}/{args.runs}: {'OK' if not run['problems'] else 'FAILED'}", flush=True)
    finally:
        if args.keep_runtime:
            print(f"runtime kept at {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)

    _print_report(args, runs)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 2,
            "scenario": args.scenario,
            "entry_mechanism": "cli_argv" if args.scenario == "file" else "cli_secondary_forward" if args.scenario == "openwith" else "plain_launch",
            "process_state": "running_primary" if args.scenario == "openwith" else "new_process",
            "app_cache": args.app_cache,
            "os_file_cache": "uncontrolled",
            "target": "packaged" if args.exe else "source",
            "git_revision": _git_revision(),
            "git_dirty": _git_dirty(),
            "executable": str(args.exe) if args.exe else None,
            "executable_sha256": _sha256(args.exe) if args.exe else None,
            "document": str(document) if document else None,
            "document_sha256": _sha256(document) if document else None,
            "fixture_manifest": fixture_manifest,
            "system": platform.platform(),
            "machine": platform.machine(),
            "memory_scope": _MEMORY_SCOPE,
            "python": sys.version,
            "runs": [
                {
                    **{key: value for key, value in run.items() if key != "output"},
                    **({"diagnostic_tail": run["output"]} if run["problems"] else {}),
                }
                for run in runs
            ],
        }
        args.json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"raw results written to {args.json}")
    return 1 if any(run["problems"] for run in runs) else 0


if __name__ == "__main__":
    raise SystemExit(main())
