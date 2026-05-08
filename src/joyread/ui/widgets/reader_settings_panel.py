"""Reader-local settings panel."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal as QtSignal
from PySide6.QtWidgets import QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QSpinBox, QVBoxLayout, QWidget

from joyread.core.reader import ReaderFitMode, ReaderSettings
from joyread.ui.resources.styles.theme import Theme


class ReaderSettingsPanel(QFrame):
    custom_enabled_changed = QtSignal(bool)
    always_one_page_changed = QtSignal(bool)
    fit_mode_changed = QtSignal(object)
    page_spacing_changed = QtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ReaderSettingsPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(Theme.reader_settings_panel_width)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            Theme.reader_settings_panel_layout_margin,
            Theme.reader_settings_panel_layout_margin,
            Theme.reader_settings_panel_layout_margin,
            Theme.reader_settings_panel_layout_margin,
        )
        layout.setSpacing(Theme.reader_settings_gap)

        self.custom_check = _check("Custom horizontal layout")
        self.custom_check.toggled.connect(self.custom_enabled_changed.emit)
        layout.addWidget(self.custom_check)

        self.one_page_check = _check("Always one page")
        self.one_page_check.toggled.connect(self.always_one_page_changed.emit)
        layout.addWidget(self.one_page_check)

        fit_row = _row("Fit Mode")
        self.fit_combo = QComboBox()
        self.fit_combo.setObjectName("ReaderFitModeCombo")
        for mode, label in (
            (ReaderFitMode.AUTO, "Auto"),
            (ReaderFitMode.FIT_HEIGHT, "Fit to Height"),
            (ReaderFitMode.FIT_WIDTH, "Fit to Width"),
            (ReaderFitMode.FIT_PAGE, "Fit to Page"),
        ):
            self.fit_combo.addItem(label, mode)
        self.fit_combo.currentIndexChanged.connect(self._emit_fit_mode)
        fit_row.layout().addWidget(self.fit_combo)
        layout.addWidget(fit_row)

        spacing_row = _row("Vertical Page Spacing")
        self.spacing_spin = QSpinBox()
        self.spacing_spin.setRange(0, 200)
        self.spacing_spin.valueChanged.connect(self.page_spacing_changed.emit)
        spacing_row.layout().addWidget(self.spacing_spin)
        layout.addWidget(spacing_row)

    def set_settings(self, settings: ReaderSettings) -> None:
        self.custom_check.blockSignals(True)
        self.one_page_check.blockSignals(True)
        self.fit_combo.blockSignals(True)
        self.spacing_spin.blockSignals(True)
        self.custom_check.setChecked(settings.custom_enabled)
        self.one_page_check.setChecked(settings.always_one_page)
        self._set_fit_mode(settings.fit_mode)
        self.spacing_spin.setValue(settings.page_spacing)
        self.custom_check.blockSignals(False)
        self.one_page_check.blockSignals(False)
        self.fit_combo.blockSignals(False)
        self.spacing_spin.blockSignals(False)
        self._sync_child_enabled(settings.custom_enabled)

    def _emit_fit_mode(self) -> None:
        mode = self.fit_combo.currentData()
        if isinstance(mode, ReaderFitMode):
            self.fit_mode_changed.emit(mode)

    def _set_fit_mode(self, fit_mode: ReaderFitMode) -> None:
        for index in range(self.fit_combo.count()):
            if self.fit_combo.itemData(index) == fit_mode:
                self.fit_combo.setCurrentIndex(index)
                return

    def _sync_child_enabled(self, custom_enabled: bool) -> None:
        self.one_page_check.setEnabled(custom_enabled)
        self.fit_combo.setEnabled(custom_enabled)


def _check(text: str) -> QCheckBox:
    check = QCheckBox(text)
    check.setProperty("class", "ReaderSettingsLabel")
    check.setFixedHeight(Theme.reader_settings_row_height)
    return check


def _row(label: str) -> QWidget:
    row = QWidget()
    row.setFixedHeight(Theme.reader_settings_row_height)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(Theme.reader_settings_gap)
    text = QLabel(label)
    text.setProperty("class", "ReaderSettingsLabel")
    layout.addWidget(text)
    layout.addStretch(1)
    return row
