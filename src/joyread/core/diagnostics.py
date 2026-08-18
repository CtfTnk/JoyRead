"""Low-overhead, opt-in performance events shared across JoyRead layers."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any


READER_PERF_ENV_VAR = "JOYREAD_READER_PERF"
READER_PERF_LOGGER_NAME = "joyread.performance.reader"
READER_PERF_ENABLED = os.environ.get(READER_PERF_ENV_VAR, "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

_logger = logging.getLogger(READER_PERF_LOGGER_NAME)


def reader_perf_enabled() -> bool:
    """Return the import-time performance switch used by hot paths."""

    return READER_PERF_ENABLED


def perf_started_ns() -> int:
    return time.perf_counter_ns()


def elapsed_ms(started_ns: int) -> float:
    return (time.perf_counter_ns() - started_ns) / 1_000_000.0


def reader_perf_event(event: str, /, **fields: object) -> None:
    """Emit one JSON event without accepting paths or arbitrary object reprs."""

    if not READER_PERF_ENABLED:
        return
    payload: dict[str, object] = {"event": str(event)}
    payload.update({str(key): _safe_value(value) for key, value in fields.items()})
    _logger.info("reader_perf %s", json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True))


def cache_identity_kind(document_cache_key: str | None) -> str:
    key = str(document_cache_key or "")
    if key.startswith("file:"):
        return "managed"
    if key.startswith("external:sha256:"):
        return "external_content"
    if key.startswith("session:"):
        return "session"
    return "other"


def _safe_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (tuple, list)):
        return [_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _safe_value(item) for key, item in value.items()}
    # Do not let Path, exception, extractor, or Qt object reprs leak into the
    # performance stream. Call sites should send explicit primitive metadata.
    return f"<{type(value).__name__}>"


__all__ = [
    "READER_PERF_ENV_VAR",
    "READER_PERF_LOGGER_NAME",
    "cache_identity_kind",
    "elapsed_ms",
    "perf_started_ns",
    "reader_perf_enabled",
    "reader_perf_event",
]
