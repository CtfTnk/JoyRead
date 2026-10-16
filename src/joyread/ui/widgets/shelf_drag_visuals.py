"""Cached, mouse-transparent visuals for bookshelf selection and reordering."""
from PySide6.QtCore import QEasingCurve, QPoint, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap, QTransform
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
        self._snapshots = snapshots[:len(Theme.shelf_drag_angles)]
        self._count = count
        size = self._snapshots[0].deviceIndependentSize()
        self._card_width = size.width() * Theme.shelf_drag_scale
        self._card_height = size.height() * Theme.shelf_drag_scale
        self._badge_font = QFont(self.font())
        self._badge_font.setPixelSize(Theme.shelf_drag_badge_font_size)
        self._badge_font.setBold(True)
        self._badge_rect = QRectF()
        if count > 1:
            height = Theme.shelf_drag_badge_diameter
            width = max(height, QFontMetrics(self._badge_font).horizontalAdvance(str(count)) + Theme.spacing_sm)
            self._badge_rect = QRectF(self._card_width + height / 2 - width,
                                     self._card_height - height / 2, width, height)
        self._card_origin = QPoint()
        card_rect = QRectF(0, 0, self._card_width, self._card_height)
        bounds = QRectF(card_rect)
        for index in range(len(self._snapshots)):
            bounds = bounds.united(self._card_transform(index).mapRect(card_rect))
        if count > 1:
            bounds = bounds.united(self._badge_rect)
        # Include the actual rotated corners and the unscaled badge, with room
        # for antialiasing. Transparent padding must never shift the cursor gap.
        bounds = bounds.adjusted(-1, -1, 1, 1).toAlignedRect()
        self._card_origin = -bounds.topLeft()
        self._badge_rect.translate(self._card_origin)
        self.setFixedSize(bounds.size())

    @property
    def card_origin(self) -> QPoint:
        """Anchor the visible front card, excluding transparent rotation padding."""
        return QPoint(self._card_origin)

    def _card_transform(self, index: int) -> QTransform:
        pivot_x = self._card_width * Theme.shelf_drag_pivot[0]
        pivot_y = self._card_height * Theme.shelf_drag_pivot[1]
        transform = QTransform()
        transform.translate(self._card_origin.x() + pivot_x, self._card_origin.y() + pivot_y)
        transform.rotate(Theme.shelf_drag_angles[index])
        transform.translate(-pivot_x, -pivot_y)
        return transform

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        painter.setOpacity(self._fade * Theme.shelf_drag_opacity)
        for index in reversed(range(len(self._snapshots))):
            painter.save()
            painter.setTransform(self._card_transform(index))
            rect = QRectF(0, 0, self._card_width, self._card_height)
            painter.drawPixmap(rect, self._snapshots[index], QRectF(self._snapshots[index].rect()))
            painter.restore()
        if self._count > 1:
            painter.setOpacity(self._fade)
            painter.setFont(self._badge_font)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(Theme.color_text))
            painter.drawEllipse(self._badge_rect)
            painter.setPen(QColor(Theme.color_window))
            painter.drawText(self._badge_rect, Qt.AlignmentFlag.AlignCenter, str(self._count))
        painter.end()


def card_snapshots(surface, moving: tuple[str, ...]) -> list[QPixmap]:
    """Cache cover/title-only cards once, using the covers already held by the UI."""
    snapshots = []
    for key in moving[:len(Theme.shelf_drag_angles)]:
        control = surface.book_controls[key]
        size = (control.size() if isinstance(control, BookCardWidget)
                else QSize(Theme.book_card_width, Theme.book_card_height))
        ratio = control.devicePixelRatioF()
        snapshot = QPixmap(round(size.width() * ratio), round(size.height() * ratio))
        snapshot.setDevicePixelRatio(ratio)
        snapshot.fill(Qt.GlobalColor.transparent)
        painter = QPainter(snapshot)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(0, 0, size.width(), size.height())
        shape = QPainterPath()
        shape.addRoundedRect(rect, Theme.book_card_radius, Theme.book_card_radius)
        painter.setClipPath(shape)
        painter.fillPath(shape, QColor(Theme.color_window))
        inset = Theme.book_card_padding
        cover_rect = QRectF(inset, inset, size.width() - inset * 2,
                           (size.width() - inset * 2) * Theme.cover_height / Theme.cover_width)
        painter.fillRect(cover_rect, QColor(Theme.color_shelf_drag_cover_background))
        cover = control._cover._pixmap
        if not cover.isNull():
            cover_size = cover.deviceIndependentSize()
            cover_size.scale(cover_rect.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF(0, 0, cover_size.width(), cover_size.height())
            target.moveCenter(cover_rect.center())
            painter.drawPixmap(target, cover, QRectF(cover.rect()))
        font = QFont(control.font())
        font.setPixelSize(Theme.shelf_drag_title_font_size)
        painter.setFont(font)
        painter.setPen(QColor(Theme.color_text))
        title_top = cover_rect.bottom() + Theme.book_card_gap
        painter.drawText(QRectF(inset, title_top, cover_rect.width(), size.height() - inset - title_top),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                         control.book.title)
        border = Theme.book_selection_border_width
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(Theme.color_card_selected), border))
        painter.drawRoundedRect(rect.adjusted(border / 2, border / 2, -border / 2, -border / 2),
                                Theme.book_card_radius - border / 2, Theme.book_card_radius - border / 2)
        painter.end()
        snapshots.append(snapshot)
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
