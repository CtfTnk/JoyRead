"""Right-side 'Custom' settings panel for the novel reader skeleton.

Figma node 616:5112 ('Custom' sidebar in Reading Page - Novel). Mirrors
``ReaderSettingsPanel``'s positioning but exposes novel-specific rows
(Enable Custom switch, Font Size spin). Persistence is deferred to the
EPUB engine work; the skeleton only wires the signals so the chrome
behaves end-to-end.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import QFrame, QSizePolicy, QVBoxLayout, QWidget

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.reader_settings_panel import (
    _right_rounded_path,
    _SectionBanner,
    _SettingRow,
    _SmallSwitch,
    _SpinButton,
)


class NovelCustomPanel(QFrame):
    """Sidebar exposing the novel-specific reader customisation rows."""

    enable_custom_changed = QtSignal(bool)
    font_size_changed = QtSignal(int)

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("NovelCustomPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(Theme.novel_custom_panel_width)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Theme.reader_settings_panel_layout_margin,
            Theme.reader_settings_panel_layout_margin,
            Theme.reader_settings_panel_layout_margin,
            Theme.reader_settings_panel_layout_margin,
        )
        layout.setSpacing(Theme.reader_settings_gap)

        layout.addWidget(_SectionBanner("Light Novel", resources))

        self.enable_switch = _SmallSwitch()
        self.enable_switch.toggled.connect(self.enable_custom_changed.emit)
        self.enable_row = _SettingRow("Enable Custom", self.enable_switch)
        layout.addWidget(self.enable_row)

        # Font size spin uses the same wrapper as the manga panel's
        # spacing row so the visual rhythm stays consistent across the
        # two readers without duplicating the spinner widget.
        self.font_size_control = _SpinButton(
            resources,
            suffix="px",
            value=Theme.novel_placeholder_font_size,
            minimum=8,
            maximum=72,
        )
        self.font_size_control.value_changed.connect(self.font_size_changed.emit)
        self.font_size_row = _SettingRow("Font Size", self.font_size_control, option_margin=0)
        layout.addWidget(self.font_size_row)

        layout.addStretch(1)
        self._sync_child_enabled(False)

    def set_enable_custom(self, enabled: bool, *, emit: bool = False) -> None:
        self.enable_switch.set_checked(enabled, emit=emit)
        self._sync_child_enabled(enabled)

    def set_font_size(self, size: int, *, emit: bool = False) -> None:
        self.font_size_control.set_value(size, emit=emit)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(*Theme.color_reader_settings_background_rgba))
        painter.drawPath(_right_rounded_path(QRectF(self.rect())))
        painter.end()

    def _sync_child_enabled(self, enabled: bool) -> None:
        self.font_size_row.set_row_enabled(enabled)


__all__ = ["NovelCustomPanel"]
