#!/usr/bin/env python3
"""Drive a real Reader window and emit repeatable performance measurements."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import perf_counter


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="PDF or manga archive to exercise")
    parser.add_argument("--pool-gb", type=int, default=5, choices=range(1, 51))
    parser.add_argument("--loops", type=int, default=1)
    parser.add_argument("--pages", type=int, default=20)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    source = args.source.expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"Source does not exist: {source}")
    if args.loops < 1 or args.pages < 1:
        raise SystemExit("--loops and --pages must be positive")

    temporary_runtime = None
    if args.runtime_dir is None:
        temporary_runtime = TemporaryDirectory(prefix="joyread-reader-perf-")
        runtime_dir = Path(temporary_runtime.name)
    else:
        runtime_dir = args.runtime_dir.expanduser().resolve()
        runtime_dir.mkdir(parents=True, exist_ok=True)
    os.environ["JOYREAD_RUNTIME_DIR"] = str(runtime_dir)
    os.environ["JOYREAD_READER_PERF"] = "1"

    try:
        return _run_qt_playground(args, source)
    finally:
        if temporary_runtime is not None:
            temporary_runtime.cleanup()


def _run_qt_playground(args: argparse.Namespace, source: Path) -> int:
    try:
        import psutil
    except ImportError as exc:  # pragma: no cover - manual developer entry point.
        raise SystemExit("Install JoyRead's dev dependencies to use the playground (psutil is missing).") from exc

    from PySide6.QtCore import QTimer

    from joyread.app.bootstrap import create_application
    from joyread.ui.views.reader_window import ReaderWindow

    app, context, initial_window = create_application(["joyread-reader-perf", str(source)])
    if not isinstance(initial_window, ReaderWindow):
        context.close()
        raise SystemExit("The supplied source did not create an image Reader window.")
    app.setQuitOnLastWindowClosed(False)
    context.archive_extraction_pool.resize(args.pool_gb * 1024**3)

    process = psutil.Process()
    started = perf_counter()
    rss_peak = process.memory_info().rss
    cycles: list[dict[str, object]] = []
    window: ReaderWindow | None = initial_window
    cycle_started = perf_counter()
    first_screen_ms: float | None = None
    actions: list[tuple[str, int | None]] = []
    action_started = 0.0
    action_name: str | None = None
    action_page: int | None = None
    action_conversion_count = 0
    page_prepare_ms: list[float] = []
    closing_started = 0.0
    cycle_deadline = perf_counter() + 300.0

    sample_timer = QTimer()
    sample_timer.setInterval(16)

    def sample() -> None:
        nonlocal rss_peak
        rss_peak = max(rss_peak, process.memory_info().rss)

    sample_timer.timeout.connect(sample)
    sample_timer.start()

    driver = QTimer()
    driver.setInterval(20)

    def begin_action() -> None:
        nonlocal action_started, action_name, action_page, action_conversion_count
        assert window is not None
        if not actions:
            finish_cycle()
            return
        action_name, action_page = actions.pop(0)
        action_started = perf_counter()
        action_conversion_count = int(window.canvas.performance_snapshot()["pixmap_conversions"])
        if action_name == "page":
            assert action_page is not None
            window.viewmodel.seek(action_page)
        elif action_name == "resize":
            window.resize(window.width() + 320, window.height() + 180)

    def action_is_complete() -> bool:
        assert window is not None
        elapsed = perf_counter() - action_started
        if action_name == "page":
            assert action_page is not None
            prepared = getattr(window.viewmodel, "_pages", {})
            return action_page in prepared and action_page in window.viewmodel.current_display_indices
        if action_name == "resize":
            conversions = int(window.canvas.performance_snapshot()["pixmap_conversions"])
            return conversions > action_conversion_count or elapsed >= 1.0
        return False

    def finish_cycle() -> None:
        nonlocal closing_started, action_name
        assert window is not None
        cycles.append(
            {
                "first_screen_ms": first_screen_ms,
                "page_prepare_ms": list(page_prepare_ms),
                "canvas": window.canvas.performance_snapshot(),
                "rss_before_close": process.memory_info().rss,
            }
        )
        action_name = "closing"
        closing_started = perf_counter()
        window.close()

    def close_is_drained() -> bool:
        return (
            context.archive_extraction_pool.active_lease_count == 0
            and context.cache_service.reader_page_cache.current_bytes == 0
        )

    def next_cycle_or_finish() -> None:
        nonlocal window, cycle_started, first_screen_ms, actions, action_name
        nonlocal action_page, page_prepare_ms, cycle_deadline
        cycle = cycles[-1]
        cycle["session_drain_ms"] = round((perf_counter() - closing_started) * 1000.0, 3)
        cycle["rss_post_close"] = process.memory_info().rss
        cycle["pool_bytes"] = context.archive_extraction_pool.current_bytes
        cycle["active_leases"] = context.archive_extraction_pool.active_lease_count
        if len(cycles) >= args.loops:
            finish_run()
            return
        window = ReaderWindow(context, source)
        window.show()
        cycle_started = perf_counter()
        first_screen_ms = None
        actions = []
        action_name = None
        action_page = None
        page_prepare_ms = []
        cycle_deadline = perf_counter() + 300.0

    def finish_run() -> None:
        sample_timer.stop()
        driver.stop()
        prepare_samples = [
            float(value)
            for cycle in cycles
            for value in cycle.get("page_prepare_ms", [])
        ]
        canvas_snapshots = [cycle["canvas"] for cycle in cycles]
        result = {
            "source_suffix": source.suffix.lower(),
            "pool_budget_bytes": args.pool_gb * 1024**3,
            "loops": args.loops,
            "elapsed_ms": round((perf_counter() - started) * 1000.0, 3),
            "first_screen_ms": [cycle["first_screen_ms"] for cycle in cycles],
            "page_prepare_p95_ms": _percentile(prepare_samples, 0.95),
            "page_prepare_max_ms": max(prepare_samples, default=0.0),
            "heartbeat_p95_ms": max(
                (float(snapshot["heartbeat_p95_ms"]) for snapshot in canvas_snapshots),
                default=0.0,
            ),
            "heartbeat_max_ms": max(
                (float(snapshot["heartbeat_max_ms"]) for snapshot in canvas_snapshots),
                default=0.0,
            ),
            "pixmap_conversions": sum(int(snapshot["pixmap_conversions"]) for snapshot in canvas_snapshots),
            "pixmap_duplicate_skips": sum(
                int(snapshot["pixmap_duplicate_skips"]) for snapshot in canvas_snapshots
            ),
            "paint_p95_ms": max(
                (float(snapshot["paint_p95_ms"]) for snapshot in canvas_snapshots),
                default=0.0,
            ),
            "paint_max_ms": max(
                (float(snapshot["paint_max_ms"]) for snapshot in canvas_snapshots),
                default=0.0,
            ),
            "rss_peak": rss_peak,
            "rss_post_close": [cycle["rss_post_close"] for cycle in cycles],
            "session_drain_ms": [cycle["session_drain_ms"] for cycle in cycles],
            "pool_bytes": context.archive_extraction_pool.current_bytes,
            "cycles": cycles,
        }
        payload = json.dumps(result, ensure_ascii=True, indent=2)
        if args.output is not None:
            args.output.expanduser().resolve().write_text(payload + "\n", encoding="utf-8")
        print(payload)
        app.quit()

    def drive() -> None:
        nonlocal first_screen_ms, actions, action_name, page_prepare_ms
        if perf_counter() >= cycle_deadline:
            driver.stop()
            sample_timer.stop()
            if window is not None:
                window.close()
            print(json.dumps({"error": "Reader performance cycle exceeded 300 seconds"}))
            app.exit(2)
            return
        assert window is not None
        if action_name == "closing":
            if close_is_drained() or perf_counter() - closing_started >= 30.0:
                next_cycle_or_finish()
            return
        if first_screen_ms is None:
            if window.viewmodel.page_count <= 0 or int(window.canvas.performance_snapshot()["resident_pixmaps"]) <= 0:
                return
            first_screen_ms = round((perf_counter() - cycle_started) * 1000.0, 3)
            window.canvas.reset_performance_measurements()
            count = window.viewmodel.page_count
            sequential = list(range(1, min(args.pages, count)))
            jumps = [index for index in (count - 1, count // 2, 0) if 0 <= index < count]
            actions = [("page", index) for index in (*sequential, *jumps)]
            actions.append(("resize", None))
            begin_action()
            return
        if action_name is not None and action_is_complete():
            if action_name == "page":
                page_prepare_ms.append(round((perf_counter() - action_started) * 1000.0, 3))
            action_name = None
            QTimer.singleShot(30, begin_action)

    driver.timeout.connect(drive)
    initial_window.show()
    driver.start()
    return int(app.exec())


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return ordered[index]


if __name__ == "__main__":
    raise SystemExit(main())
