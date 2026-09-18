"""Global reader prefetch defaults and logical page-order limits."""

PREFETCH_BEFORE_DEFAULT = 4
PREFETCH_AFTER_DEFAULT = 8
PREFETCH_BEFORE_MAX = 10
PREFETCH_AFTER_MAX = 20


def prefetch_count(value: object, *, default: int, maximum: int) -> int:
    try:
        number = int(value)
    except (ValueError, TypeError, OverflowError):
        return default
    return max(0, min(maximum, number))
