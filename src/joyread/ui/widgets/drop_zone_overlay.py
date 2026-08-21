"""Drag-and-drop target overlay: Read on the left, Import on the right.

Adapted from the "Main Window Drag Drop" design screen. Dropping a file on the
library is ambiguous -- it could mean *read this now* or *add this to my
library*, and those are different pipelines -- so instead of guessing, the drag
raises a scrim over the content area with one zone for each meaning. Nothing
commits until the pointer is released inside a zone.

The two zones are painted regions hit-tested by pointer position, not droppable
child widgets, and the overlay itself accepts no drops -- the host window owns
the four drag events and forwards them here. Both choices are about how Qt
resolves a drop target:

* The target is resolved by position, so an overlay that only appears part-way
  through a drag cannot count on being sent its own ``dragEnterEvent``. Letting
  the window that was already under the cursor stay the target removes that
  timing question entirely, and makes the zone logic testable without staging a
  real drag.
* Qt delivers a ``dragLeaveEvent`` whenever the pointer crosses into a child
  that also accepts drops. Droppable child zones would need the running
  enter/leave depth counter the source design used to undo those spurious
  leaves; two rectangles and a ``contains`` check need no such bookkeeping.

The zones are drawn rather than styled because dashed rounded borders,
translucent fills over a blurred snapshot, and a hover scale on the icon disc
are all outside what the stylesheet can express.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QRect,
    QRectF,
    QTimer,
    QVariantAnimation,
    Qt,
    Signal as QtSignal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QWidget

from joyread.app.launch.intent import DropPayload, ReadUnavailable, classify_drop_paths
from joyread.infrastructure.i18n.locale_service import t
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.icon_paint import tinted_pixmap


logger = logging.getLogger(__name__)

READ_ZONE = "read"
IMPORT_ZONE = "import"


def payload_from_mime_urls(urls) -> DropPayload:  # noqa: ANN001 - QUrl sequence.
    """Classify the local files in a drop's mime data.

    Non-local URLs (a drag out of a browser, say) carry nothing this app can
    open, so they are dropped before classification rather than failing later
    as a path that does not exist.
    """

    return classify_drop_paths(url.toLocalFile() for url in urls if url.isLocalFile())


class DropZoneOverlay(QWidget):
    """Scrim with two drop zones, shown for the duration of a drag."""

    read_requested = QtSignal(Path)
    import_requested = QtSignal(tuple)

    def __init__(self, resources: ResourceLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DropZoneOverlay")
        # Deliberately not a drop target. Qt resolves the target widget by
        # position, so an overlay that appears part-way through a drag cannot
        # count on receiving its own dragEnter; the host window owns the four
        # drag events and forwards them here. The overlay never wants the mouse
        # either -- it is only ever up while a drag is in flight.
        self.setAcceptDrops(False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._payload = DropPayload()
        self._active = False
        self._hover_zone: str | None = None
        self._confirming = False
        self._confirm_count = 0
        self._snapshot: QPixmap | None = None
        self._backdrop: QPixmap | None = None
        self._content_area: QWidget | None = None

        self._read_glyph = tinted_pixmap(
            str(resources.icon_path("icon_read.svg")),
            Theme.drop_zone_icon_glyph_size,
            color=Qt.GlobalColor.white,
        )
        self._read_glyph_disabled = tinted_pixmap(
            str(resources.icon_path("icon_read.svg")),
            Theme.drop_zone_icon_glyph_size,
            color=Qt.GlobalColor.white,
            opacity=Theme.drop_zone_disabled_glyph_opacity,
        )
        self._import_glyph = tinted_pixmap(
            str(resources.icon_path("icon_import.svg")),
            Theme.drop_zone_icon_glyph_size,
            color=Qt.GlobalColor.white,
        )
        self._confirm_glyph = tinted_pixmap(
            str(resources.icon_path("icon_confirm.svg")),
            Theme.drop_confirm_glyph_size,
            color=Qt.GlobalColor.white,
        )

        # Hover is a discrete state, but it is drawn as a continuous one:
        # fill, border, and icon scale ease between rest and hover over
        # ``drop_zone_transition_ms`` instead of snapping.
        self._hover_progress: dict[str, float] = {READ_ZONE: 0.0, IMPORT_ZONE: 0.0}
        self._hover_animations: dict[str, QVariantAnimation] = {}
        for zone in (READ_ZONE, IMPORT_ZONE):
            animation = QVariantAnimation(self)
            animation.setDuration(Theme.drop_zone_transition_ms)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            animation.valueChanged.connect(
                lambda value, name=zone: self._handle_hover_value(name, value)
            )
            self._hover_animations[zone] = animation

        self._fade = QVariantAnimation(self)
        self._fade.setDuration(Theme.drop_scrim_fade_ms)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.valueChanged.connect(self._handle_fade_value)
        self._fade.finished.connect(self._handle_fade_finished)
        self._opacity = 0.0

        self._confirm_timer = QTimer(self)
        self._confirm_timer.setSingleShot(True)
        self._confirm_timer.setInterval(Theme.drop_confirm_hold_ms)
        self._confirm_timer.timeout.connect(self.end)

        self.hide()

    # ------------------------------------------------------------------
    # State the host drives
    # ------------------------------------------------------------------

    @property
    def payload(self) -> DropPayload:
        """The live drag's payload, or an empty one once the drag is over.

        Painting reads ``_payload`` directly so a dismissed overlay keeps
        showing what it showed while it fades; callers get the empty payload
        immediately, so a late event cannot commit a drag that already ended.
        """

        return self._payload if self._active else DropPayload()

    @property
    def hover_zone(self) -> str | None:
        return self._hover_zone

    @property
    def is_confirming(self) -> bool:
        return self._confirming

    def set_content_area(self, widget: QWidget | None) -> None:
        """The region this overlay covers, and the image it blurs behind itself.

        Must be a sibling: geometry is copied straight across, so both widgets
        have to be in the same parent's coordinate system. ``None`` leaves the
        overlay at whatever size it was given and falls back to a flat scrim.
        """

        if widget is not None and widget.parentWidget() is not self.parentWidget():
            # Geometry is copied across verbatim below, which only means
            # anything while both widgets share a coordinate system. Silent
            # mispositioning already shipped here once; say so instead.
            logger.warning(
                "Drop overlay content area is not a sibling; geometry will be wrong"
            )
        self._content_area = widget
        self.sync_geometry()

    def sync_geometry(self) -> None:
        """Match the content area again.

        Called from :meth:`begin` rather than only when the host is resized:
        a resize event fires *before* the layout has moved the content area, so
        sampling it there alone leaves the overlay parked at whatever geometry
        the panel happened to have mid-layout.
        """

        content = self._content_area
        if content is None:
            return
        geometry = content.geometry()
        if geometry.isEmpty():
            return
        resized = geometry.size() != self.size()
        self.setGeometry(geometry)
        if resized and self._snapshot is not None:
            # The snapshot is painted stretched to the current rect, so a window
            # resized mid-drag would show a squashed shelf until the drag ends.
            self._capture_backdrop()

    def begin(self, payload: DropPayload) -> None:
        """Raise the overlay for *payload*. Ignored for an empty payload."""

        if not payload.can_import:
            return
        self._confirm_timer.stop()
        self._confirming = False
        self._confirm_count = 0
        self._payload = payload
        self._active = True
        self._hover_zone = None
        for zone, animation in self._hover_animations.items():
            animation.stop()
            self._hover_progress[zone] = 0.0
        self.sync_geometry()
        self._capture_backdrop()
        self.show()
        self.raise_()
        self._animate_to(1.0)

    def end(self) -> None:
        """Dismiss the overlay and stop accepting this drag.

        What is on screen is deliberately left alone until the fade completes.
        Clearing the payload or the checkmark here would repaint the overlay
        into its default state at full opacity for the first frame of the fade,
        so the last thing the user sees is a flash of the wrong content.
        """

        self._confirm_timer.stop()
        self._active = False
        self._hover_zone = None
        self._animate_to(0.0)

    def show_import_confirmation(self, count: int) -> None:
        """Hold a checkmark for the design's dwell, then dismiss.

        Only Import uses this. A committed Read opens a reader window, and that
        window appearing is the acknowledgement -- a scrim on the library
        announcing it would be talking about something already on screen.
        """

        self._confirming = True
        self._confirm_count = max(1, count)
        # The drop is spent: nothing else may commit against this payload.
        self._active = False
        self._hover_zone = None
        # The design deepens both the scrim and the blur on confirm. Re-blurring
        # the snapshot is why it is kept: the alternative is grabbing the panel
        # a second time, which costs a full render for an image that has not
        # changed.
        self._refresh_backdrop()
        self.update()
        self._confirm_timer.start()

    # ------------------------------------------------------------------
    # Drag handling, driven by the host window
    # ------------------------------------------------------------------

    def update_hover(self, point: QPoint) -> None:
        """Track the pointer, in this widget's coordinates."""

        self._set_hover(self._zone_at(point))

    def handle_drop(self, point: QPoint) -> bool:
        """Commit the drop at *point*. Returns whether anything was committed.

        Releasing on the scrim between the zones is a cancel rather than a
        choice, and so is releasing on a Read zone that is drawn disabled --
        picking a file the user never singled out would be worse than doing
        nothing.
        """

        payload = self._payload
        zone = self._zone_at(point)
        if not self._active or zone is None or not payload.can_import:
            self.end()
            return False

        if zone == READ_ZONE:
            read_path = payload.read_path
            if read_path is None:
                logger.info(
                    "Drop on the disabled Read zone was refused reason=%s",
                    payload.read_unavailable,
                )
                self.end()
                return False
            self.end()
            self.read_requested.emit(read_path)
            return True

        paths = payload.import_paths
        self.show_import_confirmation(payload.item_count)
        self.import_requested.emit(paths)
        return True

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------

    def zone_rects(self) -> dict[str, QRect]:
        """Both zone rectangles, in this widget's coordinates."""

        top = Theme.drop_pill_margin_top + Theme.drop_pill_height + Theme.drop_zone_padding_top
        left = Theme.drop_zone_padding_horizontal
        available = QRect(
            left,
            top,
            max(0, self.width() - left * 2),
            max(0, self.height() - top - Theme.drop_zone_padding_bottom),
        )
        gap = Theme.drop_zone_gap
        column = max(0, (available.width() - gap) // 2)
        return {
            READ_ZONE: QRect(available.left(), available.top(), column, available.height()),
            IMPORT_ZONE: QRect(
                available.left() + column + gap, available.top(), column, available.height()
            ),
        }

    def _zone_at(self, point: QPoint) -> str | None:
        for name, rect in self.zone_rects().items():
            if rect.contains(point):
                return name
        return None

    def _set_hover(self, zone: str | None) -> None:
        # A disabled Read zone never reads as hovered: the pointer being over it
        # must not suggest a release there would do something.
        if zone == READ_ZONE and not self._payload.can_read:
            zone = None
        if zone == self._hover_zone:
            return
        self._hover_zone = zone
        for name, animation in self._hover_animations.items():
            target = 1.0 if name == zone else 0.0
            current = self._hover_progress[name]
            if current == target:
                animation.stop()
                continue
            animation.stop()
            animation.setStartValue(current)
            animation.setEndValue(target)
            animation.start()
        self.update()

    def _handle_hover_value(self, zone: str, value: object) -> None:
        self._hover_progress[zone] = float(value)  # type: ignore[arg-type]
        self.update()

    # ------------------------------------------------------------------
    # Backdrop
    # ------------------------------------------------------------------

    def _capture_backdrop(self) -> None:
        """Grab and blur what is underneath, once per drag.

        Qt has no ``backdrop-filter``. Putting a ``QGraphicsBlurEffect`` on the
        live content widget would re-rasterize the whole shelf on every repaint
        and disturb how its children paint; nothing under the scrim animates
        during a drag, so one snapshot is both cheaper and just as accurate.
        Any failure leaves ``_backdrop`` unset and the flat scrim carries the
        overlay on its own.
        """

        source = self._content_area
        if source is None or source.width() <= 0 or source.height() <= 0:
            self._snapshot = None
            self._backdrop = None
            return
        if self._snapshot is not None and self._snapshot.size() == source.size() * (
            self._snapshot.devicePixelRatio() or 1.0
        ):
            # Still current. Re-entering the window during one drag would
            # otherwise re-render the whole shelf and re-blur it, and the
            # hide/show below is a real repaint that can flicker the overlay.
            self._refresh_backdrop()
            return
        self._snapshot = None
        self._backdrop = None
        # QWidget.grab draws children too. The host points this at a sibling, so
        # the overlay is not in that subtree today -- but a later re-parenting
        # would have it photograph itself and blur its own zones into the
        # backdrop, which reads as a rendering bug rather than a wiring one.
        was_visible = self.isVisible()
        if was_visible:
            self.setVisible(False)
        try:
            grabbed = source.grab()
        except Exception:  # pragma: no cover - defensive around a platform grab.
            logger.warning("Could not grab a backdrop for the drop overlay", exc_info=True)
            return
        finally:
            if was_visible:
                self.setVisible(True)
        if grabbed.isNull():
            return
        self._snapshot = grabbed
        self._refresh_backdrop()

    def _refresh_backdrop(self) -> None:
        """Re-blur the held snapshot at the radius the current state wants."""

        if self._snapshot is None:
            self._backdrop = None
            return
        radius = (
            Theme.drop_scrim_blur_radius_confirming
            if self._confirming
            else Theme.drop_scrim_blur_radius
        )
        self._backdrop = _blurred(self._snapshot, radius)

    # ------------------------------------------------------------------
    # Fade
    # ------------------------------------------------------------------

    def _animate_to(self, target: float) -> None:
        self._fade.stop()
        self._fade.setStartValue(float(self._opacity))
        self._fade.setEndValue(float(target))
        self._fade.start()

    def _handle_fade_value(self, value: object) -> None:
        self._opacity = float(value)  # type: ignore[arg-type]
        self.update()

    def _handle_fade_finished(self) -> None:
        if self._opacity > 0.0:
            return
        # Now that nothing is on screen, it is safe to forget what was.
        self._snapshot = None
        self._backdrop = None
        self._confirming = False
        self._payload = DropPayload()
        self.hide()

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        if self._opacity <= 0.0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setOpacity(self._opacity)

        # The overlay runs to the window's bottom edge, which is rounded. A
        # square scrim there paints over the corner and squares off the window.
        outline = _bottom_rounded_path(self.rect(), Theme.window_corner_radius)
        if self._backdrop is not None:
            painter.setClipPath(outline)
            painter.drawPixmap(self.rect(), self._backdrop)
            painter.setClipping(False)
        scrim = (
            Theme.color_drop_scrim_confirming_rgba
            if self._confirming
            else Theme.color_drop_scrim_rgba
        )
        # fillPath rather than fillRect: clipping is one-bit, so the scrim needs
        # to draw its own antialiased curve or the corner comes out stepped.
        painter.fillPath(outline, QColor(*scrim))

        if self._confirming:
            self._paint_confirmation(painter)
        else:
            self._paint_pill(painter)
            self._paint_zones(painter)
        painter.end()

    def _paint_pill(self, painter: QPainter) -> None:
        text = _count_text(self._payload.item_count)
        font = QFont(painter.font())
        font.setPixelSize(Theme.drop_pill_font_size)
        font.setWeight(QFont.Weight.Medium)
        painter.setFont(font)

        width = (
            painter.fontMetrics().horizontalAdvance(text)
            + Theme.drop_pill_padding_horizontal * 2
        )
        rect = QRectF(
            (self.width() - width) / 2.0,
            float(Theme.drop_pill_margin_top),
            float(width),
            float(Theme.drop_pill_height),
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255))
        painter.drawRoundedRect(rect, Theme.drop_pill_radius, Theme.drop_pill_radius)
        painter.setPen(QColor(0, 0, 0))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_zones(self, painter: QPainter) -> None:
        rects = self.zone_rects()
        readable = self._payload.can_read
        self._paint_zone(
            painter,
            rects[READ_ZONE],
            glyph=self._read_glyph if readable else self._read_glyph_disabled,
            title=t("dialog.drop_read_title"),
            subtitle=_read_subtitle(self._payload),
            enabled=readable,
            hover=self._hover_progress[READ_ZONE],
        )
        self._paint_zone(
            painter,
            rects[IMPORT_ZONE],
            glyph=self._import_glyph,
            title=t("dialog.drop_import_title"),
            subtitle=t("dialog.drop_import_subtitle"),
            enabled=True,
            hover=self._hover_progress[IMPORT_ZONE],
        )

    def _paint_zone(
        self,
        painter: QPainter,
        rect: QRect,
        *,
        glyph: QPixmap,
        title: str,
        subtitle: str,
        enabled: bool,
        hover: float,
    ) -> None:
        if rect.width() <= 0 or rect.height() <= 0:
            return

        # A disabled zone holds its dimmed border outright -- easing toward the
        # hover colour would animate a promise it cannot keep.
        if not enabled:
            border_color = QColor(*Theme.color_drop_zone_border_disabled_rgba)
        else:
            border_color = _blend(
                Theme.color_drop_zone_border_rgba,
                Theme.color_drop_zone_border_hover_rgba,
                hover,
            )
        fill_color = _blend(
            Theme.color_drop_zone_fill_rgba,
            Theme.color_drop_zone_fill_hover_rgba,
            hover,
        )

        pen = QPen(border_color)
        pen.setWidth(Theme.drop_zone_border_width)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(fill_color)
        inset = Theme.drop_zone_border_width / 2.0
        painter.drawRoundedRect(
            QRectF(rect).adjusted(inset, inset, -inset, -inset),
            Theme.drop_zone_radius,
            Theme.drop_zone_radius,
        )

        # Lay the disc, title, and subtitle out as one stack centred in the zone
        # so a short zone crops symmetrically instead of pushing text off one end.
        diameter = Theme.drop_zone_icon_diameter
        gap = Theme.drop_zone_content_gap
        title_height = Theme.drop_zone_title_font_size + 6
        subtitle_height = Theme.drop_zone_subtitle_font_size + 6
        total = diameter + gap + title_height + gap + subtitle_height
        top = rect.center().y() - total / 2.0

        scale = 1.0 + (Theme.drop_zone_icon_hover_scale - 1.0) * (hover if enabled else 0.0)
        scaled = diameter * scale
        disc = QRectF(
            rect.center().x() - scaled / 2.0,
            top + (diameter - scaled) / 2.0,
            scaled,
            scaled,
        )
        disc_fill = (
            Theme.color_drop_zone_icon_fill_rgba
            if enabled
            else Theme.color_drop_zone_icon_fill_disabled_rgba
        )
        disc_border = (
            Theme.color_drop_zone_icon_border_rgba
            if enabled
            else Theme.color_drop_zone_icon_border_disabled_rgba
        )
        disc_pen = QPen(QColor(*disc_border))
        disc_pen.setWidth(1)
        disc_pen.setStyle(Qt.PenStyle.SolidLine)
        painter.setPen(disc_pen)
        painter.setBrush(QColor(*disc_fill))
        painter.drawEllipse(disc)
        _draw_centered_pixmap(painter, disc, glyph)

        title_rect = QRectF(
            rect.left(), top + diameter + gap, rect.width(), float(title_height)
        )
        title_font = QFont(painter.font())
        title_font.setPixelSize(Theme.drop_zone_title_font_size)
        title_font.setWeight(QFont.Weight.Bold)
        painter.setFont(title_font)
        painter.setPen(
            QColor(
                *(
                    Theme.color_drop_zone_title_rgba
                    if enabled
                    else Theme.color_drop_zone_title_disabled_rgba
                )
            )
        )
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignCenter, title)

        subtitle_width = min(rect.width(), Theme.drop_zone_subtitle_max_width)
        subtitle_rect = QRectF(
            rect.center().x() - subtitle_width / 2.0,
            title_rect.bottom() + gap,
            float(subtitle_width),
            float(subtitle_height),
        )
        subtitle_font = QFont(painter.font())
        subtitle_font.setPixelSize(Theme.drop_zone_subtitle_font_size)
        subtitle_font.setWeight(QFont.Weight.Normal)
        painter.setFont(subtitle_font)
        painter.setPen(
            QColor(
                *(
                    Theme.color_drop_zone_subtitle_rgba
                    if enabled
                    else Theme.color_drop_zone_subtitle_disabled_rgba
                )
            )
        )
        painter.drawText(subtitle_rect, Qt.AlignmentFlag.AlignCenter, subtitle)

    def _paint_confirmation(self, painter: QPainter) -> None:
        diameter = Theme.drop_confirm_icon_diameter
        gap = Theme.drop_confirm_gap
        label_height = Theme.drop_confirm_font_size + 6
        total = diameter + gap + label_height
        top = self.rect().center().y() - total / 2.0

        disc = QRectF(
            self.rect().center().x() - diameter / 2.0, top, float(diameter), float(diameter)
        )
        pen = QPen(QColor(*Theme.color_drop_confirm_border_rgba))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.setBrush(QColor(*Theme.color_drop_confirm_fill_rgba))
        painter.drawEllipse(disc)
        _draw_centered_pixmap(painter, disc, self._confirm_glyph)

        font = QFont(painter.font())
        font.setPixelSize(Theme.drop_confirm_font_size)
        font.setWeight(QFont.Weight.Medium)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(
            QRectF(0, top + diameter + gap, float(self.width()), float(label_height)),
            Qt.AlignmentFlag.AlignCenter,
            _confirm_text(self._confirm_count),
        )


def _blend(start: tuple[int, ...], end: tuple[int, ...], amount: float) -> QColor:
    """Interpolate two RGBA theme tuples, clamped to [0, 1]."""

    amount = min(1.0, max(0.0, amount))
    return QColor(*(round(a + (b - a) * amount) for a, b in zip(start, end)))


def _bottom_rounded_path(rect: QRect, radius: float) -> QPainterPath:
    """The overlay's outline: square along the top, rounded at the bottom.

    The overlay starts below the title bar, so only the bottom corners have to
    meet the window's radius -- the top edge butts against a square seam.

    Built as one continuous subpath. Unioning an ``addRoundedRect`` with an
    ``addRect`` over the top half is the obvious shortcut and is wrong:
    overlapping subpaths cancel under Qt's fill rule, the same trap
    ``reader_controls._top_rounded_path`` documents.
    """

    bounds = QRectF(rect)
    radius = min(float(radius), bounds.width() / 2.0, bounds.height() / 2.0)
    path = QPainterPath()
    path.moveTo(bounds.left(), bounds.top())
    path.lineTo(bounds.right(), bounds.top())
    path.lineTo(bounds.right(), bounds.bottom() - radius)
    path.quadTo(bounds.right(), bounds.bottom(), bounds.right() - radius, bounds.bottom())
    path.lineTo(bounds.left() + radius, bounds.bottom())
    path.quadTo(bounds.left(), bounds.bottom(), bounds.left(), bounds.bottom() - radius)
    path.closeSubpath()
    return path


def _draw_centered_pixmap(painter: QPainter, within: QRectF, pixmap: QPixmap) -> None:
    if pixmap.isNull():
        return
    ratio = pixmap.devicePixelRatio() or 1.0
    width = pixmap.width() / ratio
    height = pixmap.height() / ratio
    painter.drawPixmap(
        QRectF(
            within.center().x() - width / 2.0,
            within.center().y() - height / 2.0,
            width,
            height,
        ),
        pixmap,
        QRectF(pixmap.rect()),
    )


def _blurred(source: QPixmap, radius: float) -> QPixmap:
    """Approximate a Gaussian blur by downsampling and scaling back up.

    ``QGraphicsBlurEffect`` cannot be applied to a bare pixmap without staging a
    ``QGraphicsScene`` around it. A smooth round trip through a smaller pixmap
    reads the same behind a scrim at these radii and is a fraction of the code.
    """

    if radius <= 0 or source.isNull():
        return source
    shrink = max(1, int(radius * 2))
    small = source.scaled(
        max(1, source.width() // shrink),
        max(1, source.height() // shrink),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    blurred = small.scaled(
        source.size(),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    blurred.setDevicePixelRatio(source.devicePixelRatio())
    return blurred


def _count_text(count: int) -> str:
    if count <= 1:
        return t("dialog.drop_count_one")
    return t("dialog.drop_count_many", count=str(count))


def _confirm_text(count: int) -> str:
    if count <= 1:
        return t("dialog.drop_confirm_importing_one")
    return t("dialog.drop_confirm_importing_many", count=str(count))


def _read_subtitle(payload: DropPayload) -> str:
    reason = payload.read_unavailable
    if reason is ReadUnavailable.FOLDER:
        return t("dialog.drop_read_blocked_folder")
    if reason is ReadUnavailable.MULTIPLE_ITEMS:
        return t("dialog.drop_read_blocked_multiple")
    return t("dialog.drop_read_subtitle")


__all__ = ["DropZoneOverlay", "IMPORT_ZONE", "READ_ZONE", "payload_from_mime_urls"]
