"""Book metadata carried inside an archive, and how competing sources resolve.

Comic archives commonly ship two sidecars that overlap: a ``meta.json`` written
by the tool that packaged the book, and a ``ComicInfo.xml`` in the widely-used
ComicRack schema. Real samples contain both, disagreeing in places, so importing
needs a stated precedence rather than whichever file happened to be read last.

The rule is **JSON wins field by field, XML fills only what JSON left empty**.
Field-by-field rather than whole-document, because the two sources are strong in
different places: ``meta.json`` carries structured, typed tags, while
``ComicInfo.xml`` is often the only one with a language.

This module is deliberately Qt-free and does no I/O, so the precedence table can
be tested without building an archive.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import unicodedata

from joyread.core.models.tag import MAX_TAG_NAME_LENGTH


#: Language tags the library actually stores, seeded in the ``languages`` table.
SUPPORTED_LANGUAGE_TAGS = frozenset({"en", "ja", "zh"})

#: What an unrecognised or absent language becomes. Never guess: a wrong
#: language is worse than an honest "unknown", and the user can still edit it.
UNKNOWN_LANGUAGE_TAG = "und"

#: External spellings mapped onto the four tags above. Deliberately small and
#: explicit -- this is a lookup table, not a language detector. Anything absent
#: becomes ``und`` rather than a guess.
_LANGUAGE_ALIASES: dict[str, str] = {
    "en": "en",
    "eng": "en",
    "en-us": "en",
    "en-gb": "en",
    "english": "en",
    "ja": "ja",
    "jp": "ja",
    "jpn": "ja",
    "ja-jp": "ja",
    "japanese": "ja",
    "zh": "zh",
    "chi": "zh",
    "zho": "zh",
    "cn": "zh",
    "zh-cn": "zh",
    "zh-tw": "zh",
    "zh-hans": "zh",
    "zh-hant": "zh",
    "chinese": "zh",
}


class MetadataSource(StrEnum):
    """Which sidecar the resolved values came from.

    Recorded so a user can tell an extracted title from a filename fallback --
    they look identical in the library otherwise.
    """

    NONE = "none"
    META_JSON = "meta.json"
    COMIC_INFO = "comicinfo"
    MERGED = "merged"


@dataclass(frozen=True)
class RawBookMetadata:
    """One sidecar's contribution, before precedence is applied.

    Every field is optional because a sidecar may carry any subset. ``None``
    means "this source said nothing", which is what lets the other source fill
    the gap without overriding an answer that was actually given.
    """

    preferred_title: str | None = None
    original_title: str | None = None
    author: str | None = None
    tags: tuple[str, ...] = ()
    language_tag: str | None = None

    def is_empty(self) -> bool:
        return not any(
            (self.preferred_title, self.original_title, self.author, self.tags, self.language_tag)
        )


@dataclass(frozen=True)
class BookMetadata:
    """Resolved metadata, ready to hand to an import."""

    preferred_title: str | None = None
    original_title: str | None = None
    author: str | None = None
    tags: tuple[str, ...] = ()
    language_tag: str = UNKNOWN_LANGUAGE_TAG
    source: MetadataSource = MetadataSource.NONE

    def is_empty(self) -> bool:
        return self.source is MetadataSource.NONE


def normalize_language_tag(value: object) -> str:
    """Map an external language string onto a tag the library stores.

    Unrecognised input becomes :data:`UNKNOWN_LANGUAGE_TAG`. A book whose
    language we cannot name is not a failure -- it is a book with an unknown
    language, which the schema already has a value for.
    """

    if value is None:
        return UNKNOWN_LANGUAGE_TAG
    text = str(value).strip().casefold().replace("_", "-")
    if not text:
        return UNKNOWN_LANGUAGE_TAG
    return _LANGUAGE_ALIASES.get(text, UNKNOWN_LANGUAGE_TAG)


def normalize_external_tags(raw_tags: object) -> tuple[str, ...]:
    """Clean a sidecar's tag list into names the library can store.

    Order-preserving and case-insensitively de-duplicated, so ``Full Color`` and
    ``full color`` in the same archive become one tag rather than two rows that
    differ only in case.

    Over-long names are **truncated, not dropped**: the tag still carries most
    of its meaning, whereas dropping it loses the information entirely.

    NFC only. No case folding beyond de-duplication, and explicitly no semantic
    or translated matching -- ``full color``, ``full-color``, and a Chinese
    translation of either stay distinct until a curated mapping or a
    user-driven merge says otherwise.
    """

    if not isinstance(raw_tags, (list, tuple)):
        return ()
    cleaned: list[str] = []
    seen: set[str] = set()
    for candidate in raw_tags:
        if not isinstance(candidate, str):
            continue
        name = unicodedata.normalize("NFC", candidate).strip()
        if not name:
            continue
        name = name[:MAX_TAG_NAME_LENGTH]
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    return tuple(cleaned)


def merge_metadata(
    json_side: RawBookMetadata | None,
    xml_side: RawBookMetadata | None,
) -> BookMetadata:
    """Resolve the two sidecars into one record.

    JSON wins per field; XML fills only fields JSON left as ``None``. Tags are a
    union rather than a winner-takes-all, because the two sources categorise
    differently and dropping one side's list loses real information -- JSON's
    order leads so its curated ordering survives.
    """

    json_side = json_side or RawBookMetadata()
    xml_side = xml_side or RawBookMetadata()

    def pick(field: str) -> str | None:
        return getattr(json_side, field) or getattr(xml_side, field) or None

    tags = normalize_external_tags(list(json_side.tags) + list(xml_side.tags))
    language = pick("language_tag")
    resolved = BookMetadata(
        preferred_title=pick("preferred_title"),
        original_title=pick("original_title"),
        author=pick("author"),
        tags=tags,
        language_tag=language or UNKNOWN_LANGUAGE_TAG,
        source=_source_of(json_side, xml_side),
    )
    return resolved


def _source_of(json_side: RawBookMetadata, xml_side: RawBookMetadata) -> MetadataSource:
    json_used = not json_side.is_empty()
    xml_used = not xml_side.is_empty()
    if json_used and xml_used:
        return MetadataSource.MERGED
    if json_used:
        return MetadataSource.META_JSON
    if xml_used:
        return MetadataSource.COMIC_INFO
    return MetadataSource.NONE


__all__ = [
    "BookMetadata",
    "MetadataSource",
    "RawBookMetadata",
    "SUPPORTED_LANGUAGE_TAGS",
    "UNKNOWN_LANGUAGE_TAG",
    "merge_metadata",
    "normalize_external_tags",
    "normalize_language_tag",
]
