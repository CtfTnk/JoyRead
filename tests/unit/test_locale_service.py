import json

import pytest

from joyread.infrastructure.i18n import locale_service
from joyread.infrastructure.resources.resource_loader import ResourceLoader


@pytest.fixture(autouse=True)
def reset_locale() -> None:
    locale_service._service = None  # type: ignore[attr-defined]
    yield
    locale_service.init(ResourceLoader().locale_dir(), None, "English")


def test_lazy_translation_uses_english_fallback_before_app_context() -> None:
    locale_service._service = None  # type: ignore[attr-defined]

    assert locale_service.t("toolbar.search_placeholder") == "Search books..."


def test_load_language_switches_to_bundled_chinese_and_japanese() -> None:
    locale_service.init(ResourceLoader().locale_dir(), None, "Chinese")
    assert locale_service.t("sidebar.all") == "全部"

    locale_service.load_language("Japanese")
    assert locale_service.t("sidebar.all") == "すべて"


def test_missing_active_key_falls_back_to_english(tmp_path) -> None:
    user_locales = tmp_path / "locales"
    user_locales.mkdir()
    (user_locales / "zh.json").write_text(
        json.dumps({"menu": {"read": "OVERRIDE READ"}}),
        encoding="utf-8",
    )

    locale_service.init(ResourceLoader().locale_dir(), user_locales, "Chinese")

    assert locale_service.t("menu.read") == "OVERRIDE READ"
    assert locale_service.t("sidebar.all") == "All"


def test_user_locale_override_wins_over_bundled_locale(tmp_path) -> None:
    user_locales = tmp_path / "locales"
    user_locales.mkdir()
    (user_locales / "ja.json").write_text(
        json.dumps({"toolbar": {"search_submit": "CUSTOM SEARCH"}}),
        encoding="utf-8",
    )

    locale_service.init(ResourceLoader().locale_dir(), user_locales, "Japanese")

    assert locale_service.t("toolbar.search_submit") == "CUSTOM SEARCH"


def test_interpolation_and_bad_interpolation_are_safe() -> None:
    locale_service.init(ResourceLoader().locale_dir(), None, "English")

    assert locale_service.t("dialog.unknown_language_code", code="xx") == "Unknown language code: xx"
    assert locale_service.t("dialog.unknown_language_code", wrong="xx") == "Unknown language code: {code}"


def test_language_metadata_maps_canonical_values_to_native_display_names() -> None:
    assert locale_service.LANGUAGE_VALUES == ("English", "Chinese", "Japanese")
    assert locale_service.LANGUAGE_DISPLAY_OPTIONS == ("English", "中文", "日本語")
    assert locale_service.language_display_name("Chinese") == "中文"
    assert locale_service.language_value_from_display("日本語") == "Japanese"
    assert locale_service.language_code_for_value("Japanese") == "ja"


def test_book_language_display_name_follows_active_app_locale() -> None:
    locale_service.init(ResourceLoader().locale_dir(), None, "Chinese")

    assert locale_service.book_language_display_name("en", "English") == "英语"
    assert locale_service.book_language_display_name("ja", "Japanese") == "日语"
    assert locale_service.book_language_display_name("und", "Unknown") == "未知"
    assert locale_service.book_language_display_name("fr", "French") == "French"
