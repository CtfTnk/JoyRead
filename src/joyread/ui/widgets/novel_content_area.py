"""Scrollable placeholder content area for the novel reader skeleton.

The widget owns a long lorem-ipsum body so the footer slider can be wired
to its vertical scrollbar end-to-end. When the EPUB engine lands, the
content widget is replaced; the slider/indicator wiring on the shell
stays the same as long as the new widget exposes the same signals and
``set_scroll_percentage`` API.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QEvent, QPoint, Qt, Signal as QtSignal
from PySide6.QtGui import QFont, QMouseEvent
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget

from joyread.ui.resources.styles.theme import Theme


logger = logging.getLogger(__name__)


_PLACEHOLDER_PARAGRAPHS: tuple[str, ...] = tuple(
    f"Chapter {chapter_index + 1} — placeholder. The novel reader skeleton "
    "renders this lorem-ipsum body so the chrome can be exercised before the "
    "real EPUB engine lands. Scrolling here updates the footer slider, and "
    "dragging the slider scrolls this content area. "
    + (
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Praesent "
        "in mauris eu tortor porttitor accumsan. Mauris suscipit, ligula "
        "sit amet pharetra semper, nibh ante cursus purus, vel sagittis "
        "velit mauris vel metus. Aenean fermentum risus id tortor. Integer "
        "id quam. Morbi mi. Quisque nisl felis, venenatis tristique, "
        "dignissim in, ultrices sit amet, augue. Proin sodales libero eget "
        "ante. Nulla quam. Aenean laoreet. Vestibulum nisi lectus, commodo "
        "ac, facilisis ac, ultricies eu, pede. Ut orci risus, accumsan "
        "porttitor, cursus quis, aliquet eget, justo. "
    ) * 3
    for chapter_index in range(8)
)


class NovelContentArea(QScrollArea):
    """Right-rail scrollable placeholder; emits scroll fraction on change.

    Mirrors ``ReaderCanvas``'s mouse signal surface (mouse_moved /
    left_clicked / right_clicked) so the novel shell can reuse the same
    edge-reveal + wake-on-right-click chrome wiring as the manga shell.
    """

    scroll_changed = QtSignal(float)
    mouse_moved = QtSignal(QPoint)
    left_clicked = QtSignal()
    right_clicked = QtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("NovelContentArea")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        # Footer slider is the canonical progress UI; hiding the inner
        # scrollbar avoids two scroll-position indicators on one axis.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Let the shell's rounded background show through the corners
        # instead of being overpainted by an opaque viewport.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("QScrollArea { background: transparent; }")
        self.viewport().setAutoFillBackground(False)
        self.viewport().setStyleSheet("background: transparent;")
        # No native text-context menu on the placeholder body — the
        # shell uses right-click to wake the chrome instead.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.viewport().setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.viewport().setMouseTracking(True)
        # Forwarding mouse signals via a viewport event filter keeps the
        # default scroll-area drag/wheel paths intact (filter returns False).
        self.viewport().installEventFilter(self)
        # Re-entrancy guard: the shell wires the slider's ``valueChanged`` to
        # ``set_scroll_percentage``, and we emit ``scroll_changed`` from the
        # scrollbar's ``valueChanged``. Without this guard the two would
        # ping-pong each other on a single user interaction.
        self._suppress_scroll_signal = False
        self._font_size = Theme.novel_placeholder_font_size
        self._custom_enabled = False

        self._content = QWidget()
        self._content.setObjectName("NovelContent")
        self._content.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._content.setAutoFillBackground(False)
        self._content.setStyleSheet("background: transparent;")
        self._content.setMouseTracking(True)
        layout = QVBoxLayout(self._content)
        layout.setContentsMargins(
            Theme.novel_content_margin,
            Theme.novel_content_margin,
            Theme.novel_content_margin,
            Theme.novel_content_margin,
        )
        layout.setSpacing(Theme.novel_content_margin // 2)

        self._status_label = QLabel("Novel engine not yet wired — showing placeholder text.")
        self._status_label.setObjectName("NovelContentStatus")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._body_label = QLabel("\n\n".join(_PLACEHOLDER_PARAGRAPHS))
        self._body_label.setObjectName("NovelContentBody")
        self._body_label.setWordWrap(True)
        # Skeleton: no text selection so right-click reaches the chrome
        # wiring. A real engine can re-enable a richer interaction model.
        self._body_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._body_label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self._body_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._body_label.setMaximumWidth(Theme.novel_content_max_width)
        self._body_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._body_label, stretch=1)
        layout.addStretch(1)

        self.setWidget(self._content)
        self.verticalScrollBar().valueChanged.connect(self._emit_scroll_changed)
        self._apply_font_size()

    def is_scrollable(self) -> bool:
        return self.verticalScrollBar().maximum() > 0

    def scroll_percentage(self) -> float:
        bar = self.verticalScrollBar()
        span = bar.maximum() - bar.minimum()
        if span <= 0:
            return 0.0
        return (bar.value() - bar.minimum()) / span

    def set_scroll_percentage(self, fraction: float) -> None:
        bar = self.verticalScrollBar()
        span = bar.maximum() - bar.minimum()
        if span <= 0:
            return
        value = bar.minimum() + int(round(max(0.0, min(1.0, fraction)) * span))
        if bar.value() == value:
            return
        self._suppress_scroll_signal = True
        try:
            bar.setValue(value)
        finally:
            self._suppress_scroll_signal = False

    def scroll_by_viewport(self, direction: int) -> None:
        bar = self.verticalScrollBar()
        step = max(1, self.viewport().height() - self._font_size * 2)
        bar.setValue(bar.value() + direction * step)

    def apply_enable_custom(self, enabled: bool) -> None:
        self._custom_enabled = enabled
        status = "Custom novel styling ON." if enabled else "Custom novel styling OFF."
        self._status_label.setText(status + " (Placeholder — engine wiring pending.)")
        logger.debug("NovelContentArea apply_enable_custom enabled=%s", enabled)

    def apply_font_size(self, size: int) -> None:
        self._font_size = max(8, min(72, int(size)))
        self._apply_font_size()
        logger.debug("NovelContentArea apply_font_size size=%s", self._font_size)

    def _apply_font_size(self) -> None:
        font: QFont = self._body_label.font()
        font.setPointSize(self._font_size)
        self._body_label.setFont(font)

    def _emit_scroll_changed(self, _value: int) -> None:
        if self._suppress_scroll_signal:
            return
        self.scroll_changed.emit(self.scroll_percentage())

    def eventFilter(self, watched: object, event: QEvent) -> bool:  # noqa: N802 - Qt API.
        if watched is self.viewport():
            if event.type() == QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
                # Forward in viewport-local coordinates so the shell's
                # edge-distance check uses the same origin as the manga
                # canvas (top-left of the visible area).
                self.mouse_moved.emit(event.position().toPoint())
            elif event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
                if event.button() == Qt.MouseButton.RightButton:
                    self.right_clicked.emit()
                elif event.button() == Qt.MouseButton.LeftButton:
                    self.left_clicked.emit()
        return False


def make_test_content(loader: Callable[[], str]) -> str:
    """Hook for future engine work; currently unused but documents intent."""
    return loader()


__all__ = ["NovelContentArea"]
