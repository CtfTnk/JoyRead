"""Collapsible Figma search panel for the bookshelf toolbar."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLineEdit,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme


class SearchPanelWidget(QFrame):
    """Figma search panel with expanded and collapsed states."""

    search_submitted = QtSignal(str)

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._resources = resources
        self._expanded = True

        self.setProperty("class", "SearchPanel")
        self.setFixedHeight(Theme.toolbar_control_height)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(2)
        shadow.setOffset(0, 0)
        shadow.setColor(QColor(0, 0, 0, 64))
        self.setGraphicsEffect(shadow)

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(Theme.control_gap)

        self._search_bar = self._build_search_bar()
        self._collapse_button = self._icon_button(
            "icon_left.svg",
            "Collapse search",
            "CollapseSearchButton",
            "FigmaSearchOuterButton",
        )
        self._collapse_button.clicked.connect(lambda: self.set_expanded(False))

        self._expand_button = self._icon_button(
            "icon_search.svg",
            "Expand search",
            "ExpandSearchButton",
            "FigmaSearchOuterButton",
        )
        self._expand_button.clicked.connect(lambda: self.set_expanded(True))

        self._layout.addWidget(self._search_bar)
        self._layout.addWidget(self._collapse_button)
        self._layout.addWidget(self._expand_button)

        self.set_expanded(False)

    @property
    def query(self) -> str:
        return self._input.text()

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self._search_bar.setVisible(expanded)
        self._collapse_button.setVisible(expanded)
        self._expand_button.setVisible(not expanded)
        self.setFixedSize(
            Theme.search_panel_width if expanded else Theme.toolbar_button_size,
            Theme.toolbar_control_height,
        )
        if expanded:
            self._input.setFocus(Qt.FocusReason.MouseFocusReason)

    def submit(self) -> None:
        self.search_submitted.emit(self.query)

    def _build_search_bar(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("FigmaSearchBar")
        frame.setProperty("class", "FigmaSearchBar")
        frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        frame.setFixedSize(Theme.search_width, Theme.toolbar_control_height)

        layout = QHBoxLayout(frame)
        # Figma's 4px padding is measured from the outside of the 1px stroke.
        layout.setContentsMargins(
            Theme.control_layout_margin,
            Theme.control_layout_margin,
            Theme.control_layout_margin,
            Theme.control_layout_margin,
        )
        layout.setSpacing(Theme.search_bar_gap)

        input_frame = QWidget()
        input_frame.setObjectName("FigmaSearchInputFrame")
        input_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        input_layout = QHBoxLayout(input_frame)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(0)

        self._input = QLineEdit()
        self._input.setObjectName("FigmaSearchInput")
        self._input.setPlaceholderText("Search anything...")
        self._input.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._input.setFixedHeight(Theme.search_input_height)
        self._input.setMinimumWidth(Theme.search_input_text_width)
        self._input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._input.returnPressed.connect(self.submit)

        palette = self._input.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(Theme.color_text))
        self._input.setPalette(palette)

        input_layout.addWidget(self._input)
        layout.addWidget(input_frame)

        submit_button = self._icon_button("icon_search.svg", "Search", "SearchSubmitButton", "FigmaSearchInnerButton")
        submit_button.setFixedSize(Theme.search_inner_button_size, Theme.search_inner_button_size)
        submit_button.clicked.connect(self.submit)
        layout.addWidget(submit_button)

        return frame

    def _icon_button(self, icon_name: str, tooltip: str, object_name: str, css_class: str) -> QToolButton:
        button = QToolButton()
        button.setObjectName(object_name)
        button.setProperty("class", css_class)
        button.setIcon(QIcon(str(self._resources.icon_path(icon_name))))
        button.setIconSize(QSize(Theme.icon_size, Theme.icon_size))
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedSize(Theme.toolbar_button_size, Theme.toolbar_button_size)
        return button
