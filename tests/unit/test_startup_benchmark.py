"""Correctness guards for the packaged-startup benchmark harness."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from threading import Thread
import time


REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = REPO_ROOT / "scripts" / "bench_startup.py"


def _load_benchmark():
    spec = importlib.util.spec_from_file_location("bench_startup", BENCHMARK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_openwith_waits_for_the_primary_delivery_belonging_to_this_run(tmp_path: Path) -> None:
    benchmark = _load_benchmark()
    log_file = tmp_path / benchmark.LOG_RELATIVE
    log_file.parent.mkdir(parents=True)
    log_file.write_text("Reader window created\nLaunch intent delivered\n", encoding="utf-8")
    offset = log_file.stat().st_size

    def append_current_delivery() -> None:
        time.sleep(0.05)
        with log_file.open("a", encoding="utf-8") as stream:
            stream.write("Dispatching launch request\n")
            stream.flush()
            time.sleep(0.05)
            stream.write("Existing Reader window focused\nLaunch intent delivered\n")

    writer = Thread(target=append_current_delivery)
    writer.start()
    try:
        text, problems = benchmark._wait_for_primary_delivery(tmp_path, offset, 1.0)
    finally:
        writer.join()

    assert "Existing Reader window focused" in text
    assert problems == []


def test_openwith_rejects_a_completed_dispatch_without_a_reader() -> None:
    benchmark = _load_benchmark()

    problems = benchmark._primary_delivery_problems(
        "Dispatching launch request\nLaunch intent delivered\n"
    )

    assert problems == ["primary dispatched the intent but opened or focused no Reader window"]
