"""Presentation-only material context and inside-painted interaction surfaces."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QWidget

from joyread.ui.resources.styles.theme import GlassPalette, Theme


def glass_profile(widget: QWidget | None) -> str:
    """Resolve the nearest surface context, including a popup's owning widget.

    This follows the actual UI hierarchy, so a menu inside an embedded Reader
    uses the Reader preset even though its top-level owner is the Library.
    No screenshots, image sampling, or per-frame work are needed.
    """
    while widget is not None:
        profile = widget.property("glassBackdrop")
        if isinstance(profile, str) and profile in Theme.glass_palettes:
            return profile
        widget = widget.parentWidget()
    return "quiet"


def glass_palette(widget: QWidget) -> GlassPalette:
    return Theme.glass_palettes[glass_profile(widget)]


class MaterialItemFrame(QFrame):
    """List feedback without consuming its compact text/layout allocation."""

    def __init__(self, parent: QWidget | None = None, *, radius: int) -> None:
        super().__init__(parent)
        self._material_radius = radius
        self._material_hovered = False
        self._material_pressed = False
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def event(self, event: QEvent) -> bool:
        # QFrame has no QAbstractButton pressed state. Observe input before
        # each row's existing click handler, without consuming or emitting it.
        if hasattr(self, "_material_pressed"):
            kind = event.type()
            if kind == QEvent.Type.Enter:
                self._material_hovered = True
                self.update()
            elif kind == QEvent.Type.Leave:
                self._material_hovered = False
                self.update()
            elif kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._material_pressed = True
                self.update()
            elif kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self._material_pressed = False
                self.update()
            elif kind in (QEvent.Type.Hide, QEvent.Type.WindowDeactivate, QEvent.Type.EnabledChange):
                self._material_hovered = False
                self._material_pressed = False
                self.update()
            elif kind == QEvent.Type.DynamicPropertyChange:
                self.update()
        return super().event(event)

    def paintEvent(self, event) -> None:
        del event
        if not self.isEnabled() or self.property("menuEnabled") in (False, "false"):
            return
        palette = glass_palette(self)
        if self._material_hovered and self._material_pressed:
            fill, edge = palette.pressed, palette.pressed_edge
        elif self.property("selected") in (True, "true"):
            fill, edge = palette.selected, palette.selected_edge
        elif self._material_hovered:
            fill, edge = palette.hover, palette.hover_edge
        else:
            return
        painter = QPainter(self)
        paint_material_surface(painter, QRectF(self.rect()), fill, edge, self._material_radius)


def paint_material_surface(
    painter: QPainter,
    bounds: QRectF,
    fill: tuple[int, int, int, int],
    edge: tuple[int, int, int, int],
    radius: int,
) -> None:
    """Paint a material inside its bounds without reserving layout space."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    width = Theme.glass_item_border_width
    inset = width / 2
    painter.setPen(QPen(QColor(*edge), width))
    painter.setBrush(QColor(*fill))
    inner_radius = max(0, radius - inset)
    painter.drawRoundedRect(bounds.adjusted(inset, inset, -inset, -inset), inner_radius, inner_radius)
    painter.restore()
