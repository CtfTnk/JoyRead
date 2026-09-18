"""A keyed sidebar section whose body can be hidden without rebuilding rows."""

from PySide6.QtCore import Signal as QtSignal
from PySide6.QtWidgets import QWidget, QVBoxLayout

from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.section_banner import SidebarSectionBanner


class SidebarSection(QWidget):
    expansion_requested = QtSignal(str, bool)

    def __init__(self, key, title, resources, parent=None):
        super().__init__(parent)
        self.key = key
        self.expanded = True
        self.setObjectName("SidebarSectionGroup")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Theme.sidebar_gap)
        self.banner = SidebarSectionBanner(title, resources)
        layout.addWidget(self.banner)
        self.body = QWidget(self)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(Theme.sidebar_gap)
        layout.addWidget(self.body)
        self.banner.toggle_requested.connect(lambda: self.expansion_requested.emit(self.key, not self.expanded))

    def set_expanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        if expanded == self.expanded:
            return
        self.expanded = expanded
        self.body.setVisible(self.expanded)
        self.banner.set_expanded(self.expanded)
