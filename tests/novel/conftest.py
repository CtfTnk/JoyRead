"""Skip the whole novel suite when the ``joyread[epub]`` extra is absent.

The check runs at collection, before any module here is imported, because
importing one would reach ``joyread.novel.core.epub`` and fail on ``lxml``
rather than skip. Everything under this directory tests the novel reader, so
the gate belongs to the directory rather than to individual tests.
"""

import pytest


pytest.importorskip(
    "lxml",
    reason="the joyread[epub] extra is not installed; novel reader tests skipped",
)
