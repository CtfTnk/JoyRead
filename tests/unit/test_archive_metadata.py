"""Reading the metadata sidecars an archive carries.

Two rules are the point of these tests. Sidecars come out of a user-supplied
archive, so a bad one must cost the book its metadata and never its import; and
when ``meta.json`` and ``ComicInfo.xml`` disagree, the winner is decided per
field rather than per document.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from joyread.core.archive.inspection import ArchiveMetadataEntry
from joyread.core.archive.metadata import (
    parse_comic_info,
    parse_meta_json,
    read_archive_metadata,
)
from joyread.core.models.archive_metadata import (
    UNKNOWN_LANGUAGE_TAG,
    MetadataSource,
    RawBookMetadata,
    merge_metadata,
    normalize_external_tags,
    normalize_language_tag,
)
from joyread.core.models.tag import MAX_TAG_NAME_LENGTH


def _meta_json(**overrides) -> bytes:
    document = {
        "title": {"english": "Preferred", "japanese": "原題"},
        "tags": [
            {"type": "tag", "name": "full color"},
            {"type": "artist", "name": "Artist Name"},
        ],
    }
    document.update(overrides)
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def _comic_info(body: str) -> bytes:
    return f"<ComicInfo>{body}</ComicInfo>".encode("utf-8")


def _entry(path: str, data: bytes, container: str = "book.cbz") -> ArchiveMetadataEntry:
    return ArchiveMetadataEntry(
        container=container,
        path=path,
        name=PurePosixPath(path).name,
        data=data,
    )


# ----------------------------------------------------------------------
# meta.json
# ----------------------------------------------------------------------


def test_meta_json_maps_artist_to_author_and_keeps_only_ordinary_tags() -> None:
    """``group``/``parody``/``character`` are different axes of classification.

    Flattening them into the tag list would make an artist and a genre
    indistinguishable in the tag browser.
    """

    payload = _meta_json(
        tags=[
            {"type": "tag", "name": "romance"},
            {"type": "artist", "name": "Someone"},
            {"type": "parody", "name": "a parody"},
            {"type": "group", "name": "a group"},
            {"type": "character", "name": "a character"},
        ]
    )

    result = parse_meta_json(payload)

    assert result is not None
    assert result.author == "Someone"
    assert result.tags == ("romance",)


def test_the_english_title_slot_is_not_a_language_claim() -> None:
    """A Chinese sample stores a Chinese display title under ``english``.

    Treating the key as a language and reaching elsewhere for non-Latin text
    would pick the wrong name.
    """

    payload = _meta_json(title={"english": "中文标题", "japanese": "日本語タイトル"})

    result = parse_meta_json(payload)

    assert result is not None
    assert result.preferred_title == "中文标题"
    assert result.original_title == "日本語タイトル"


def test_meta_json_that_is_not_an_object_is_ignored() -> None:
    assert parse_meta_json(b'["not", "an", "object"]') is None
    assert parse_meta_json(b"not json at all") is None
    assert parse_meta_json(b"") is None


def test_meta_json_survives_wrongly_shaped_fields() -> None:
    """Every field is attacker-shaped, so none of them may raise."""

    payload = json.dumps(
        {"title": "a string, not an object", "tags": {"not": "a list"}, "language": 42}
    ).encode()

    result = parse_meta_json(payload)

    assert result is None


def test_meta_json_tags_that_are_not_strings_are_dropped() -> None:
    payload = _meta_json(
        tags=[
            {"type": "tag", "name": "kept"},
            {"type": "tag", "name": 123},
            {"type": "tag", "name": "   "},
            {"type": "tag"},
            "not an object",
        ]
    )

    result = parse_meta_json(payload)

    assert result is not None
    assert result.tags == ("kept",)


def test_a_utf8_bom_does_not_break_parsing() -> None:
    result = parse_meta_json(b"\xef\xbb\xbf" + _meta_json())

    assert result is not None
    assert result.preferred_title == "Preferred"


# ----------------------------------------------------------------------
# ComicInfo.xml
# ----------------------------------------------------------------------


def test_comic_info_reads_the_comicrack_fields() -> None:
    payload = _comic_info(
        "<Title>A Title</Title><AlternateSeries>Alt</AlternateSeries>"
        "<Writer>A Writer</Writer><Tags>one, two</Tags><LanguageISO>ja</LanguageISO>"
    )

    result = parse_comic_info(payload)

    assert result is not None
    assert result.preferred_title == "A Title"
    assert result.original_title == "Alt"
    assert result.author == "A Writer"
    assert result.tags == ("one", "two")
    assert result.language_tag == "ja"


def test_comic_info_that_is_not_xml_is_ignored() -> None:
    assert parse_comic_info(b"<ComicInfo><unclosed>") is None
    assert parse_comic_info(b"") is None


def test_a_doctype_is_refused_before_the_parser_runs() -> None:
    """``xml.etree`` refuses external entities but *does* expand internal ones,
    so a billion-laughs document parses and multiplies until memory is gone.

    Both that and external entities need a DTD, and a real ComicInfo.xml has
    none -- so the DOCTYPE is refused outright rather than parsed carefully.
    """

    bomb = b"""<?xml version="1.0"?>
    <!DOCTYPE lolz [
     <!ENTITY lol "lol">
     <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
     <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
    ]>
    <ComicInfo><Title>&lol3;</Title></ComicInfo>"""

    assert parse_comic_info(bomb) is None


def test_a_lowercase_doctype_is_refused_too() -> None:
    payload = b'<?xml version="1.0"?><!doctype x [<!ENTITY a "b">]><ComicInfo/>'

    assert parse_comic_info(payload) is None


def test_an_external_entity_reference_yields_nothing() -> None:
    payload = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b"<ComicInfo><Title>&x;</Title></ComicInfo>"
    )

    assert parse_comic_info(payload) is None


def test_empty_comic_info_elements_are_not_treated_as_values() -> None:
    result = parse_comic_info(_comic_info("<Title>  </Title><Writer></Writer>"))

    assert result is None


# ----------------------------------------------------------------------
# Precedence
# ----------------------------------------------------------------------


def test_json_wins_field_by_field_and_xml_fills_the_gaps() -> None:
    """Per field, not per document: the two sources are strong in different
    places, and a whole-document winner discards the other's contribution."""

    json_side = RawBookMetadata(preferred_title="From JSON", author="JSON Artist")
    xml_side = RawBookMetadata(
        preferred_title="From XML", author="XML Writer", language_tag="en"
    )

    merged = merge_metadata(json_side, xml_side)

    assert merged.preferred_title == "From JSON"
    assert merged.author == "JSON Artist"
    assert merged.language_tag == "en"  # only XML had one
    assert merged.source is MetadataSource.MERGED


def test_tags_from_both_sources_are_unioned_with_json_first() -> None:
    """The two categorise differently; keeping only one side loses real
    information, and JSON's curated order should lead."""

    merged = merge_metadata(
        RawBookMetadata(tags=("b", "a")),
        RawBookMetadata(tags=("a", "c")),
    )

    assert merged.tags == ("b", "a", "c")


def test_missing_sources_resolve_to_an_empty_record() -> None:
    merged = merge_metadata(None, None)

    assert merged.is_empty()
    assert merged.source is MetadataSource.NONE
    assert merged.language_tag == UNKNOWN_LANGUAGE_TAG


def test_the_source_names_which_sidecar_answered() -> None:
    assert merge_metadata(RawBookMetadata(author="a"), None).source is MetadataSource.META_JSON
    assert merge_metadata(None, RawBookMetadata(author="a")).source is MetadataSource.COMIC_INFO


def test_a_root_sidecar_beats_one_from_a_nested_chapter() -> None:
    """A chapter archive may describe only itself; the volume-level sidecar
    describes the book."""

    entries = (
        _entry("ComicInfo.xml", _comic_info("<Title>Chapter 1</Title>"), "book.cbz::ch1.cbz"),
        _entry("ComicInfo.xml", _comic_info("<Title>Whole Volume</Title>"), "book.cbz"),
    )

    result = read_archive_metadata(entries)

    assert result.preferred_title == "Whole Volume"


def test_a_corrupt_json_falls_back_to_the_xml() -> None:
    entries = (
        _entry("meta.json", b"{ not json"),
        _entry("ComicInfo.xml", _comic_info("<Title>Fallback</Title>")),
    )

    result = read_archive_metadata(entries)

    assert result.preferred_title == "Fallback"
    assert result.source is MetadataSource.COMIC_INFO


def test_both_corrupt_yields_nothing_rather_than_raising() -> None:
    entries = (
        _entry("meta.json", b"{ not json"),
        _entry("ComicInfo.xml", b"<unclosed"),
    )

    result = read_archive_metadata(entries)

    assert result.is_empty()
    assert result.language_tag == UNKNOWN_LANGUAGE_TAG


# ----------------------------------------------------------------------
# Normalisation
# ----------------------------------------------------------------------


def test_language_maps_onto_the_four_tags_the_library_stores() -> None:
    assert normalize_language_tag("English") == "en"
    assert normalize_language_tag("EN-US") == "en"
    assert normalize_language_tag("jpn") == "ja"
    assert normalize_language_tag("zh_Hant") == "zh"


def test_an_unrecognised_language_becomes_und_rather_than_a_guess() -> None:
    assert normalize_language_tag("Klingon") == UNKNOWN_LANGUAGE_TAG
    assert normalize_language_tag("") == UNKNOWN_LANGUAGE_TAG
    assert normalize_language_tag(None) == UNKNOWN_LANGUAGE_TAG
    assert normalize_language_tag(42) == UNKNOWN_LANGUAGE_TAG


def test_tags_are_deduplicated_case_insensitively_in_order() -> None:
    assert normalize_external_tags(["Full Color", "full color", "Romance"]) == (
        "Full Color",
        "Romance",
    )


def test_an_overlong_tag_is_truncated_rather_than_dropped() -> None:
    """It still carries most of its meaning; dropping loses it entirely."""

    result = normalize_external_tags(["x" * (MAX_TAG_NAME_LENGTH + 20)])

    assert len(result) == 1
    assert len(result[0]) == MAX_TAG_NAME_LENGTH


def test_empty_and_non_string_tags_are_dropped() -> None:
    assert normalize_external_tags(["  ", "", None, 5, "kept"]) == ("kept",)
    assert normalize_external_tags("not a list") == ()
    assert normalize_external_tags(None) == ()


def test_tags_are_nfc_normalised_so_equivalent_spellings_collapse() -> None:
    composed = "é"
    decomposed = "é"
    assert composed != decomposed

    assert normalize_external_tags([composed, decomposed]) == (composed,)


def test_translations_are_not_treated_as_the_same_tag() -> None:
    """Explicitly out of scope for v1: equivalence needs a curated mapping or a
    user-driven merge, not a guess at import time."""

    result = normalize_external_tags(["full color", "full-color", "全彩"])

    assert len(result) == 3


def test_an_unintelligible_language_lets_the_other_sidecar_answer() -> None:
    """``und`` is what an absent language already resolves to, so a source that
    yields it has not really answered -- and must not outrank one that did."""

    entries = (
        _entry("meta.json", _meta_json(language="Klingon")),
        _entry("ComicInfo.xml", _comic_info("<LanguageISO>ja</LanguageISO>")),
    )

    result = read_archive_metadata(entries)

    assert result.language_tag == "ja"


# ----------------------------------------------------------------------
# Prolog handling: a DOCTYPE cannot be hidden, and encoding is the
# document's to declare
# ----------------------------------------------------------------------


def test_a_doctype_hidden_behind_a_huge_comment_is_still_refused() -> None:
    """XML allows unlimited comments before the DTD, so a fixed scan window is
    not a guard -- a document can push its DOCTYPE past any prefix and still
    parse normally, entity expansion included."""

    padding = b"<!-- " + b"x" * 8192 + b" -->"
    bomb = (
        b'<?xml version="1.0"?>'
        + padding
        + b"""
        <!DOCTYPE lolz [
         <!ENTITY lol "LOLOLOL">
         <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
        ]>
        <ComicInfo><Title>&lol2;</Title></ComicInfo>"""
    )

    assert parse_comic_info(bomb) is None


def test_a_doctype_behind_comments_and_processing_instructions_is_refused() -> None:
    payload = (
        b'<?xml version="1.0"?><!-- one --><?xml-stylesheet href="x"?><!-- two -->'
        b'<!DOCTYPE r [<!ENTITY a "b">]><ComicInfo><Title>T</Title></ComicInfo>'
    )

    assert parse_comic_info(payload) is None


def test_comments_before_the_root_element_do_not_block_a_clean_document() -> None:
    """The prolog walk must skip what it is allowed to skip, or every sidecar
    with a generator comment would lose its metadata."""

    payload = (
        b'<?xml version="1.0"?><!-- written by some packer -->'
        b"<ComicInfo><Title>Kept</Title></ComicInfo>"
    )

    result = parse_comic_info(payload)

    assert result is not None
    assert result.preferred_title == "Kept"


def test_an_unterminated_comment_is_not_treated_as_a_clean_document() -> None:
    assert parse_comic_info(b"<!-- never closed <ComicInfo><Title>T</Title></ComicInfo>") is None


def test_a_utf16_comic_info_is_read_rather_than_discarded() -> None:
    """XML declares its own encoding and UTF-16 is legal, so decoding the bytes
    as UTF-8 before parsing silently drops a valid sidecar."""

    document = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        "<ComicInfo><Title>日本語タイトル</Title><Writer>作者</Writer></ComicInfo>"
    )

    result = parse_comic_info(document.encode("utf-16"))

    assert result is not None
    assert result.preferred_title == "日本語タイトル"
    assert result.author == "作者"


def test_a_doctype_in_a_utf16_document_is_refused_too() -> None:
    """The guard runs before the parser, so it has to understand the same
    encodings the parser does -- otherwise UTF-16 is a way around it."""

    document = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE r [<!ENTITY a "b">]><ComicInfo><Title>T</Title></ComicInfo>'
    )

    assert parse_comic_info(document.encode("utf-16")) is None


# ----------------------------------------------------------------------
# Which sidecar describes the book
# ----------------------------------------------------------------------


def test_a_chapter_sidecar_never_outranks_the_book_level_one_in_the_same_archive() -> None:
    """Both live in one container, so container nesting cannot separate them.

    Ranking on the container alone left them tied and let the archive's write
    order decide -- here the chapter is written first, which is exactly the case
    that silently produced the wrong title.
    """

    # The chapter directory is named so it sorts *before* the root sidecar's
    # own name: if depth stopped deciding, the alphabetical tie-break would
    # quietly pick the chapter and the test would still look like it passed.
    entries = (
        _entry("Arc-01/ComicInfo.xml", _comic_info("<Title>Chapter 1</Title>")),
        _entry("ComicInfo.xml", _comic_info("<Title>Whole Volume</Title>")),
    )

    assert read_archive_metadata(entries).preferred_title == "Whole Volume"


def test_sidecar_precedence_does_not_depend_on_entry_order() -> None:
    entries = (
        _entry("A/deep/ComicInfo.xml", _comic_info("<Title>Deep</Title>")),
        _entry("ComicInfo.xml", _comic_info("<Title>Root</Title>")),
        _entry("B/ComicInfo.xml", _comic_info("<Title>Mid</Title>")),
    )

    for ordering in (entries, tuple(reversed(entries)), (entries[1], entries[2], entries[0])):
        assert read_archive_metadata(ordering).preferred_title == "Root"


def test_two_sidecars_at_equal_depth_resolve_by_path_not_by_write_order() -> None:
    entries = (
        _entry("z/ComicInfo.xml", _comic_info("<Title>Z</Title>")),
        _entry("a/ComicInfo.xml", _comic_info("<Title>A</Title>")),
    )

    assert read_archive_metadata(entries).preferred_title == "A"
    assert read_archive_metadata(tuple(reversed(entries))).preferred_title == "A"


def test_a_nested_sidecar_of_the_other_kind_still_fills_a_gap() -> None:
    """Shallowest-wins is decided *within* a kind. A root meta.json that names
    no language has not answered, so a chapter's ComicInfo may -- dropping it
    would lose real information to no benefit, and the user can still edit it.
    """

    entries = (
        _entry("meta.json", _meta_json()),
        _entry("ch01/ComicInfo.xml", _comic_info("<LanguageISO>ja</LanguageISO>")),
    )

    result = read_archive_metadata(entries)

    assert result.preferred_title == "Preferred"  # from the root JSON
    assert result.language_tag == "ja"  # from the nested XML


def test_a_doctype_in_bom_less_utf16_is_refused_in_both_endian_orders() -> None:
    """Expat accepts UTF-16 without a BOM -- the declaration is enough.

    A guard that decodes one way while the parser decodes another is a guard
    that can be walked around, and this is exactly how: with no BOM the guard
    read the bytes as UTF-8, saw no DOCTYPE, and handed a live entity bomb to a
    parser that read them as UTF-16 and expanded it.
    """

    bomb = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE lolz [<!ENTITY lol "LOLOLOL">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
        "<ComicInfo><Title>&lol2;</Title></ComicInfo>"
    )

    for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
        assert parse_comic_info(bomb.encode(encoding)) is None, encoding


def test_clean_utf16_still_parses_in_every_byte_order() -> None:
    """The guard scans several readings of the bytes, so it must not start
    refusing documents that merely *look* unusual to one of them."""

    document = '<?xml version="1.0" encoding="UTF-16"?><ComicInfo><Title>日本語</Title></ComicInfo>'

    for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
        result = parse_comic_info(document.encode(encoding))
        assert result is not None, encoding
        assert result.preferred_title == "日本語"


def test_a_recursively_nested_json_sidecar_loses_metadata_not_the_import() -> None:
    """``RecursionError`` is not a ``ValueError``.

    A couple of hundred KB of nested brackets exhausts the JSON parser's stack,
    and that escaping would fail an otherwise perfectly good import over a file
    the book does not need.
    """

    payload = (b"[" * 100_000) + (b"]" * 100_000)

    assert parse_meta_json(payload) is None
