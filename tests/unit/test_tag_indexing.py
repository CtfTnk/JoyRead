"""Tests for A-Z tag indexing, including CJK romanization."""

from __future__ import annotations

from threading import Thread

import pytest

from joyread.core import tag_indexing
from joyread.core.models.tag import Tag
from joyread.core.tag_indexing import (
    BUCKET_ORDER,
    OTHER_BUCKET,
    bucket_of,
    group_tags,
    matches_query,
    reading_of,
    warm_romanizers,
)


def _tag(name: str) -> Tag:
    return Tag(name.lower().replace(" ", "-") or "blank", name)


@pytest.mark.parametrize(
    ("name", "expected"),
    (
        ("Action", "A"),
        ("zombies", "Z"),
        ("Sci-Fi", "S"),
        # Accents fold onto the plain letter rather than falling off the rail.
        ("Éclair", "E"),
        ("café", "C"),
    ),
)
def test_latin_names_index_on_their_own_initial(name: str, expected: str) -> None:
    assert bucket_of(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    (
        ("ホラー", "H"),  # horaa
        ("日常", "N"),  # nichijou
        ("少年漫画", "S"),  # shounenmanga
        ("恋愛", "R"),  # ren'ai
        ("冒険", "B"),  # bouken
        ("百合", "Y"),  # yuri
    ),
)
def test_japanese_names_index_by_romanized_reading(name: str, expected: str) -> None:
    """A kana/kanji tag lands on the same rail as Latin tags, not a
    separate script bucket the A-Z rail cannot reach."""

    assert bucket_of(name) == expected


@pytest.mark.parametrize(
    ("name", "japanese", "chinese"),
    (
        ("恋愛", "R", "L"),  # ren'ai vs lian'ai
        ("百合", "Y", "B"),  # yuri vs baihe
        ("冒険", "B", "M"),  # bouken vs maoxian
    ),
)
def test_han_only_names_follow_the_requested_language(name: str, japanese: str, chinese: str) -> None:
    """Han characters are shared, so the same tag reads differently in each
    language. Kana-bearing names are unambiguous and ignore the setting."""

    assert bucket_of(name) == japanese
    assert bucket_of(name, han_language="zh") == chinese


def test_kana_names_ignore_the_han_language_setting() -> None:
    assert bucket_of("ホラー") == bucket_of("ホラー", han_language="zh") == "H"


@pytest.mark.parametrize("name", ("", "   ", "1990s", "!!!", "???"))
def test_names_without_a_latin_initial_fall_into_the_other_bucket(name: str) -> None:
    assert bucket_of(name) == OTHER_BUCKET


def test_reading_is_empty_for_names_that_need_no_romanization() -> None:
    assert reading_of("Action") == ""
    assert reading_of("ホラー") != ""


def test_group_tags_orders_buckets_and_drops_empty_ones() -> None:
    groups = group_tags([_tag("Zombies"), _tag("Action"), _tag("Adventure"), _tag("ホラー")])

    assert [letter for letter, _ in groups] == ["A", "H", "Z"]
    assert [tag.name for tag in dict(groups)["A"]] == ["Action", "Adventure"]
    # Only buckets that actually hold tags -- the rail is built from this, and
    # a strip of letters leading nowhere is worse than a short one.
    assert OTHER_BUCKET not in dict(groups)


def test_group_tags_puts_the_other_bucket_last() -> None:
    groups = group_tags([_tag("1990s"), _tag("Action")])

    assert [letter for letter, _ in groups] == ["A", OTHER_BUCKET]
    assert BUCKET_ORDER[-1] == OTHER_BUCKET


def test_group_tags_filters_by_query() -> None:
    groups = group_tags([_tag("Action"), _tag("Adventure"), _tag("Zombies")], query="adv")

    assert [letter for letter, _ in groups] == ["A"]
    assert [tag.name for tag in dict(groups)["A"]] == ["Adventure"]


def test_group_tags_with_a_query_matching_nothing_is_empty() -> None:
    assert group_tags([_tag("Action")], query="zzzz") == []


def test_query_matches_the_romanized_reading_as_well_as_the_display_name() -> None:
    """The reading is already computed for bucketing, so searching it lets a
    user type ``horror`` to reach ``ホラー``."""

    assert matches_query("ホラー", "hora")
    assert matches_query("ホラー", "ホラ")
    assert not matches_query("ホラー", "zombie")


def test_empty_query_matches_everything() -> None:
    assert matches_query("Action", "")
    assert matches_query("ホラー", "   ")


def test_warming_builds_both_converters() -> None:
    """Startup warms these off the UI thread. Left cold, the first Japanese
    or Chinese tag pays ~110ms to load dictionaries, and it pays it inside
    the dialog the user just asked for."""

    warm_romanizers()

    assert tag_indexing._romanizer._kakasi is not None
    assert tag_indexing._romanizer._pypinyin is not None


def test_warming_is_idempotent_and_safe_from_many_threads() -> None:
    """The warm-up races the UI thread by design: the user can open a tag
    surface before the background thread finishes. Concurrent callers must
    not build competing converters or observe a half-built one."""

    warm_romanizers()
    converter = tag_indexing._romanizer._kakasi
    results: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            warm_romanizers()
            results.append(bucket_of("日常"))
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert results == ["N"] * 8
    # Same object throughout: warming again must not rebuild the dictionary.
    assert tag_indexing._romanizer._kakasi is converter
