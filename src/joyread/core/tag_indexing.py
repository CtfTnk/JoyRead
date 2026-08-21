"""Index tag names onto a single A-Z alphabet, romanizing CJK readings.

The tag rail is one alphabet. A Japanese or Chinese tag parked in a separate
script bucket is unreachable from it, so names are indexed by their romanized
reading -- pykakasi for Japanese, pypinyin for Chinese -- and land on the same
letters as Latin names. ``ホラー`` indexes under H, ``日常`` under N.

Qt-free by design: bucketing is a pure text transform, so it stays testable
without a QApplication and without the UI layer.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable
from functools import lru_cache
from string import ascii_uppercase
from threading import RLock
from time import perf_counter
from typing import TYPE_CHECKING

from joyread.core.models.tag import MAX_TAG_COUNT

if TYPE_CHECKING:  # pragma: no cover - typing only.
    from joyread.core.models.tag import Tag


logger = logging.getLogger(__name__)

OTHER_BUCKET = "#"
BUCKET_ORDER: tuple[str, ...] = (*ascii_uppercase, OTHER_BUCKET)

# Big enough to hold a full library's worth of readings, doubled because a
# name can be cached under either ``han_language``. At the previous 2048 the
# cache thrashed on any library past that size and stopped paying for itself
# entirely -- grouping 10,000 tags a second time measured no faster than the
# first (41ms vs 42ms). Sized to the cap it works again: a second grouping of
# 5,000 tags is 42ms cold, 2ms warm. Entries are two short strings, so the
# whole cache is a few MB at worst.
_CACHE_SIZE = MAX_TAG_COUNT * 2

# Hiragana, katakana, and katakana phonetic extensions. Any of these makes the
# name unambiguously Japanese regardless of the active UI language.
_KANA_RE = re.compile(r"[぀-ゟ゠-ヿㇰ-ㇿ]")
# Han ideographs, shared between Japanese and Chinese -- hence ``han_language``.
_HAN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")

_JAPANESE = "ja"
_CHINESE = "zh"


class _Romanizer:
    """Lazily-built romanizers, shared per process.

    pykakasi loads dictionaries when it is constructed -- about 110ms -- so
    building it at import time would put that cost on every app start even
    for a library with no Japanese tags. Import failures degrade to "no
    reading" (the name falls into ``#``) rather than taking the shelf down.

    Construction is locked because ``warm()`` runs on a background thread
    while the UI thread may already be bucketing: without it both threads
    build their own converter and pay the load twice.
    """

    def __init__(self) -> None:
        self._kakasi = None
        self._pypinyin = None
        self._kakasi_broken = False
        self._pypinyin_broken = False
        self._lock = RLock()

    def _ensure_kakasi(self) -> None:
        with self._lock:
            if self._kakasi is not None or self._kakasi_broken:
                return
            try:
                import pykakasi

                self._kakasi = pykakasi.kakasi()
            except Exception:  # pragma: no cover - depends on a broken install.
                self._kakasi_broken = True
                logger.warning("pykakasi unavailable; Japanese tags index under %s", OTHER_BUCKET)

    def _ensure_pypinyin(self) -> None:
        with self._lock:
            if self._pypinyin is not None or self._pypinyin_broken:
                return
            try:
                from pypinyin import lazy_pinyin

                self._pypinyin = lazy_pinyin
            except Exception:  # pragma: no cover - depends on a broken install.
                self._pypinyin_broken = True
                logger.warning("pypinyin unavailable; Chinese tags index under %s", OTHER_BUCKET)

    def warm(self) -> None:
        self._ensure_kakasi()
        self._ensure_pypinyin()

    def japanese(self, text: str) -> str:
        self._ensure_kakasi()
        if self._kakasi is None:
            return ""
        try:
            return "".join(part["hepburn"] for part in self._kakasi.convert(text))
        except Exception:  # pragma: no cover - defensive around a 3rd-party API.
            logger.exception("pykakasi failed to convert a tag name")
            return ""

    def chinese(self, text: str) -> str:
        self._ensure_pypinyin()
        if self._pypinyin is None:
            return ""
        try:
            return "".join(self._pypinyin(text))
        except Exception:  # pragma: no cover - defensive around a 3rd-party API.
            logger.exception("pypinyin failed to convert a tag name")
            return ""


_romanizer = _Romanizer()


def warm_romanizers() -> None:
    """Build both converters now, so the first CJK tag does not pay for them.

    Safe to call from any thread and any number of times; it touches no Qt
    objects. Call it off the UI thread -- doing it inline just moves the
    stall rather than removing it.
    """

    started = perf_counter()
    _romanizer.warm()
    logger.info("Tag romanizers warmed in %.0f ms", (perf_counter() - started) * 1000.0)


def _ascii_initial(text: str) -> str:
    """First ASCII letter of *text*, folding accents (``Éclair`` -> ``E``)."""

    for char in text:
        folded = unicodedata.normalize("NFKD", char)
        for candidate in folded:
            if "A" <= candidate <= "Z" or "a" <= candidate <= "z":
                return candidate.upper()
    return ""


@lru_cache(maxsize=_CACHE_SIZE)
def reading_of(name: str, *, han_language: str = _JAPANESE) -> str:
    """Romanized reading used for indexing and search.

    Returns ``""`` when the name needs no romanization (already Latin) or
    when no romanizer could produce one.

    ``han_language`` decides how to read characters that Japanese and Chinese
    share: ``恋愛`` is *ren'ai* (R) as Japanese but *lian'ai* (L) as Chinese.
    Names containing kana ignore it -- those are Japanese either way.
    """

    text = (name or "").strip()
    if not text:
        return ""
    if _KANA_RE.search(text):
        return _romanizer.japanese(text)
    if _HAN_RE.search(text):
        if han_language == _CHINESE:
            return _romanizer.chinese(text)
        return _romanizer.japanese(text)
    return ""


@lru_cache(maxsize=_CACHE_SIZE)
def bucket_of(name: str, *, han_language: str = _JAPANESE) -> str:
    """Index *name* onto ``A``-``Z``, or ``#`` when it has no Latin initial."""

    text = (name or "").strip()
    if not text:
        return OTHER_BUCKET
    # A Latin initial wins outright -- never pay for a dictionary lookup on a
    # name that is already alphabetical.
    initial = _ascii_initial(text[:1])
    if initial:
        return initial
    return _ascii_initial(reading_of(text, han_language=han_language)) or OTHER_BUCKET


def matches_query(name: str, query: str, *, han_language: str = _JAPANESE) -> bool:
    """Case-insensitive substring match on the display name or its reading.

    Matching the reading too means ``horror`` finds ``ホラー`` -- the reading
    is already computed for bucketing, so this costs nothing extra.
    """

    needle = (query or "").strip().casefold()
    if not needle:
        return True
    if needle in (name or "").casefold():
        return True
    reading = reading_of(name, han_language=han_language)
    return bool(reading) and needle in reading.casefold()


def group_tags(
    tags: Iterable[Tag],
    *,
    han_language: str = _JAPANESE,
    query: str = "",
) -> list[tuple[str, tuple[Tag, ...]]]:
    """Bucket *tags* by initial, in ``BUCKET_ORDER``, dropping empty buckets.

    Empty buckets are dropped because the rail is built from this result: a
    strip of 27 letters most of which lead nowhere is worse than a short one
    that always lands on tags.
    """

    buckets: dict[str, list[Tag]] = {}
    for tag in tags:
        if not matches_query(tag.name, query, han_language=han_language):
            continue
        buckets.setdefault(bucket_of(tag.name, han_language=han_language), []).append(tag)
    return [
        (letter, tuple(sorted(buckets[letter], key=lambda t: _sort_key(t, han_language))))
        for letter in BUCKET_ORDER
        if letter in buckets
    ]


def _sort_key(tag: Tag, han_language: str) -> tuple[str, str]:
    reading = reading_of(tag.name, han_language=han_language)
    return ((reading or tag.name).casefold(), tag.name.casefold())


def clear_caches() -> None:
    """Drop memoized readings. Used by tests that switch ``han_language``."""

    reading_of.cache_clear()
    bucket_of.cache_clear()


__all__ = [
    "BUCKET_ORDER",
    "OTHER_BUCKET",
    "bucket_of",
    "clear_caches",
    "group_tags",
    "matches_query",
    "reading_of",
    "warm_romanizers",
]
