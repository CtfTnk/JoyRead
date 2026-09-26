"""Optional real-book fixtures configured only in the ignored ``test_set/``.

The public suite owns generic assertions. Personal sample filenames belong in
``test_set/fixtures.json`` and are never committed with those assertions. The
optional keys are ``cbz_short``, ``cbz_large``, ``cbr``, ``rar_encrypted``,
``epub_en``, and ``epub_ja``; values are paths relative to ``test_set``.
"""

from __future__ import annotations

import json
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2] / "test_set"
_MANIFEST = _ROOT / "fixtures.json"


def local_fixture_path(key: str, fallback: str) -> Path:
    """Return a sample inside ``test_set`` or a missing generic fallback."""
    default = _ROOT / fallback
    try:
        paths = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default
    value = paths.get(key) if isinstance(paths, dict) else None
    if not isinstance(value, str) or not value:
        return default
    candidate = (_ROOT / value).resolve()
    return candidate if candidate.is_relative_to(_ROOT.resolve()) else default
