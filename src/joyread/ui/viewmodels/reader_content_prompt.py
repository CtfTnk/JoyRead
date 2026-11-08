"""Localized wording for the Reader's "mostly unsupported files" question.

Kept out of ``ReaderViewModel`` so the counting rule (``core.reader.content_check``)
and its wording can each be read on their own. Every count comes from the
profile; no entry name ever reaches the text except the archive's own.
"""

from __future__ import annotations

from joyread.core.reader.content_check import ReaderContentWarning, UnshownGroup, UnshownKind
from joyread.infrastructure.i18n.locale_service import t
from joyread.ui.viewmodels.reader_items import ReaderContentPrompt


_GROUP_KEYS = {
    UnshownKind.LOCKED_ARCHIVES: "reader.content_warning_locked",
    UnshownKind.SKIPPED_ARCHIVES: "reader.content_warning_skipped",
    UnshownKind.OUT_OF_REACH: "reader.content_warning_deep",
    UnshownKind.OTHER_FILES: "reader.content_warning_more",
}


def build_content_prompt(warning: ReaderContentWarning, display_name: str) -> ReaderContentPrompt:
    rest = t("reader.content_warning_separator").join(_group_text(group) for group in warning.groups)
    return ReaderContentPrompt(
        title=t("reader.content_warning_title"),
        message=t(
            "reader.content_warning_body",
            pages=warning.pages,
            total=warning.countable,
            name=display_name,
            percent=warning.shown_percent,
            rest=rest,
        ),
    )


def _group_text(group: UnshownGroup) -> str:
    number = "one" if group.count == 1 else "many"
    if group.kind is UnshownKind.FILE_TYPE:
        if not group.file_type:
            return t(f"reader.content_warning_untyped_{number}", count=group.count)
        return t(f"reader.content_warning_type_{number}", count=group.count, type=group.file_type)
    return t(f"{_GROUP_KEYS[group.kind]}_{number}", count=group.count)
