"""Cooperative cancellation between thumbnail preparation stages."""

from threading import Event


class ThumbnailCancelled(Exception):
    """An obsolete thumbnail should stop without reporting a render failure."""


def check_thumbnail_cancelled(cancelled: Event | None) -> None:
    # Native reads/decodes cannot be preempted safely. Their callers check on
    # return and before the next costly stage; no resource is released early.
    if cancelled is not None and cancelled.is_set():
        raise ThumbnailCancelled
