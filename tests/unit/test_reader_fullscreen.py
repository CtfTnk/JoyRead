"""Reader full-screen transitions across native window state conventions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QWidget

from joyread.ui.widgets import reader_fullscreen


class _TrackingWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.transitions: list[str] = []

    def showNormal(self) -> None:  # noqa: N802 - Qt API
        self.transitions.append("normal")
        super().showNormal()

    def showFullScreen(self) -> None:  # noqa: N802 - Qt API
        self.transitions.append("fullscreen")
        super().showFullScreen()


@pytest.mark.parametrize(
    ("platform", "start_maximized", "expected"),
    (
        ("windows", True, ["normal", "fullscreen"]),
        ("windows", False, ["fullscreen"]),
        ("offscreen", True, ["fullscreen"]),
    ),
)
def test_reader_fullscreen_normalizes_only_windows_maximized_transition(
    qtbot, monkeypatch, platform: str, start_maximized: bool, expected: list[str]
) -> None:
    window = _TrackingWindow()
    qtbot.addWidget(window)
    if start_maximized:
        window.showMaximized()
    else:
        window.show()
    monkeypatch.setattr(
        reader_fullscreen,
        "QGuiApplication",
        SimpleNamespace(platformName=lambda: platform),
    )

    reader_fullscreen.enter_reader_fullscreen(window)
    assert window.transitions == expected
    assert window.isFullScreen()
    reader_fullscreen.enter_reader_fullscreen(window)
    assert window.transitions == expected  # F again is harmless while full screen.
    assert reader_fullscreen.leave_reader_fullscreen(window)
    assert window.isMaximized() is start_maximized
