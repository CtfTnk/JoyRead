"""Cross-shell parity: the two reader shells must behave identically where
they duplicate each other.

This lives here rather than beside the manga reader tests because asserting
on both shells means importing the novel one, which pulls in the EPUB parser
and ``lxml``. Phase 5 of the decoupling hoists the duplicated methods into a
shared base, at which point there is one implementation to test and this file
goes away.
"""

from __future__ import annotations

from types import SimpleNamespace

from joyread.infrastructure.i18n import locale_service
from joyread.novel.ui.novel_reader_shell import NovelReaderShellWidget
from joyread.ui.views.reader_shell import ReaderShellWidget


def test_bookmark_rename_dialog_uses_active_locale() -> None:
    calls: list[dict[str, object]] = []

    class FakeDialog:
        def show_input(self, *args, **kwargs) -> None:
            calls.append({"args": args, "kwargs": kwargs})

    receiver = SimpleNamespace(dialog_overlay=FakeDialog(), viewmodel=SimpleNamespace(rename_bookmark=lambda *_args: None))

    try:
        locale_service.load_language("Chinese")
        ReaderShellWidget._show_rename_bookmark_dialog(receiver, "bookmark-1", "旧书签")
        NovelReaderShellWidget._show_rename_bookmark_dialog(receiver, "bookmark-2", "旧书签")

        assert [call["args"][:2] for call in calls] == [
            ("重命名书签", "书签名称"),
            ("重命名书签", "书签名称"),
        ]
        assert [call["kwargs"]["confirm_text"] for call in calls] == ["重命名", "重命名"]
        assert [call["kwargs"]["cancel_text"] for call in calls] == ["取消", "取消"]
        assert calls[0]["kwargs"]["validator"]("   ") == "书签名称不能为空。"
    finally:
        locale_service.load_language("English")
