"""Memory-bounded planning for sequential archive reads."""

from __future__ import annotations

from collections.abc import Iterable


MAX_SEQUENTIAL_BATCH_ITEMS = 8
MAX_SEQUENTIAL_BATCH_BYTES = 256 * 1024 * 1024


def plan_read_batch(
    candidates: Iterable[tuple[int, int | None]],
    *,
    max_items: int = MAX_SEQUENTIAL_BATCH_ITEMS,
    max_declared_bytes: int = MAX_SEQUENTIAL_BATCH_BYTES,
) -> tuple[int, ...]:
    """Return the largest ordered prefix allowed by item and byte limits.

    Unknown-size entries and entries larger than the byte limit are isolated
    so py7zr never combines an unbounded member with other requested pages.
    A single oversized entry is still returned because it must either be read
    alone or rejected by the configured extraction guardrail.
    """

    item_limit = max(1, int(max_items))
    byte_limit = max(1, int(max_declared_bytes))
    selected: list[int] = []
    declared_total = 0

    for raw_index, raw_size in candidates:
        if len(selected) >= item_limit:
            break
        page_index = int(raw_index)
        size = None if raw_size is None else max(0, int(raw_size))
        if size is None or size > byte_limit:
            if not selected:
                selected.append(page_index)
            break
        if selected and declared_total + size > byte_limit:
            break
        selected.append(page_index)
        declared_total += size

    return tuple(selected)
