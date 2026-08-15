#!/usr/bin/env python3
"""Drive a real Reader window and emit repeatable performance measurements."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from time import perf_counter


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="PDF or manga archive to exercise")
    parser.add_argument("--pool-gb", type=int, default=5, choices=range(1, 51))
    parser.add_argument("--loops", type=int, default=1)
    parser.add_argument("--pages", type=int, default=20)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--no-bulk-conversion",
        action="store_true",
        help="Warm through the chunked py7zr-era loop, for an in-harness baseline.",
    )
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

    archive_io = _watch_archive_io()
    if args.no_bulk_conversion:
        _disable_bulk_conversion()

    app, context, initial_window = create_application(["joyread-reader-perf", str(source)])
    if not isinstance(initial_window, ReaderWindow):
        context.close()
        raise SystemExit("The supplied source did not create an image Reader window.")
    app.setQuitOnLastWindowClosed(False)
    context.archive_extraction_pool.resize(args.pool_gb * 1024**3)
    archive_io.watch_pool(context.archive_extraction_pool)
    _watch_cache_policy(context)

    def conversion_is_running() -> bool:
        """Whether a whole-document warmup is in flight right now.

        Page-turn latency means something different before and after the
        conversion finishes, so the two must never be pooled into one p95.
        """

        coordinator = getattr(context, "archive_warmup_coordinator", None)
        return coordinator is not None and coordinator._active_key is not None  # noqa: SLF001

    process = psutil.Process()
    # Concurrent extractors each hold their own decompression dictionary, so
    # the count bounds how many of those can coexist.
    nonlocal_counts: list[int] = []
    page_cache = context.cache_service.reader_page_cache
    started = perf_counter()

    def sample_tree() -> tuple[int, int, int]:
        """Parent RSS, child RSS, and their sum at one instant.

        Extraction now runs in a `7zz` subprocess, so parent-only RSS
        understates what JoyRead costs the machine. Parent and children must be
        read at the same tick: adding separately-observed peaks gives an upper
        bound, not a real simultaneous peak.
        """

        try:
            parent = process.memory_info().rss
        except psutil.Error:
            return 0, 0, 0
        children = 0
        live = 0
        for child in process.children(recursive=True):
            try:
                children += child.memory_info().rss
                live += 1
            except psutil.Error:
                # A short-lived extractor can exit mid-walk; skip it.
                continue
        nonlocal_counts.append(live)
        return parent, children, parent + children

    rss_peak, child_peak, tree_peak = sample_tree()
    overlap_tree_peak = 0
    cache_peak_bytes = page_cache.current_bytes
    cache_peak_entries = len(page_cache)
    overlap_prepare_ms: list[float] = []
    ready_prepare_ms: list[float] = []
    action_during_conversion = False
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
        nonlocal rss_peak, child_peak, tree_peak, overlap_tree_peak
        nonlocal cache_peak_bytes, cache_peak_entries
        parent, children, tree = sample_tree()
        rss_peak = max(rss_peak, parent)
        child_peak = max(child_peak, children)
        tree_peak = max(tree_peak, tree)
        if conversion_is_running():
            overlap_tree_peak = max(overlap_tree_peak, tree)
        cache_peak_bytes = max(cache_peak_bytes, page_cache.current_bytes)
        cache_peak_entries = max(cache_peak_entries, len(page_cache))

    sample_timer.timeout.connect(sample)
    sample_timer.start()

    driver = QTimer()
    driver.setInterval(20)

    def begin_action() -> None:
        nonlocal action_started, action_name, action_page, action_conversion_count
        nonlocal action_during_conversion
        assert window is not None
        if not actions:
            finish_cycle()
            return
        action_name, action_page = actions.pop(0)
        action_started = perf_counter()
        action_during_conversion = conversion_is_running()
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
            # Three phases, never one p95: time to first visible page, page
            # turns competing with a running conversion, and page turns after
            # it has published.
            "cold_first_screen_ms": _percentile(
                [float(cycle["first_screen_ms"] or 0.0) for cycle in cycles], 0.95
            ),
            "overlap_prepare_count": len(overlap_prepare_ms),
            "overlap_prepare_p95_ms": _percentile(overlap_prepare_ms, 0.95),
            "overlap_prepare_max_ms": max(overlap_prepare_ms, default=0.0),
            "ready_prepare_count": len(ready_prepare_ms),
            "ready_prepare_p95_ms": _percentile(ready_prepare_ms, 0.95),
            "ready_prepare_max_ms": max(ready_prepare_ms, default=0.0),
            "overlap_process_tree_rss_peak": overlap_tree_peak,
            "background_thread_limit": _background_thread_limit(),
            "cache_policy": _observed_cache_policy(context),
            "bulk_extractions": archive_io.bulk_extractions,
            "backend_read_calls": archive_io.backend_reads,
            "pool_page_reads": archive_io.pool_page_reads,
            "pool_manifest_reads": archive_io.pool_manifest_reads,
            "archive_io_threads": archive_io.report(),
            "decompression_on_gui_thread": archive_io.decompression_on_gui_thread,
            "rss_peak": rss_peak,
            "child_rss_peak": child_peak,
            "max_concurrent_children": max(nonlocal_counts, default=0),
            # The honest number: parent and children measured together.
            "process_tree_rss_peak": tree_peak,
            "page_cache_peak_bytes": cache_peak_bytes,
            "page_cache_max_bytes": page_cache.max_bytes,
            "page_cache_peak_entries": cache_peak_entries,
            "page_cache_peak_fill": (
                round(cache_peak_bytes / page_cache.max_bytes, 4)
                if page_cache.max_bytes else 0.0
            ),
            "rss_post_close": [cycle["rss_post_close"] for cycle in cycles],
            "session_drain_ms": [cycle["session_drain_ms"] for cycle in cycles],
            "pool_bytes": context.archive_extraction_pool.current_bytes,
            "cycles": cycles,
        }
        payload = json.dumps(result, ensure_ascii=True, indent=2)
        try:
            if args.output is not None:
                destination = args.output.expanduser().resolve()
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(payload + "\n", encoding="utf-8")
            print(payload)
        finally:
            # Always quit. An exception escaping this slot would otherwise leave
            # the event loop running with no driver, which looks like a hang in
            # the run being measured rather than a reporting failure.
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
                elapsed = round((perf_counter() - action_started) * 1000.0, 3)
                page_prepare_ms.append(elapsed)
                # Attributed by the state at the *start* of the turn: a read
                # that began while conversion was running competed with it.
                bucket = (
                    overlap_prepare_ms if action_during_conversion else ready_prepare_ms
                )
                bucket.append(elapsed)
            action_name = None
            QTimer.singleShot(30, begin_action)

    driver.timeout.connect(drive)
    initial_window.show()
    driver.start()
    return int(app.exec())


class _ArchiveIoWatch:
    """Records which threads perform archive I/O, and how much of each kind.

    Decompression must never run on the GUI thread: a page turn that unpacks in
    the event loop freezes the window. Categories are kept apart because they
    are not equally serious -- reading a page is real work, while the readiness
    manifest is a few hundred bytes that the view legitimately asks for while
    painting. Counting the calls also answers the other half of the acceptance
    test: one bulk extraction per document instead of one per chunk.
    """

    _MANIFEST_ENTRY = "__joyread_ready_manifest__.json"

    def __init__(self) -> None:
        self.threads: dict[str, set[str]] = {}
        self.bulk_extractions = 0
        self.backend_reads = 0
        self.pool_page_reads = 0
        self.pool_manifest_reads = 0

    @property
    def decompression_on_gui_thread(self) -> bool:
        """Whether any page-scale archive work ran in the event loop."""

        return any(
            "MainThread" in names
            for category, names in self.threads.items()
            if category != "pool_manifest"
        )

    def report(self) -> dict[str, list[str]]:
        return {category: sorted(names) for category, names in sorted(self.threads.items())}

    def note(self, category: str) -> None:
        self.threads.setdefault(category, set()).add(threading.current_thread().name)

    def watch_pool(self, pool: object) -> None:
        original = pool.get

        def wrapped(cache_key, entry_name, *rest, **call_kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            if entry_name == self._MANIFEST_ENTRY:
                self.note("pool_manifest")
                self.pool_manifest_reads += 1
            else:
                self.note("pool_page")
                self.pool_page_reads += 1
            return original(cache_key, entry_name, *rest, **call_kwargs)

        pool.get = wrapped  # type: ignore[method-assign]


def _background_thread_limit() -> int | None:
    from joyread.core.archive.formats.seven_zip_command import background_thread_limit

    return background_thread_limit()


def _observed_cache_policy(context: object) -> str:
    """The policy the last opened archive session decided on, if any."""

    return getattr(context, "_joyread_perf_last_cache_policy", "unknown")


def _watch_cache_policy(context: object) -> None:
    """Record the cache policy of every archive session the run opens."""

    service = getattr(context, "archive_image_service", None)
    if service is None:
        return
    original = service.open

    def wrapped(*call_args, **call_kwargs):  # noqa: ANN002, ANN003, ANN202
        session = original(*call_args, **call_kwargs)
        plan = getattr(session, "cache_plan", None)
        policy = getattr(getattr(plan, "policy", None), "value", "unknown")
        setattr(context, "_joyread_perf_last_cache_policy", policy)
        return session

    service.open = wrapped  # type: ignore[method-assign]


def _disable_bulk_conversion() -> None:
    """Force the chunked warmup path so both halves share one harness."""

    from joyread.core.archive.formats.seven_zip_backend import SevenZipArchiveBackend

    SevenZipArchiveBackend.supports_bulk_extraction = lambda self, source: False  # type: ignore[assignment]


def _watch_archive_io() -> _ArchiveIoWatch:
    from joyread.core.archive.formats.seven_zip_backend import SevenZipArchiveBackend
    from joyread.core.archive.service import ArchiveImageService

    watch = _ArchiveIoWatch()

    def wrap(owner: object, name: str, counter: str, category: str) -> None:
        original = getattr(owner, name)

        def wrapped(*call_args, **call_kwargs):  # noqa: ANN002, ANN003, ANN202
            watch.note(category)
            setattr(watch, counter, getattr(watch, counter) + 1)
            return original(*call_args, **call_kwargs)

        setattr(owner, name, wrapped)

    wrap(SevenZipArchiveBackend, "extract_members", "bulk_extractions", "bulk_extract")
    wrap(ArchiveImageService, "_read_entries_uncached", "backend_reads", "backend_read")
    return watch


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return ordered[index]


if __name__ == "__main__":
    raise SystemExit(main())
