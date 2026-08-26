"""Parse the metadata sidecars an archive carries.

Everything here reads bytes that came out of a **user-supplied archive**, so it
is written defensively: a malformed, hostile, or simply unexpected sidecar must
cost the book its metadata, never its import. Every parser returns ``None``
rather than raising, and the caller falls back to the filename.

The two shapes:

``meta.json``
    Written by the packaging tool. ``title`` is an object of named variants and
    ``tags`` is a list of ``{type, name}`` objects, which is what makes it the
    stronger source -- the tag *kind* is stated rather than guessed.

``ComicInfo.xml``
    The ComicRack schema. Flat elements, a comma-separated ``Tags`` string, and
    often the only source with a language.

See :mod:`joyread.core.models.archive_metadata` for how the two resolve when
they disagree.
"""

from __future__ import annotations

import json
import logging
from pathlib import PurePosixPath
from typing import Iterator
from xml.etree import ElementTree

from joyread.core.archive.inspection import ArchiveMetadataEntry
from joyread.core.models.archive_metadata import (
    SUPPORTED_LANGUAGE_TAGS,
    BookMetadata,
    RawBookMetadata,
    merge_metadata,
    normalize_external_tags,
    normalize_language_tag,
)


logger = logging.getLogger(__name__)

#: ``meta.json`` tag objects whose ``type`` becomes a library tag. Everything
#: else -- ``group``, ``parody``, ``character``, ``category``, ``language`` --
#: is a different axis of classification, and flattening them into one tag list
#: would make an artist and a genre indistinguishable in the tag browser.
_TAG_TYPE = "tag"

#: ``meta.json`` tag type that carries authorship.
_ARTIST_TYPE = "artist"

#: The ``title`` variant a packaging tool writes as the primary display name.
#:
#: Note this is a *slot name, not a language claim*: a Chinese-language sample
#: stores a Chinese display title under ``english``. Reading it as "the English
#: title" and reaching for another variant when the text is not Latin would pick
#: the wrong name.
_PREFERRED_TITLE_KEY = "english"
_ORIGINAL_TITLE_KEY = "japanese"


def read_archive_metadata(entries: tuple[ArchiveMetadataEntry, ...]) -> BookMetadata:
    """Resolve the sidecars an inspection collected into one metadata record.

    Sidecars are ranked outermost-and-shallowest first, **within each kind**. A
    nested chapter may carry its own ``ComicInfo.xml`` describing that chapter,
    which must not outrank the book-level ``ComicInfo.xml`` describing the whole
    volume.

    Across kinds, depth is deliberately not compared: a root ``meta.json`` that
    named no language has not answered, so a chapter's ``ComicInfo.xml`` may
    still fill that gap. Dropping it would lose real information for no gain,
    and every field stays editable afterwards.
    """

    resolved = _resolve_sidecars(entries)
    return merge_metadata(
        resolved.get(_META_JSON, (None, None))[1],
        resolved.get(_COMIC_INFO, (None, None))[1],
    )


def select_sidecars(
    entries: tuple[ArchiveMetadataEntry, ...],
) -> tuple[ArchiveMetadataEntry, ...]:
    """The sidecars that actually decided the metadata, at most one per kind.

    A canonical artifact carries these forward so it stays self-describing.
    Sharing :func:`_resolve_sidecars` with :func:`read_archive_metadata` is the
    point: a repackaged book whose embedded sidecar disagreed with the title in
    the library would be a very quiet bug to find.
    """

    resolved = _resolve_sidecars(entries)
    return tuple(entry for entry, _parsed in resolved.values())


_META_JSON = "meta.json"
_COMIC_INFO = "comicinfo.xml"


def _resolve_sidecars(
    entries: tuple[ArchiveMetadataEntry, ...],
) -> dict[str, tuple[ArchiveMetadataEntry, RawBookMetadata]]:
    """Best readable sidecar of each kind, ranked outermost-shallowest first.

    A sidecar that will not parse does not claim the slot -- a corrupt root
    ``meta.json`` lets a deeper one answer rather than silencing the kind.
    """

    chosen: dict[str, tuple[ArchiveMetadataEntry, RawBookMetadata]] = {}
    for entry in sorted(entries, key=_sidecar_rank):
        kind = entry.name.casefold()
        if kind in chosen:
            continue
        if kind == _META_JSON:
            parsed = parse_meta_json(entry.data)
        elif kind == _COMIC_INFO:
            parsed = parse_comic_info(entry.data)
        else:
            continue
        if parsed is not None:
            chosen[kind] = (entry, parsed)
    return chosen


def parse_meta_json(payload: bytes) -> RawBookMetadata | None:
    """Read a ``meta.json`` sidecar. ``None`` when it cannot be understood."""

    document = _load_json(payload)
    if not isinstance(document, dict):
        return None

    titles = document.get("title")
    titles = titles if isinstance(titles, dict) else {}
    tags: list[str] = []
    artist: str | None = None
    for item in _iter_tag_objects(document.get("tags")):
        kind = str(item.get("type", "")).strip().casefold()
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if kind == _TAG_TYPE:
            tags.append(name)
        elif kind == _ARTIST_TYPE and artist is None:
            artist = name.strip()

    language = document.get("language")
    return _non_empty(
        RawBookMetadata(
            preferred_title=_clean(titles.get(_PREFERRED_TITLE_KEY)),
            original_title=_clean(titles.get(_ORIGINAL_TITLE_KEY)),
            author=artist,
            tags=normalize_external_tags(tags),
            language_tag=_language_or_none(language),
        )
    )


def parse_comic_info(payload: bytes) -> RawBookMetadata | None:
    """Read a ``ComicInfo.xml`` sidecar. ``None`` when it cannot be understood."""

    root = _load_xml(payload)
    if root is None:
        return None
    raw_tags = _text(root, "Tags")
    tags = [part.strip() for part in raw_tags.split(",")] if raw_tags else []
    return _non_empty(
        RawBookMetadata(
            preferred_title=_clean(_text(root, "Title")),
            original_title=_clean(_text(root, "AlternateSeries")),
            author=_clean(_text(root, "Writer")),
            tags=normalize_external_tags(tags),
            language_tag=_language_or_none(_text(root, "LanguageISO")),
        )
    )


# ----------------------------------------------------------------------
# Defensive loading
# ----------------------------------------------------------------------


def _load_json(payload: bytes) -> object | None:
    try:
        return json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        # ``RecursionError`` is not a ``ValueError``: a few hundred KB of nested
        # brackets exhausts the parser's stack, and letting that escape would
        # fail an otherwise perfectly good import over a sidecar. Every parser
        # here owes the caller the same contract -- a bad sidecar costs the book
        # its metadata, never its import.
        logger.info(
            "Ignoring unreadable meta.json sidecar",
            extra={
                "event": "archive.metadata.unreadable",
                "category": "archive",
                "status": "skipped",
                "action": "meta.json",
                "error_type": type(exc).__name__,
            },
        )
        return None


def _load_xml(payload: bytes) -> ElementTree.Element | None:
    # ``xml.etree`` refuses *external* entities but does expand internal ones,
    # so a "billion laughs" document parses and multiplies until it exhausts
    # memory. Both that and external entities require a DTD, and a legitimate
    # ComicInfo.xml has none -- so refusing a DOCTYPE outright removes the whole
    # class without pulling in a hardened parser. (``lxml`` is deliberately the
    # EPUB-only extra; see AGENTS.md.)
    if _declares_a_doctype(payload):
        logger.warning(
            "Refusing a ComicInfo.xml that declares a DOCTYPE",
            extra={
                "event": "archive.metadata.rejected",
                "category": "archive",
                "status": "skipped",
                "action": "comicinfo",
            },
        )
        return None
    try:
        # Bytes, not text: an XML document declares its own encoding, and a
        # ComicInfo.xml written as UTF-16 is perfectly legal. Decoding as UTF-8
        # here would silently discard it.
        return ElementTree.fromstring(payload)
    except (UnicodeDecodeError, ValueError, ElementTree.ParseError) as exc:
        logger.info(
            "Ignoring unreadable ComicInfo.xml sidecar",
            extra={
                "event": "archive.metadata.unreadable",
                "category": "archive",
                "status": "skipped",
                "action": "comicinfo",
                "error_type": type(exc).__name__,
            },
        )
        return None


def _declares_a_doctype(payload: bytes) -> bool:
    """Whether the document has a DTD, decided before any parsing happens.

    Two things make this harder than a substring search.

    *Position.* XML permits unlimited whitespace, comments, and processing
    instructions before the DOCTYPE, so a fixed byte window is not a guard: a
    document can push its DTD past any prefix a scanner will read and still
    parse normally. This walks the prolog instead, which self-terminates at the
    root element.

    *Encoding.* Expat accepts UTF-16 with **or without** a BOM -- the XML
    declaration is enough, and it sniffs besides. A guard that decodes one way
    while the parser decodes another is a guard that can be walked around, which
    BOM-less UTF-16 did in both endian orders. Rather than re-implement expat's
    sniffing rules and hope they match, every plausible reading is scanned and
    any DTD in any of them refuses the document. A false positive costs one
    sidecar its metadata; a false negative costs the protection entirely.
    """

    return any(_prolog_declares_a_doctype(text) for text in _readings_of(payload))


def _readings_of(payload: bytes) -> Iterator[str]:
    """Each way a parser might plausibly read these bytes as text.

    BOMs are stripped rather than skipped by the walker: a leading ``\ufeff`` is
    not whitespace, so it would end the prolog scan on its first character.
    """

    yield payload.decode("utf-8", errors="replace").lstrip("\ufeff")
    # A NUL this early is not valid UTF-8 XML, so the bytes are some wide
    # encoding and the ASCII structure is interleaved with padding.
    if b"\x00" in payload[:_ENCODING_SNIFF_BYTES]:
        for encoding in ("utf-16-le", "utf-16-be"):
            yield payload.decode(encoding, errors="replace").lstrip("\ufeff")


def _prolog_declares_a_doctype(text: str) -> bool:
    index = 0
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        if text.startswith("<!--", index):
            index = _skip_until(text, index + 4, "-->")
        elif text.startswith("<?", index):
            index = _skip_until(text, index + 2, "?>")
        else:
            # First real token in the prolog: either the DTD or the root
            # element, and nothing after the root element can introduce one.
            return text[index : index + 9].upper() == "<!DOCTYPE"
        if index < 0:
            # Unterminated comment or PI. The parser will refuse the document
            # anyway, so there is no DTD it could act on.
            return False
    return False


def _skip_until(text: str, start: int, terminator: str) -> int:
    end = text.find(terminator, start)
    return -1 if end < 0 else end + len(terminator)


#: How far in to look for the NUL bytes that mean "not UTF-8". The XML
#: declaration and the first tag are well inside this.
_ENCODING_SNIFF_BYTES = 64


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------


def _iter_tag_objects(value: object):
    if not isinstance(value, (list, tuple)):
        return
    for item in value:
        if isinstance(item, dict):
            yield item


def _text(root: ElementTree.Element, tag: str) -> str:
    element = root.find(tag)
    return (element.text or "").strip() if element is not None else ""


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _language_or_none(value: object) -> str | None:
    """A supported tag, or ``None`` when this source contributed nothing usable.

    Unrecognised input yields ``None`` rather than ``und`` on purpose. ``und``
    is what an absent language already resolves to, so returning it here would
    claim the sidecar answered when it did not -- marking an otherwise-empty
    record as "metadata found", and outranking a *different* sidecar that named
    a language we do understand.
    """

    if not isinstance(value, str):
        return None
    tag = normalize_language_tag(value)
    return tag if tag in SUPPORTED_LANGUAGE_TAGS else None


def _non_empty(metadata: RawBookMetadata) -> RawBookMetadata | None:
    return None if metadata.is_empty() else metadata


def _sidecar_rank(entry: ArchiveMetadataEntry) -> tuple[int, int, str]:
    """Sort key that puts the outermost, shallowest sidecar first.

    Both halves of the depth matter. Nested containers are labelled
    ``parent::child``, so separator count is container nesting -- but a plain
    archive holding both ``ComicInfo.xml`` and ``chapter/ComicInfo.xml`` has one
    container and two very different sidecars, so the directory depth *inside*
    the container counts too. Ranking on the container alone left those two tied
    and let the archive's write order decide which described the book.

    The path is the final tie-break so the result never depends on entry order.
    """

    directory_depth = len(PurePosixPath(entry.path).parts) - 1
    return (entry.container.count("::"), directory_depth, entry.path)


__all__ = [
    "parse_comic_info",
    "parse_meta_json",
    "read_archive_metadata",
    "select_sidecars",
]
