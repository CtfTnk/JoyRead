"""JoyRead application entrypoint."""

from __future__ import annotations

# Import order is load-bearing and must not be alphabetized. `startup_trace`
# records the trace origin at its own import, and it is deliberately stdlib-only
# so that instant is as close to process entry as Python allows. Importing
# `bootstrap` first would hide its own ~865 ms of module-scope imports inside
# the very window the trace exists to measure.
from joyread.app import startup_trace

import sys  # noqa: E402

from joyread.app.bootstrap import run  # noqa: E402

startup_trace.mark("bootstrap_imported")


def main() -> int:
    return run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
