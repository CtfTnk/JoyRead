"""Standalone visual tester for Figma-derived button widgets."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.shelf_viewmodel import FileFilter, SortField, ViewMode
from joyread.ui.widgets.dropdown_button import FigmaDropdownButton
from joyread.ui.widgets.mode_switches import ListModeSwitchWidget, SortModeSwitchWidget
from joyread.ui.widgets.search_panel import SearchPanelWidget


class ButtonTesterWindow(QWidget):
    """Tiny inspection surface for iterating on imported Figma button assets."""

    def __init__(self, resources: ResourceLoader) -> None:
        super().__init__()
        self._resources = resources
        self.setWindowTitle("JoyRead Button Tester")
        self.resize(560, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(18)

        title = QLabel("Figma Button Tester")
        title.setObjectName("TesterTitle")
        root.addWidget(title)

        self._list_status = QLabel()
        self._sort_status = QLabel()
        self._sort_dropdown_status = QLabel()
        self._filter_dropdown_status = QLabel()
        self._search_status = QLabel()

        self._list_switch = ListModeSwitchWidget(resources)
        self._list_switch.value_changed.connect(self._update_list_status)
        root.addWidget(self._section("switch_list_mode / node 50:574", self._list_switch, self._list_status))

        self._sort_switch = SortModeSwitchWidget(resources)
        self._sort_switch.value_changed.connect(self._update_sort_status)
        root.addWidget(self._section("switch_sort_mode / node 69:462", self._sort_switch, self._sort_status))

        self._sort_dropdown = FigmaDropdownButton(
            resources,
            [field.value for field in SortField],
            width=Theme.sort_dropdown_width,
            initial_value=SortField.ADD_TIME.value,
            tooltip="Sort by",
        )
        self._sort_dropdown.value_changed.connect(self._update_sort_dropdown_status)
        root.addWidget(
            self._section("dropout-button_sortby / node 61:133", self._sort_dropdown, self._sort_dropdown_status)
        )

        self._filter_dropdown = FigmaDropdownButton(
            resources,
            [filter_name.value for filter_name in FileFilter],
            width=Theme.file_filter_width,
            initial_value=FileFilter.ALL.value,
            tooltip="Filter by file type",
        )
        self._filter_dropdown.value_changed.connect(self._update_filter_dropdown_status)
        root.addWidget(
            self._section(
                "dropout-button_file-type / node 70:515",
                self._filter_dropdown,
                self._filter_dropdown_status,
            )
        )

        self._search_panel = SearchPanelWidget(resources)
        self._search_panel.search_submitted.connect(self._update_search_status)
        root.addWidget(self._section("search-panel_extended / node 50:1072", self._search_panel, self._search_status))

        disabled_row = QFrame()
        disabled_row.setObjectName("TesterPanel")
        disabled_layout = QHBoxLayout(disabled_row)
        disabled_layout.setContentsMargins(12, 12, 12, 12)
        disabled_layout.setSpacing(16)
        disabled_layout.addWidget(QLabel("Disabled examples"))
        disabled_list = ListModeSwitchWidget(resources)
        disabled_sort = SortModeSwitchWidget(resources)
        disabled_list.setEnabled(False)
        disabled_sort.setEnabled(False)
        disabled_layout.addStretch(1)
        disabled_layout.addWidget(disabled_list)
        disabled_layout.addWidget(disabled_sort)
        root.addWidget(disabled_row)

        reset_button = QPushButton("Reset")
        reset_button.setFixedSize(Theme.tester_reset_width, Theme.tester_reset_height)
        reset_button.clicked.connect(self._reset)
        root.addWidget(reset_button, alignment=Qt.AlignmentFlag.AlignRight)
        root.addStretch(1)

        self._reset()

    def _section(self, title: str, switch: QWidget, status: QLabel) -> QFrame:
        frame = QFrame()
        frame.setObjectName("TesterPanel")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(16)

        label_group = QVBoxLayout()
        label_group.setSpacing(4)
        label = QLabel(title)
        label.setObjectName("TesterSectionTitle")
        status.setObjectName("TesterStatus")
        label_group.addWidget(label)
        label_group.addWidget(status)

        layout.addLayout(label_group, stretch=1)
        layout.addWidget(switch)
        return frame

    def _reset(self) -> None:
        self._list_switch.set_value(ViewMode.GRID.value)
        self._sort_switch.set_ascending(False)
        self._update_list_status(self._list_switch.value)
        self._update_sort_status(self._sort_switch.value)
        self._sort_dropdown.set_value(SortField.ADD_TIME.value)
        self._filter_dropdown.set_value(FileFilter.ALL.value)
        self._search_panel.set_expanded(True)
        self._update_sort_dropdown_status(self._sort_dropdown.value)
        self._update_filter_dropdown_status(self._filter_dropdown.value)
        self._update_search_status(self._search_panel.query)

    def _update_list_status(self, value: str) -> None:
        self._list_status.setText(f"Current value: {value}")

    def _update_sort_status(self, value: str) -> None:
        direction = "ascending" if value == SortModeSwitchWidget.ASCENDING else "descending"
        self._sort_status.setText(f"Current value: {direction}")

    def _update_sort_dropdown_status(self, value: str) -> None:
        self._sort_dropdown_status.setText(f"Current value: {value}")

    def _update_filter_dropdown_status(self, value: str) -> None:
        self._filter_dropdown_status.setText(f"Current value: {value}")

    def _update_search_status(self, value: str) -> None:
        self._search_status.setText(f"Submitted query: {value or '(empty)'}")


def create_button_tester(argv: list[str] | None = None) -> tuple[QApplication, ButtonTesterWindow]:
    app = QApplication(argv or sys.argv)
    resources = ResourceLoader()
    app.setStyleSheet(resources.load_stylesheet() + _tester_stylesheet())
    return app, ButtonTesterWindow(resources)


def _tester_stylesheet() -> str:
    return """
    QWidget {
        background: #f5f5f5;
    }
    QLabel#TesterTitle {
        font-size: 20px;
        font-weight: 700;
    }
    QFrame#TesterPanel {
        background: #ffffff;
        border: 1px solid #e0e0e0;
        border-radius: 10px;
    }
    QLabel#TesterSectionTitle {
        font-size: 14px;
        font-weight: 700;
    }
    QLabel#TesterStatus {
        color: #6d6d6d;
        font-size: 12px;
    }
    QPushButton {
        background: #ffffff;
        border: 1px solid #929292;
        border-radius: 10px;
        min-height: 32px;
        font-size: 14px;
    }
    QPushButton:hover {
        background: #e5e5e5;
    }
    """


def main() -> int:
    app, window = create_button_tester(sys.argv)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
