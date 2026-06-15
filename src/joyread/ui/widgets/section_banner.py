"""Reusable section header banner.

The banner first shipped as the sidebar's "Book Shelf" / "Collections" header.
It is now shared with the settings page so that grouping inside General
(``General`` and ``Cache`` sub-groups) uses the same visual language as the
sidebar.

The widget keeps the sidebar's default ``QFrame#SidebarSectionBanner`` and
``QLabel#SidebarSectionLabel`` / ``QLabel#SidebarSectionArrow`` object names by
default so existing QSS continues to apply without changes. Callers that want
distinct styling (different padding, no chevron) can override via the
constructor.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme


class SectionBanner(QFrame):
    """Title cell with an optional chevron indicator on the right."""

    def __init__(
        self,
        title: str,
        resources: ResourceLoader,
        parent: QWidget | None = None,
        *,
        show_indicator: bool = True,
        frame_object_name: str = "SidebarSectionBanner",
        label_object_name: str = "SidebarSectionLabel",
        indicator_object_name: str = "SidebarSectionArrow",
    ) -> None:
        super().__init__(parent)
        self.setObjectName(frame_object_name)
        self.setFixedHeight(Theme.sidebar_section_height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.sidebar_section_padding_left,
            Theme.sidebar_section_padding_top,
            Theme.sidebar_section_padding_right,
            Theme.sidebar_section_padding_bottom,
        )
        layout.setSpacing(0)

        self._label = QLabel(title)
        self._label.setObjectName(label_object_name)
        self._label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._label, stretch=1)

        if show_indicator:
            arrow = QLabel()
            arrow.setObjectName(indicator_object_name)
            arrow.setFixedSize(Theme.sidebar_section_arrow_size, Theme.sidebar_section_arrow_size)
            arrow.setPixmap(
                QIcon(str(resources.icon_path("icon_dropout.svg"))).pixmap(
                    QSize(Theme.sidebar_section_arrow_size, Theme.sidebar_section_arrow_size)
                )
            )
            layout.addWidget(arrow)

    def set_title(self, title: str) -> None:
        """Update the displayed title text (called on language change)."""
        self._label.setText(title)


# Backward-compatible alias retained for the sidebar so existing imports and
# tests do not need to change. New callers should reach for ``SectionBanner``.
SidebarSectionBanner = SectionBanner
