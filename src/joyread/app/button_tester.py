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
from joyread.ui.viewmodels.shelf_viewmodel import ViewMode
from joyread.ui.widgets.mode_switches import ListModeSwitchWidget, SortModeSwitchWidget


class ButtonTesterWindow(QWidget):
    """Tiny inspection surface for iterating on imported Figma button assets."""

    def __init__(self, resources: ResourceLoader) -> None:
        super().__init__()
        self._resources = resources
        self.setWindowTitle("JoyRead Button Tester")
        self.resize(520, 360)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(18)

        title = QLabel("Figma Switch Button Tester")
        title.setObjectName("TesterTitle")
        root.addWidget(title)

        self._list_status = QLabel()
        self._sort_status = QLabel()

        self._list_switch = ListModeSwitchWidget(resources)
        self._list_switch.value_changed.connect(self._update_list_status)
        root.addWidget(self._section("switch_list_mode / node 50:574", self._list_switch, self._list_status))

        self._sort_switch = SortModeSwitchWidget(resources)
        self._sort_switch.value_changed.connect(self._update_sort_status)
        root.addWidget(self._section("switch_sort_mode / node 69:462", self._sort_switch, self._sort_status))

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

    def _update_list_status(self, value: str) -> None:
        self._list_status.setText(f"Current value: {value}")

    def _update_sort_status(self, value: str) -> None:
        direction = "ascending" if value == SortModeSwitchWidget.ASCENDING else "descending"
        self._sort_status.setText(f"Current value: {direction}")


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
