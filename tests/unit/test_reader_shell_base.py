"""The chrome both reader shells inherit.

These assertions used to be duplicated across the two shells and pinned
together by a parity test that imported the novel shell into the manga suite
just to prove the copies had not drifted. With one implementation there is one
thing to test, and it tests here without reaching into the novel package --
so this runs whether or not the novel reader is installed.
"""

from __future__ import annotations

from types import SimpleNamespace

from joyread.infrastructure.i18n import locale_service
from joyread.ui.views.reader_shell_base import ReaderShellBase


def test_bookmark_rename_dialog_uses_active_locale() -> None:
    calls: list[dict[str, object]] = []

    class FakeDialog:
        def show_input(self, *args, **kwargs) -> None:
            calls.append({"args": args, "kwargs": kwargs})

    receiver = SimpleNamespace(
        dialog_overlay=FakeDialog(),
        viewmodel=SimpleNamespace(rename_bookmark=lambda *_args: None),
    )

    try:
        locale_service.load_language("Chinese")
        ReaderShellBase._show_rename_bookmark_dialog(receiver, "bookmark-1", "旧书签")

        assert [call["args"][:2] for call in calls] == [("重命名书签", "书签名称")]
        assert [call["kwargs"]["confirm_text"] for call in calls] == ["重命名"]
        assert [call["kwargs"]["cancel_text"] for call in calls] == ["取消"]
        assert calls[0]["kwargs"]["validator"]("   ") == "书签名称不能为空。"
    finally:
        locale_service.load_language("English")


def test_renaming_a_bookmark_passes_the_uuid_the_dialog_was_opened_for() -> None:
    """Each dialog stays bound to its own bookmark.

    The uuid lives in the callback's closure, so opening a second dialog
    cannot retarget the first. A refactor that hung the pending uuid off
    ``self`` instead -- the obvious way to "simplify" this lambda -- would
    rename the wrong bookmark here.

    (The ``bookmark_uuid=bookmark_uuid`` default argument in the source is
    belt-and-braces rather than load-bearing: every call gets its own scope,
    so a plain closure would bind correctly too. Verified by mutation.)
    """

    renamed: list[tuple[str, str]] = []

    class FakeDialog:
        def __init__(self) -> None:
            self.confirms: list[object] = []

        def show_input(self, *_args, **kwargs) -> None:
            self.confirms.append(kwargs["on_confirm"])

    dialog = FakeDialog()
    receiver = SimpleNamespace(
        dialog_overlay=dialog,
        viewmodel=SimpleNamespace(rename_bookmark=lambda uuid, name: renamed.append((uuid, name))),
    )

    ReaderShellBase._show_rename_bookmark_dialog(receiver, "first", "one")
    ReaderShellBase._show_rename_bookmark_dialog(receiver, "second", "two")
    # Confirm the *first* dialog after the second one was opened.
    dialog.confirms[0]("renamed one")

    assert renamed == [("first", "renamed one")]


def test_seeking_from_the_topic_panel_navigates_then_dismisses() -> None:
    """Order matters: the panel hides only after the seek, so its own
    selection handling finishes first."""

    events: list[str] = []
    receiver = SimpleNamespace(
        viewmodel=SimpleNamespace(seek=lambda index: events.append(f"seek:{index}")),
        _hide_topic_panel=lambda: events.append("hide"),
    )

    ReaderShellBase._seek_from_topic_panel(receiver, 7)

    assert events == ["seek:7", "hide"]


def test_the_hide_timer_does_not_run_while_a_dialog_is_open() -> None:
    """Auto-hide must not steal the chrome out from under a modal prompt."""

    restarts: list[bool] = []
    auto_hide = SimpleNamespace(restart=lambda: restarts.append(True))

    with_dialog = SimpleNamespace(
        dialog_overlay=SimpleNamespace(isVisible=lambda: True), auto_hide=auto_hide
    )
    ReaderShellBase._start_hide_timer_if_allowed(with_dialog)
    assert restarts == []

    without_dialog = SimpleNamespace(
        dialog_overlay=SimpleNamespace(isVisible=lambda: False), auto_hide=auto_hide
    )
    ReaderShellBase._start_hide_timer_if_allowed(without_dialog)
    assert restarts == [True]
