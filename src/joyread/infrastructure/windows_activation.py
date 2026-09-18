"""Windows foreground requests for explicit launches, without focus-stealing tricks."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import lru_cache
import logging
import sys


logger = logging.getLogger(__name__)
IS_WINDOWS = sys.platform == "win32"


@lru_cache(maxsize=1)
def _user32():
    # Load lazily: importing the broker must stay portable and lightweight.
    api = ctypes.WinDLL("user32", use_last_error=True)
    api.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
    api.AllowSetForegroundWindow.restype = wintypes.BOOL
    # HWND is pointer-sized; ctypes' default int would truncate it on Win64.
    api.SetForegroundWindow.argtypes = (wintypes.HWND,)
    api.SetForegroundWindow.restype = wintypes.BOOL
    return api


def allow_foreground_process(process_id: int) -> bool:
    """Let the launched secondary pass its foreground eligibility to the primary."""
    if not IS_WINDOWS or not 0 < process_id < 0xFFFFFFFF:
        return False
    try:
        allowed = bool(_user32().AllowSetForegroundWindow(process_id))
    except OSError:
        logger.debug("Windows foreground permission unavailable", exc_info=True)
        return False
    logger.debug("Windows foreground permission pid=%d allowed=%s", process_id, allowed)
    return allowed


def request_foreground_window(window_id: int) -> bool:
    """Activate an already-shown HWND while respecting Windows foreground policy."""
    if not IS_WINDOWS or window_id <= 0:
        return False
    try:
        activated = bool(_user32().SetForegroundWindow(window_id))
    except OSError:
        logger.debug("Windows foreground activation unavailable", exc_info=True)
        return False
    logger.debug("Windows foreground request hwnd=%d activated=%s", window_id, activated)
    return activated
