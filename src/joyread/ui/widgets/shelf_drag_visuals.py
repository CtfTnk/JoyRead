"""Cached, mouse-transparent visuals for bookshelf selection and reordering."""
from PySide6.QtCore import QEasingCurve, QPoint, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from joyread.infrastructure.resources.pixmaps import blurred_pixmap
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.book_card import BookCardWidget


class FadingVisual(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._fade = 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(Theme.shelf_drag_animation_ms)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._set_fade)

    def _set_fade(self, value):
        self._fade = float(value)
        self.update()

    def fade_in(self):
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self.show()
        self._animation.start()

    def fade_out(self):
        self._animation.stop()
        self._animation.setStartValue(self._fade)
        self._animation.setEndValue(0.0)
        self._animation.finished.connect(self.deleteLater)
        self.raise_()
        self._animation.start()


class DropPlaceholder(FadingVisual):
    def __init__(self, source: QPixmap, size, parent):
        super().__init__(parent)
        self.setFixedSize(size)
        self._snapshot = blurred_pixmap(source, Theme.shelf_drag_blur_radius)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        border = Theme.drop_zone_border_width
        rect = QRectF(self.rect()).adjusted(border / 2, border / 2, -border / 2, -border / 2)
        shape = QPainterPath()
        shape.addRoundedRect(rect, Theme.book_card_radius, Theme.book_card_radius)
        painter.setClipPath(shape)
        painter.setOpacity(self._fade * Theme.shelf_drag_placeholder_opacity)
        painter.drawPixmap(self.rect(), self._snapshot)
        painter.setOpacity(self._fade)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(Theme.color_card_selected), border, Qt.PenStyle.DashLine))
        painter.drawPath(shape)
        painter.end()


class DragCardPreview(FadingVisual):
    def __init__(self, snapshots: list[QPixmap], count: int, parent):
        super().__init__(parent)
        self._snapshots = snapshots
        self._count = count
        self._card_width = Theme.book_card_width * Theme.shelf_drag_scale
        self._card_height = Theme.book_card_height * Theme.shelf_drag_scale
        self._padding = Theme.shelf_drag_stack_offset * 3
        self.setFixedSize(round(self._card_width + self._padding * 2), round(self._card_height + self._padding * 2))

    @property
    def card_origin(self) -> QPoint:
        """Anchor the visible front card, excluding transparent rotation padding."""
        return QPoint(self._padding, self._padding)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        painter.setOpacity(self._fade * Theme.shelf_drag_opacity)
        for index in reversed(range(len(self._snapshots))):
            painter.save()
            offset = index * Theme.shelf_drag_stack_offset
            painter.translate(self._padding + self._card_width / 2 + offset,
                              self._padding + self._card_height / 2 + offset)
            painter.rotate(Theme.shelf_drag_angles[index])
            rect = QRectF(-self._card_width / 2, -self._card_height / 2, self._card_width, self._card_height)
            painter.drawPixmap(rect, self._snapshots[index], QRectF(self._snapshots[index].rect()))
            painter.restore()
        if self._count > 1:
            painter.setOpacity(self._fade)
            font = QFont(self.font())
            font.setPixelSize(Theme.shelf_drag_badge_font_size)
            font.setBold(True)
            painter.setFont(font)
            diameter = max(Theme.shelf_drag_badge_diameter, painter.fontMetrics().horizontalAdvance(str(self._count)) + Theme.spacing_md)
            rect = QRectF(self._padding + self._card_width - diameter / 2,
                          self._padding + self._card_height - diameter / 2, diameter, diameter)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(Theme.color_text))
            painter.drawEllipse(rect)
            painter.setPen(QColor(Theme.color_window))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(self._count))
        painter.end()


def card_snapshots(surface, moving: tuple[str, ...], resources) -> list[QPixmap]:
    """Snapshot once. List rows use the same card presentation without disk I/O."""
    snapshots = []
    for key in moving[:len(Theme.shelf_drag_angles)]:
        control = surface.book_controls[key]
        if isinstance(control, BookCardWidget):
            snapshots.append(control.grab())
        else:
            card = BookCardWidget(control.book, resources, surface._content)
            card._cover.set_pixmap(control._cover._pixmap)
            card.ensurePolished()
            card.layout().activate()
            snapshots.append(card.grab())
            card.deleteLater()
    return snapshots


class SelectionRectangle(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter = QPainter(self)
        fill = QColor(Theme.color_card_selected)
        fill.setAlpha(Theme.shelf_selection_fill_alpha)
        painter.fillRect(self.rect(), fill)
        painter.setPen(QColor(Theme.color_card_selected))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.end()
