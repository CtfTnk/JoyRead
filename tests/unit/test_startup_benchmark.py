"""Correctness guards for the packaged-startup benchmark harness."""

from __future__ import annotations

import importlib.util
import json
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


def test_cloned_profile_rebases_both_library_paths(tmp_path: Path) -> None:
    benchmark = _load_benchmark()
    template = tmp_path / "fixture" / "profile_template"
    config = template / ".joyread_support" / "Config"
    config.mkdir(parents=True)
    original_root = str(template / "JoyRead-Library")
    (config / "settings.json").write_text(
        json.dumps({"storage_location": original_root, "last_good_storage_location": original_root}),
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    args = benchmark._parse_args(["--fixture-dir", str(template.parent), "--app-cache", "clean"])

    benchmark._prepare_run_dir(args, run_dir, None, populated=True)

    settings = json.loads((run_dir / ".joyread_support" / "Config" / "settings.json").read_text())
    assert settings["storage_location"] == str(run_dir / "JoyRead-Library")
    assert settings["last_good_storage_location"] == settings["storage_location"]
    assert json.loads((config / "settings.json").read_text())["storage_location"] == original_root


def test_memory_sampler_labels_parent_only_when_child_listing_is_denied(monkeypatch) -> None:
    benchmark = _load_benchmark()

    class _Memory:
        rss = 1234

    class _Process:
        def memory_info(self):
            return _Memory()

        def children(self, *, recursive):  # noqa: ANN001
            raise PermissionError("process listing denied")

    monkeypatch.setattr(benchmark.psutil, "Process", lambda _pid: _Process())

    assert benchmark._rss_tree(42) == 1234
    assert benchmark._MEMORY_SCOPE == "parent_only"


def test_benchmark_rejects_an_error_surface_before_usable_content(monkeypatch, tmp_path: Path) -> None:
    benchmark = _load_benchmark()

    class _Process:
        pid = 42

        def poll(self):
            return None

    monkeypatch.setattr(benchmark, "_rss_tree", lambda _pid: 100)
    monkeypatch.setattr(
        benchmark,
        "_read_log",
        lambda _path: "startup first_paint at 12.0 ms (+1.0 ms)\nstartup library_error_visible at 13.0 ms (+1.0 ms)",
    )

    marks, peak, problems = benchmark._wait_for_content(_Process(), tmp_path, "library_first_book_paint", 1.0)

    assert marks["library_error_visible"] == 13.0
    assert peak == 100
    assert problems == ["an error surface was shown before usable content"]
