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

from joyread.ui.widgets.localized_text import LocalizedLabel, set_localized

from PySide6.QtCore import QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QIcon, QPainter, QTransform
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget

from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.infrastructure.i18n.locale_service import t


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

        self._label = LocalizedLabel(title)
        self._label.setObjectName(label_object_name)
        self._label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._label, stretch=1)

        if show_indicator:
            arrow = LocalizedLabel()
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
        set_localized(self._label, "setText", title)


class SidebarSectionBanner(SectionBanner):
    """Interactive sidebar header; ordinary settings banners stay decorative."""

    toggle_requested = QtSignal()

    def __init__(self, title, resources, parent=None):
        super().__init__(title, resources, parent)
        self._expanded = None
        self._pressed_inside = False
        self._pointer_inside = False
        self._keyboard_pressed = False
        self._arrow = self.findChild(QLabel, "SidebarSectionArrow")
        self._down_arrow = self._arrow.pixmap()
        self._right_arrow = self._down_arrow.transformed(QTransform().rotate(-90))
        self.setProperty("collapsible", "true")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setMouseTracking(True)
        self.set_expanded(True)

    def set_expanded(self, expanded: bool) -> None:
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self._arrow.setPixmap(self._down_arrow if expanded else self._right_arrow)
        self._refresh_accessibility()

    def set_title(self, title: str) -> None:
        super().set_title(title)
        self._refresh_accessibility()

    def _refresh_accessibility(self):
        title = self._label.text()
        self.setAccessibleName(title)
        hint = t("sidebar.collapse_section" if self._expanded else "sidebar.expand_section", name=title)
        set_localized(self, "setToolTip", hint)
        set_localized(self, "setAccessibleDescription", hint)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed_inside = True
            self._pointer_inside = self.rect().contains(event.position().toPoint())
            self.update()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._pressed_inside:
            self._pressed_inside = False
            self._pointer_inside = self.rect().contains(event.position().toPoint())
            self.update()
            if self._pointer_inside:
                self.toggle_requested.emit()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self._pointer_inside = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._pointer_inside = False
        self.update()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        inside = self.rect().contains(event.position().toPoint())
        if inside != self._pointer_inside:
            self._pointer_inside = inside
            self.update()
        super().mouseMoveEvent(event)

    def focusInEvent(self, event):
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self._keyboard_pressed = False
        self.update()
        super().focusOutEvent(event)

    def hideEvent(self, event):
        self._pressed_inside = False
        self._keyboard_pressed = False
        self._pointer_inside = False
        super().hideEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not event.isAutoRepeat():
                self._keyboard_pressed = True
                self.update()
            event.accept()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not event.isAutoRepeat() and self._keyboard_pressed:
                self._keyboard_pressed = False
                self.update()
                self.toggle_requested.emit()
            event.accept()
        else:
            super().keyReleaseEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        # Paint button feedback directly: changing/repolishing QSS properties
        # on every hover/press would invalidate layout during section reflow.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        down = self._keyboard_pressed or (self._pressed_inside and self._pointer_inside)
        if down or self._pointer_inside:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(Theme.color_selected if down else Theme.color_sidebar_item_hover))
            painter.drawRoundedRect(self.rect(), Theme.sidebar_item_radius, Theme.sidebar_item_radius)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QColor(Theme.color_button_edge))
            painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), Theme.sidebar_item_radius, Theme.sidebar_item_radius)
        painter.end()
