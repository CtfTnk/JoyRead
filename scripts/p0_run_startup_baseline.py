#!/usr/bin/env python3
"""Run the complete P0 source or packaged startup matrix on one desktop.

Example (from the repository conda environment)::

    python scripts/p0_run_startup_baseline.py --output reports/p0-2026-09-24/mac-source
    python scripts/p0_run_startup_baseline.py --output reports/p0-2026-09-24/mac-package --exe dist/JoyRead.app/Contents/MacOS/JoyRead

The script prepares a disposable 50-book profile, runs 3 repetitions of each
Library/CBZ/PDF scenario in clean and warm application-cache conditions, and
writes JSON plus a concise index. Keep the output in Git-ignored ``reports/``
until P5 acceptance, then delete it. Native Finder/Explorer checks are separate.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys


SCRIPT_DIR = Path(__file__).resolve().parent

NATIVE_CHECKLIST = """# P0 native desktop behavior checklist

Use the packaged app built from the same source revision as the JSON runs. Use
the generated CBZ/PDF files and a disposable JoyRead profile. Record the exact
package checksum and observed result for each row; this is behavior evidence,
not a timer. The CLI `file`/`openwith` runs do not cover these native requests.

| Platform | Action | Observed result / log evidence |
| --- | --- | --- |
| macOS | Finder Open With one CBZ, then one PDF, with JoyRead closed | Pending |
| macOS | Finder Open With two supported files together | Pending |
| macOS | Finder Open With a file while JoyRead is already running | Pending |
| macOS | Dock reopen after closing the last JoyRead window | Pending |
| Windows | Explorer Open With one CBZ, then one PDF, with JoyRead closed | Pending |
| Windows | Explorer Open With a file while JoyRead is already running; check foreground | Pending |
| Both | Open the same canonical path twice, including a symlink/alias where supported | Pending |
| Both | Open a missing or invalid supported-suffix file | Pending |
| Both | Launch with configured Library unavailable; record recovery and Reader result | Pending |
| Both | Close the last window and launch a second time | Pending |

Do not alter the user's real Library for the unavailable-Library case. On macOS,
`open --env JOYREAD_RUNTIME_DIR=<disposable-profile> -a <test-app> <file>` can
launch through LaunchServices with an isolated profile; also perform a Finder UI
pass. On Windows, use a dedicated test user/profile for Explorer association.
"""


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--fixture-dir", type=Path, help="Reuse a previously generated fixture.")
    parser.add_argument("--exe", type=Path, help="Packaged executable; omit for source.")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=90.0)
    return parser.parse_args(argv)


def _row(payload: dict[str, object]) -> str:
    runs = payload["runs"]
    assert isinstance(runs, list)
    scenario = str(payload["scenario"])
    mark = "library_first_book_paint" if scenario == "library" else (
        "primary_reader_first_page_paint" if scenario == "openwith" else "reader_first_page_paint"
    )
    times = [float(run["milestones"][mark]) for run in runs if mark in run["milestones"]]
    memory = [float(run["peak_rss_bytes"]) / 1024**2 for run in runs if run.get("peak_rss_bytes")]
    failures = sum(bool(run["problems"]) for run in runs)
    name = f"{scenario}/{payload['document'] and Path(str(payload['document'])).suffix[1:] or 'library'}/{payload['app_cache']}"
    timing = f"{statistics.median(times):.1f} ({min(times):.1f}–{max(times):.1f})" if times else "—"
    rss = f"{statistics.median(memory):.1f}" if memory else "—"
    return f"| {name} | {len(runs)} | {timing} | {rss} | {failures} |"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.runs < 1:
        raise SystemExit("--runs must be positive")
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    fixture = args.fixture_dir.expanduser().resolve() if args.fixture_dir else output / "fixture"
    if not (fixture / "manifest.json").is_file():
        subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "p0_prepare_startup_fixture.py"), str(fixture)],
            check=True,
        )

    matrix = (
        ("library", None),
        ("file", fixture / "samples" / "reader-20-pages.cbz"),
        ("file", fixture / "samples" / "reader-20-pages.pdf"),
        ("openwith", fixture / "samples" / "reader-20-pages.cbz"),
        ("openwith", fixture / "samples" / "reader-20-pages.pdf"),
    )
    payloads: list[dict[str, object]] = []
    failures: list[str] = []
    for scenario, document in matrix:
        for cache in ("clean", "warm"):
            name = f"{scenario}-{document.suffix[1:] if document else 'books'}-{cache}"
            json_path = output / f"{name}.json"
            command = [
                sys.executable,
                str(SCRIPT_DIR / "p0_bench_startup.py"),
                "--fixture-dir", str(fixture),
                "--scenario", scenario,
                "--app-cache", cache,
                "--runs", str(args.runs),
                "--timeout", str(args.timeout),
                "--json", str(json_path),
            ]
            if document is not None:
                command.extend(("--document", str(document)))
            if args.exe is not None:
                command.extend(("--exe", str(args.exe.expanduser().resolve())))
            console_path = output / f"{name}.log"
            with console_path.open("w", encoding="utf-8") as console:
                result = subprocess.run(command, stdout=console, stderr=subprocess.STDOUT)
            print(f"{name}: {'OK' if result.returncode == 0 else 'FAILED'} ({console_path})", flush=True)
            if json_path.is_file():
                payloads.append(json.loads(json_path.read_text(encoding="utf-8")))
            if result.returncode != 0:
                failures.append(name)

    lines = [
        "# JoyRead P0 startup measurement index",
        "",
        f"Target: {'packaged ' + str(args.exe.expanduser().resolve()) if args.exe else 'source tree'}.",
        "All times are milliseconds. Each cell is median (minimum–maximum). Memory is peak RSS, MiB, for the scope named below.",
        "Library/file content times start at JoyRead's in-process trace origin; forwarding times start before spawning the secondary process. The JSON records spawn-to-origin separately.",
        "`first_paint` is a Qt paint event, not proof that the OS compositor has presented the frame.",
        "The operating system file cache was not controlled. `file` and `openwith` use CLI arguments, not native Finder events.",
        "",
        "| Scenario / file / app cache | Runs | Usable content | Peak RSS median | Failures |",
        "| --- | ---: | ---: | ---: | ---: |",
        *(_row(payload) for payload in payloads),
        "",
        f"Memory scope: {payloads[0]['memory_scope'] if payloads else 'unavailable'}.",
        f"Failures: {', '.join(failures) if failures else 'none'}.",
        "See the individual JSON and console logs for revision, environment, fixture hashes, and every run.",
    ]
    (output / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (output / "NATIVE_CHECKLIST.md").write_text(NATIVE_CHECKLIST, encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
