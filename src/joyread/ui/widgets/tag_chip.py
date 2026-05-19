"""Chip widget for the Settings -> Tags management surface."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal as QtSignal
from PySide6.QtGui import QFontMetrics, QMouseEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget

from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.elided_label import ElidedLabel


class TagChipWidget(QFrame):
    """A single tag chip.

    Emits ``chip_clicked(tag_id, additive)`` on left-click, where
    ``additive`` is ``True`` when Shift is held — same selection
    convention as :class:`BookCardWidget`. Use ``as_add_chip(parent)``
    for the special "+" entry that lives at the end of the flow.
    """

    chip_clicked = QtSignal(str, bool)
    add_clicked = QtSignal()

    def __init__(
        self,
        tag_id: str,
        name: str,
        parent: QWidget | None = None,
        *,
        is_add_chip: bool = False,
    ) -> None:
        super().__init__(parent)
        self._tag_id = tag_id
        self._name = name
        self._is_add_chip = is_add_chip
        self._pressed_inside = False
        self._selected = False
        self.setProperty("class", "TagChip")
        self.setProperty("selected", "false")
        if is_add_chip:
            self.setProperty("addChip", "true")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(Theme.tag_chip_min_width)
        self.setMaximumWidth(Theme.tag_chip_max_width)
        self.setFixedHeight(Theme.tag_chip_height)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.tag_chip_padding_horizontal,
            Theme.tag_chip_padding_vertical,
            Theme.tag_chip_padding_horizontal,
            Theme.tag_chip_padding_vertical,
        )
        layout.setSpacing(0)

        if is_add_chip:
            # Special "+" entry: a plain QLabel keeps the centre glyph
            # crisp at any chip width.
            self._label: QLabel = QLabel(name)
            self._label.setProperty("class", "TagChipLabel")
            self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        else:
            self._label = ElidedLabel(name, max_lines=1)
            self._label.setProperty("class", "TagChipLabel")
            self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Set the font in code (rather than relying on QSS) so the chip's
        # sizeHint — which is consulted by FlowLayout before QSS is
        # polished — computes the right width. Otherwise the chip is
        # measured with the default Qt font (smaller), the FlowLayout
        # caches that width, then QSS applies the larger font and
        # ElidedLabel ends up clipping ("Comedy" -> "Co...").
        font = self._label.font()
        font.setPixelSize(Theme.tag_chip_font_size)
        self._label.setFont(font)
        layout.addWidget(self._label, stretch=1)

    @classmethod
    def as_add_chip(cls, parent: QWidget | None = None) -> "TagChipWidget":
        return cls(tag_id="", name="+", parent=parent, is_add_chip=True)

    @property
    def tag_id(self) -> str:
        return self._tag_id

    @property
    def is_add_chip(self) -> bool:
        return self._is_add_chip

    @property
    def selected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        if selected == self._selected:
            return
        self._selected = selected
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def set_name(self, name: str) -> None:
        self._name = name
        if isinstance(self._label, ElidedLabel):
            self._label.set_full_text(name)
        else:
            self._label.setText(name)
        self.updateGeometry()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API override.
        # ElidedLabel's sizeHint reports width 0 to allow shrink-to-fit,
        # which leaves the chip pinned at min_width inside a FlowLayout.
        # Compute the natural text width here and clamp to [min, max].
        metrics = QFontMetrics(self._label.font())
        text_width = metrics.horizontalAdvance(self._name)
        padding = 2 * Theme.tag_chip_padding_horizontal
        border = 2 * Theme.tag_chip_border_width
        desired = text_width + padding + border
        clamped = max(Theme.tag_chip_min_width, min(Theme.tag_chip_max_width, desired))
        return QSize(clamped, Theme.tag_chip_height)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API override.
        return QSize(Theme.tag_chip_min_width, Theme.tag_chip_height)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed_inside = True
            event.accept()
            return
        self._pressed_inside = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed_inside:
            self._pressed_inside = False
            event.accept()
            if not self.rect().contains(event.position().toPoint()):
                return
            if self._is_add_chip:
                self.add_clicked.emit()
                return
            additive = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self.chip_clicked.emit(self._tag_id, additive)
            return
        self._pressed_inside = False
        super().mouseReleaseEvent(event)
