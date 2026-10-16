"""Fixed title and quiet close action shared by the floating content panels."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QHBoxLayout, QToolButton, QWidget

from joyread.infrastructure.i18n.locale_service import t
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.elided_label import ElidedLabel
from joyread.ui.widgets.localized_text import set_localized


class FloatingPanelHeader(QWidget):
    close_requested = QtSignal()

    def __init__(
        self,
        resources: ResourceLoader,
        title: str,
        *,
        body_inset: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("FloatingPanelHeader")
        self.setFixedHeight(Theme.floating_panel_header_height)

        layout = QHBoxLayout(self)
        # The surrounding layout already owns the body's 8px visual inset.
        # Add only the remainder to put header controls 16px from the panel.
        inset = Theme.floating_panel_header_horizontal_inset - body_inset
        layout.setContentsMargins(inset, 0, inset, 0)
        layout.setSpacing(0)

        self.title = ElidedLabel()
        self.title.setObjectName("FloatingPanelTitle")
        font = QFont(self.title.font())
        font.setPixelSize(Theme.floating_panel_header_font_size)
        font.setWeight(QFont.Weight.DemiBold)
        self.title.setFont(font)
        self.title.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.title, stretch=1)

        self.close_button = QToolButton()
        self.close_button.setProperty("class", "FloatingPanelCloseButton")
        self.close_button.setIcon(QIcon(str(resources.icon_path("icon_close.svg"))))
        self.close_button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
        self.close_button.setFixedSize(Theme.floating_panel_close_size, Theme.floating_panel_close_size)
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        set_localized(self.close_button, "setToolTip", t("toolbar.close"))
        set_localized(self.close_button, "setAccessibleName", t("toolbar.close"))
        self.close_button.clicked.connect(self.close_requested.emit)
        layout.addWidget(self.close_button)
        self.set_title(title)

    def set_title(self, title: str) -> None:
        set_localized(self.title, "setText", title)
