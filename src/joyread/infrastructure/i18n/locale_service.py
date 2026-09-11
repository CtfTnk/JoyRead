"""Locale / i18n service for JoyRead.

Translations are stored as JSON files.  The service resolves them in priority
order: user-supplied override (in the settings Config/locales directory) first,
then the bundled locale that ships with the app.

Usage::

    from joyread.infrastructure.i18n import locale_service

    # Called once at startup from create_app_context():
    locale_service.init(bundled_dir, user_dir, language="English")

    # Called whenever the user changes the language setting:
    locale_service.load_language("Chinese")

    # Called from any UI module that needs a translated string:
    from joyread.infrastructure.i18n.locale_service import t
    label = t("menu.read")
    msg = t("dialog.delete_book_msg", title="My Book")

To add a new language, create a JSON file whose name matches the locale code
(e.g. ``ko.json`` for Korean) in the locales directory and register its
``LanguageOption`` metadata below.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LanguageOption:
    """Supported language metadata.

    ``settings_value`` is the canonical value stored in settings.json.
    ``native_name`` is the fixed label shown in the language dropdown.
    """

    settings_value: str
    code: str
    native_name: str


LANGUAGE_OPTIONS: tuple[LanguageOption, ...] = (
    LanguageOption("English", "en", "English"),
    LanguageOption("Chinese", "zh", "中文"),
    LanguageOption("Japanese", "ja", "日本語"),
)

# Canonical values stored in settings.json.
LANGUAGE_VALUES: tuple[str, ...] = ("System",) + tuple(option.settings_value for option in LANGUAGE_OPTIONS)

# Native display labels shown in the Language dropdown.
LANGUAGE_DISPLAY_OPTIONS: tuple[str, ...] = tuple(option.native_name for option in LANGUAGE_OPTIONS)

# Maps the canonical option stored in settings.json to the locale file name
# (without extension). Add entries here when shipping new built-in languages.
LANGUAGE_TO_CODE: dict[str, str] = {option.settings_value: option.code for option in LANGUAGE_OPTIONS}
LANGUAGE_CODE_TO_VALUE: dict[str, str] = {option.code: option.settings_value for option in LANGUAGE_OPTIONS}


class LocaleService:
    """Loads and vends translated strings for a single active language."""

    def __init__(self, bundled_dir: Path, user_dir: Path | None = None) -> None:
        self._bundled_dir = bundled_dir
        self._user_dir = user_dir
        self._language_code = "en"
        self._translations: dict[str, str] = {}
        self._fallback: dict[str, str] = {}
        # Always load English as the fallback so missing keys degrade
        # gracefully when a translation file is incomplete.
        self._load_into(self._fallback, "en", include_user=False)

    def load(self, language: str) -> None:
        """Switch to *language* (a canonical value from ``LANGUAGE_VALUES``)."""
        lang_code = language_code_for_value(language)
        new_translations: dict[str, str] = {}
        self._load_into(new_translations, lang_code)
        self._language_code = lang_code
        self._translations = new_translations
        logger.info("Locale switched to language=%s code=%s keys=%d", language, lang_code, len(new_translations))

    @property
    def language_code(self) -> str:
        """Locale code currently used for translations and locale-sensitive UI."""

        return self._language_code

    def t(self, key: str, **kwargs: object) -> str:
        """Return the translated string for *key*, falling back to English then the key itself."""
        text = self._translations.get(key) or self._fallback.get(key) or key
        if "{app_name}" in text:
            kwargs.setdefault("app_name", self._translations.get("app.name") or self._fallback.get("app.name") or "JoyRead")
        if kwargs:
            try:
                text = text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                pass
        return text

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_into(self, target: dict[str, str], lang_code: str, *, include_user: bool = True) -> None:
        """Merge valid string entries; an override never masks bundled siblings."""
        directories = [self._bundled_dir]
        if include_user and self._user_dir is not None:
            directories.append(self._user_dir)
        for directory in directories:
            path = directory / f"{lang_code}.json"
            if not path.is_file():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("Locale root must be an object")
                entries: dict[str, str] = {}
                _flatten(raw, "", entries)
                target.update({key: value for key, value in entries.items() if value})
            except (OSError, ValueError) as exc:
                logger.warning("Failed to load locale file %s: %s", path, exc)


def _flatten(obj: object, prefix: str, out: dict[str, str]) -> None:
    """Recursively flatten a nested JSON object into dot-separated keys."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            child_key = f"{prefix}.{k}" if prefix else k
            _flatten(v, child_key, out)
    elif isinstance(obj, str):
        out[prefix] = obj
    # Non-string leaves (numbers, booleans, null) are ignored.


# ---------------------------------------------------------------------------
# Module-level API — use these from UI code
# ---------------------------------------------------------------------------

_service: LocaleService | None = None


def default_bundled_locale_dir() -> Path:
    """Return the bundled locale directory for source/installed package usage."""
    return Path(__file__).resolve().parents[2] / "resources" / "locales"


def resolve_system_language(ui_languages: list[str] | tuple[str, ...], fallback_locale: str = "") -> str:
    """Resolve UI preferences in order, using the locale only for an empty list."""
    for tag in ui_languages or (fallback_locale,):
        code = tag.strip().replace("_", "-").split("-", 1)[0].split(".", 1)[0].split("@", 1)[0].lower()
        if code in LANGUAGE_CODE_TO_VALUE:
            return code
    return "en"


def language_code_for_value(language: str) -> str:
    """Resolve a preference without changing the persisted selection."""
    if language == "System":
        from PySide6.QtCore import QLocale
        system = QLocale.system()
        return resolve_system_language(system.uiLanguages(), system.name())
    return LANGUAGE_TO_CODE.get(language, "en")


def language_display_options() -> tuple[str, ...]:
    return (t("settings.language_system"), *LANGUAGE_DISPLAY_OPTIONS)


def app_display_name() -> str:
    return t("app.name")


def language_display_name(language: str) -> str:
    """Map a canonical settings language value to its native display label."""
    if language == "System":
        return t("settings.language_system")
    for option in LANGUAGE_OPTIONS:
        if option.settings_value == language:
            return option.native_name
    return LANGUAGE_OPTIONS[0].native_name


def language_value_from_display(display_name: str) -> str:
    """Map a native display label back to the canonical settings value."""
    if display_name == t("settings.language_system"):
        return "System"
    for option in LANGUAGE_OPTIONS:
        if option.native_name == display_name:
            return option.settings_value
    return LANGUAGE_OPTIONS[0].settings_value


def book_language_display_name(language_code: str | None, fallback_name: str | None = None) -> str:
    """Return a book-language label translated for the active app locale.

    Book records store ISO-like language tags plus repository display names.
    For JoyRead's built-in languages, prefer the active locale label so the
    detail panel follows the selected app language instead of the database's
    English seed values.
    """
    code = (language_code or "").strip().lower()
    if code in LANGUAGE_CODE_TO_VALUE or code == "und":
        return t(f"language_name.{code}")
    return (fallback_name or language_code or t("language_name.und")).strip()


def _get_service() -> LocaleService:
    """Return the module service, lazily initialising English fallback if needed."""
    global _service
    if _service is None:
        _service = LocaleService(default_bundled_locale_dir())
        _service.load("English")
    return _service


def init(bundled_dir: Path, user_dir: Path | None, language: str) -> None:
    """Initialise the module-level locale service.

    Called once from ``create_app_context()`` before any UI is constructed.
    """
    global _service
    _service = LocaleService(bundled_dir, user_dir)
    _service.load(language)
    _notify_language_changed()


def load_language(language: str) -> None:
    """Switch the active language."""
    _get_service().load(language)
    _notify_language_changed()


def active_language_code() -> str:
    """Return the active locale file code (for example ``en`` or ``zh``)."""

    return _get_service().language_code


def t(key: str, **kwargs: object) -> str:
    """Translate *key* to the current language.

    Returns the key itself if absent from both the active and fallback
    translation tables.
    """
    return TranslatedText(key, kwargs)


class TranslatedText(str):
    """A string with presentation provenance, so open controls can retranslate.

    Plain strings (user content, paths, technical details) remain plain. Never
    recover keys by matching rendered text: user content can equal a UI label.
    """

    def __new__(cls, key: str, parameters: dict[str, object] | None = None):
        parameters = parameters or {}
        rendered = {name: value.resolve() if isinstance(value, TranslatedText) else value
                    for name, value in parameters.items()}
        instance = super().__new__(cls, _get_service().t(key, **rendered))
        instance.key = key
        instance.parameters = parameters
        return instance

    def resolve(self) -> str:
        return TranslatedText(self.key, self.parameters)


def _notify_language_changed() -> None:
    # The Qt adapter is optional for headless service consumers. Import lazily
    # to keep locale loading independent of constructing any windows.
    from joyread.infrastructure.i18n.qt_locale import refresh_application_language
    refresh_application_language()


def join_translated(parts: list[str] | tuple[str, ...], separator: str = "\n") -> str:
    """Compose UI text while preserving translation provenance of each part."""
    return JoinedText(parts, separator)


class JoinedText(TranslatedText):
    def __new__(cls, parts: list[str] | tuple[str, ...], separator: str):
        instance = str.__new__(cls, separator.join(
            value.resolve() if isinstance(value, TranslatedText) else value for value in parts
        ))
        instance.parts = tuple(parts)
        instance.separator = separator
        return instance

    def resolve(self) -> str:
        return JoinedText(self.parts, self.separator)
