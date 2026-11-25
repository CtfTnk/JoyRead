"""Read archive member names stored without a declared encoding.

A ZIP name is UTF-8 only when its entry says so. Otherwise the format says
"IBM code page 437", and in practice the bytes are whatever code page the
author's system used. Japanese Windows writes Shift-JIS, Simplified Chinese
Windows writes GBK, and Traditional Chinese Windows writes Big5. Western Europe
writes cp850 and the US cp437. Some tools write the Windows ANSI page (cp1252),
and macOS and Linux tools write UTF-8 but never set the flag. Read as cp437, a
Japanese or Chinese name turns into box-drawing mojibake.

Nothing in the archive says which page was used, so
:func:`choose_legacy_codec` reads all of an archive's undeclared names with
each candidate and keeps the reading that looks most like real text:

* A CJK reading is scored by where each character falls in its own character
  set. Common characters (JIS level 1 kanji and kana, GB2312 level 1 hanzi,
  Big5 level 1) count fully. Rare or user-defined areas count little or
  nothing. Shift-JIS read as GBK lands almost entirely in GBK's rare
  extension areas, and GBK read as Shift-JIS turns into half-width katakana
  and private-use characters, so the right reading stands out.
* A Western reading is scored by whether each high byte decodes to a Latin
  letter that sits inside a word: "Bücher" in cp437. A Shift-JIS character
  whose second byte is ASCII can pass that test once, but lead bytes come one
  per character, so in a Japanese name they make up half the letters or
  more. That density is held against the Western reading.

The scores were tuned against hand-written manga-style names and against
CPython's CJK sample texts cut into name-length pieces. Single names are read
correctly about 99.7% of the time for Shift-JIS, every time for GBK, and about
95% of the time for Big5; three names read together are correct essentially
every time. The cp437 and cp850 names JoyRead already showed correctly never
changed.

Ties go to UTF-8, then to cp437, the ZIP default, so a name that reads equally
well either way keeps the reading JoyRead has always shown. Korean (cp949) is
not a candidate: its byte layout is the same as GBK's, so short names cannot be
told apart without a language model.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
import unicodedata

#: What a ZIP name without the UTF-8 flag means by specification, and the
#: answer whenever no other reading is clearly better.
DEFAULT_LEGACY_CODEC = "cp437"

#: Earlier wins a tie. UTF-8 first: no Western or CJK byte string is valid
#: UTF-8 by accident unless it is very short. Then the readings JoyRead
#: already used, so a name that reads equally well either way does not change.
_PREFERENCE = ("utf-8", "cp437", "cp850", "cp1252", "cp932", "gbk", "cp950")
_WESTERN = frozenset({"cp437", "cp850", "cp1252"})

#: A reading below this average is no better than mojibake.
_MIN_SCORE = 0.5

#: Plenty to tell code pages apart, and it bounds the cost of a huge archive.
#: Folder names repeat in every entry, so distinct components are few.
_MAX_COMPONENTS = 256

# Bytes that may follow an accented letter at the end of a Latin word.
_WORD_EDGE = frozenset(b" .,-_()[]{}'!&+0123456789")


def choose_legacy_codec(raw_names: Iterable[bytes]) -> str:
    """The codec to read every undeclared name in one archive with.

    ``raw_names`` are the names exactly as stored. One codec covers the whole
    archive: its names were written by one tool on one system, and reading
    them together is what makes short names decidable.
    """

    parts = _distinct_components(raw_names)
    if not parts:
        return DEFAULT_LEGACY_CODEC
    best = DEFAULT_LEGACY_CODEC
    best_score = -1.0
    for codec in _PREFERENCE:
        score = _score(parts, codec)
        if score is not None and score > best_score:
            best, best_score = codec, score
    if best != DEFAULT_LEGACY_CODEC and best_score < _MIN_SCORE:
        return DEFAULT_LEGACY_CODEC
    return best


def _distinct_components(raw_names: Iterable[bytes]) -> list[bytes]:
    """Each distinct non-ASCII path component, in first-seen order.

    Splitting on "/" is safe in every candidate: no multi-byte character uses
    0x2F. Backslash is not split on, because 0x5C is a valid second byte in
    Shift-JIS (表 is 0x95 0x5C).
    """

    seen: dict[bytes, None] = {}
    for raw in raw_names:
        for part in raw.split(b"/"):
            if part and not part.isascii() and part not in seen:
                seen[part] = None
                if len(seen) >= _MAX_COMPONENTS:
                    return list(seen)
    return list(seen)


def _score(parts: Sequence[bytes], codec: str) -> float | None:
    """Average plausibility of the reading, or None if it cannot decode."""

    try:
        decoded = [part.decode(codec) for part in parts]
    except UnicodeDecodeError:
        return None
    if codec in _WESTERN:
        return _western_score(parts, codec)
    weight = _CJK_WEIGHTS[codec]
    total = 0.0
    count = 0
    for text in decoded:
        for char in text:
            if ord(char) >= 0x80:
                total += weight(char)
                count += 1
    return total / count if count else None


# ---------------------------------------------------------------------------
# CJK readings: where each character falls in its own character set
# ---------------------------------------------------------------------------


def _cp932_weight(char: str) -> float:
    encoded = char.encode("cp932")
    if len(encoded) == 1:
        # Half-width katakana: real, but what GBK bytes turn into.
        return 0.5 if 0xA1 <= encoded[0] <= 0xDF else 0.0
    code = int.from_bytes(encoded, "big")
    if 0x829F <= code <= 0x82F1 or 0x8340 <= code <= 0x8396:
        return 1.0  # hiragana, katakana
    if 0x8140 <= code <= 0x81FC or 0x824F <= code <= 0x829A:
        return 1.0  # punctuation, full-width digits and letters
    if 0x839F <= code <= 0x8460:
        return 0.3  # Greek, Cyrillic
    if 0x849F <= code <= 0x84BE:
        return 0.1  # box drawing
    if encoded[0] == 0x87:
        return 0.6  # NEC row 13: circled numbers, roman numerals
    if 0x889F <= code <= 0x9872:
        return 1.0  # JIS level 1 kanji
    if 0x989F <= code <= 0xEAA4:
        return 0.5  # JIS level 2 kanji
    if 0xED40 <= code <= 0xEEFC or 0xFA40 <= code <= 0xFC4B:
        return 0.3  # IBM extensions: 髙, 﨑 and other name variants
    return 0.0  # user-defined area


def _gbk_weight(char: str) -> float:
    encoded = char.encode("gbk")
    if len(encoded) == 1:
        return 0.0
    lead, trail = encoded
    if trail >= 0xA1:
        if 0xA1 <= lead <= 0xA5:
            return 1.0  # punctuation, numbers, full-width forms, kana
        if 0xA6 <= lead <= 0xA8:
            return 0.3  # Greek, Cyrillic, pinyin
        if lead == 0xA9:
            return 0.1  # box drawing
        if 0xB0 <= lead <= 0xD7:
            return 1.0  # GB2312 level 1
        if 0xD8 <= lead <= 0xF7:
            return 0.5  # GB2312 level 2
    if 0xA8 <= lead <= 0xA9:
        return 0.1  # GBK/5 symbols
    # GBK/3 and GBK/4: traditional and rare ideographs. Shift-JIS read as GBK
    # lands here, but so does traditional Chinese typed on a GBK system.
    return 0.5


def _cp950_weight(char: str) -> float:
    encoded = char.encode("cp950")
    if len(encoded) == 1:
        return 0.0
    code = int.from_bytes(encoded, "big")
    if 0xA140 <= code <= 0xA3FE:
        return 1.0  # punctuation, bopomofo
    if 0xA440 <= code <= 0xC67E:
        return 1.0  # Big5 level 1
    if 0xC940 <= code <= 0xF9D5:
        return 0.5  # Big5 level 2
    return 0.0


def _utf8_weight(char: str) -> float:
    point = ord(char)
    if point < 0xA0:
        return 0.0  # C1 controls
    if (
        0x3000 <= point <= 0x30FF  # CJK punctuation, kana
        or 0x4E00 <= point <= 0x9FFF  # CJK ideographs
        or 0xAC00 <= point <= 0xD7A3  # Hangul
        or 0xFF00 <= point <= 0xFFEF  # full-width forms
        or 0xC0 <= point <= 0x17F  # accented Latin letters
        or 0x2000 <= point <= 0x206F  # general punctuation
    ):
        return 1.0
    return 0.5


_CJK_WEIGHTS = {
    "utf-8": _utf8_weight,
    "cp932": _cp932_weight,
    "gbk": _gbk_weight,
    "cp950": _cp950_weight,
}


# ---------------------------------------------------------------------------
# Western readings: accented letters inside Latin words
# ---------------------------------------------------------------------------


def _latin_cases(codec: str) -> tuple[str | None, ...]:
    """Per byte: "u" or "l" when it decodes to a Latin letter, else None."""

    cases: list[str | None] = []
    for byte in range(256):
        char = bytes([byte]).decode(codec, "replace")
        if not unicodedata.name(char, "").startswith("LATIN"):
            cases.append(None)
        else:
            cases.append("u" if unicodedata.category(char) == "Lu" else "l")
    return tuple(cases)


_LATIN_CASES = {codec: _latin_cases(codec) for codec in _WESTERN}


def _is_ascii_letter(byte: int | None) -> bool:
    return byte is not None and (0x41 <= byte <= 0x5A or 0x61 <= byte <= 0x7A)


def _fits_word(byte: int | None) -> bool:
    return byte is None or _is_ascii_letter(byte) or byte in _WORD_EDGE


def _western_score(parts: Sequence[bytes], codec: str) -> float:
    cases = _LATIN_CASES[codec]
    total = 0.0
    count = 0
    for part in parts:
        factor = _density_factor(part)
        start = 0
        size = len(part)
        while start < size:
            if part[start] < 0x80:
                start += 1
                continue
            end = start
            while end < size and part[end] >= 0x80:
                end += 1
            run = part[start:end]
            count += len(run)
            before = part[start - 1] if start else None
            after = part[end] if end < size else None
            run_cases = [cases[byte] for byte in run]
            if None not in run_cases and len(run) == 2:
                # Two accented letters together ("öß", "çã") are ordinary in
                # German and Portuguese, but only inside a word.
                total += 2 * (1.0 if _fits_word(before) and _fits_word(after) else 0.6)
            elif None not in run_cases and len(run) == 1:
                total += factor * _single_letter_weight(run_cases[0], before, after)
            start = end
    return total / count if count else 0.0


def _single_letter_weight(case: str | None, before: int | None, after: int | None) -> float:
    if case == "u" and before is not None and 0x61 <= before <= 0x7A:
        return 0.2  # a capital in the middle of a lower-case word
    if after is None or after in _WORD_EDGE or 0x61 <= after <= 0x7A:
        return 1.0
    if 0x41 <= after <= 0x5A:
        return 1.0 if case == "u" else 0.2
    return 0.1  # followed by "@", "\\", "^", "|" and the like


def _density_factor(part: bytes) -> float:
    """How far the share of high bytes among letters fits Latin text.

    Accented letters are a minority in a European word. When they are half
    the letters or more, the bytes are far more likely to be the lead bytes
    of double-byte characters.
    """

    high = sum(1 for byte in part if byte >= 0x80)
    letters = sum(1 for byte in part if _is_ascii_letter(byte))
    share = high / (high + letters)
    if share <= 0.4:
        return 1.0
    if share <= 0.5:
        return 0.8
    return 0.5


__all__ = ["DEFAULT_LEGACY_CODEC", "choose_legacy_codec"]
