"""Reusable labels that hide overflowing text without changing layout size."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFontMetrics, QResizeEvent, QTextLayout, QTextOption
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget

from joyread.ui.resources.styles.theme import Theme


class ElidedLabel(QLabel):
    """Label that exposes full text only when it is visually clipped."""

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        elide_mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
        max_lines: int = 1,
        reserve_full_height: bool = False,
    ) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._elide_mode = elide_mode
        self._max_lines = max(1, max_lines)
        self._reserve_full_height = reserve_full_height
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        # We compute line breaks ourselves and insert "\n" — letting QLabel
        # word-wrap on top of that produces a phantom 3rd row during resize
        # whenever its actual paint width disagrees with our measurement.
        self.setWordWrap(False)
        self.set_full_text(text)

    @property
    def full_text(self) -> str:
        return self._full_text

    @property
    def max_lines(self) -> int:
        return self._max_lines

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API override.
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self._refresh_elision()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_elision()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API override.
        hint = super().sizeHint()
        return QSize(0, hint.height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API override.
        hint = super().minimumSizeHint()
        return QSize(0, hint.height())

    def _refresh_elision(self) -> None:
        metrics = QFontMetrics(self.font())
        available_width = max(0, self.contentsRect().width())
        if self._max_lines == 1:
            display_text = metrics.elidedText(self._full_text, self._elide_mode, available_width)
            is_clipped = display_text != self._full_text
            used_lines = 1
        else:
            display_text, is_clipped, used_lines = self._multi_line_elided_text(metrics, available_width)
        self._sync_fixed_height(metrics, used_lines)
        QLabel.setText(self, display_text)
        # Only show the hint when there is hidden content. This keeps short
        # titles from producing redundant hover chrome.
        self.setToolTip(self._full_text if is_clipped else "")

    def _sync_fixed_height(self, metrics: QFontMetrics, used_lines: int) -> None:
        guard = Theme.elided_label_clip_guard if self._max_lines > 1 else 0
        effective_lines = self._max_lines if self._reserve_full_height else max(1, used_lines)
        self.setFixedHeight((metrics.lineSpacing() * effective_lines) + guard)

    def _multi_line_elided_text(self, metrics: QFontMetrics, available_width: int) -> tuple[str, bool, int]:
        if available_width <= 0:
            return "", bool(self._full_text), 1

        source = self._full_text.replace("\n", " ")
        text_layout = QTextLayout(source, self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        text_layout.setTextOption(option)

        line_ranges: list[tuple[int, int]] = []
        text_layout.beginLayout()
        try:
            while True:
                line = text_layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(available_width)
                line_ranges.append((line.textStart(), line.textLength()))
        finally:
            text_layout.endLayout()

        if not line_ranges:
            return "", False, 1

        if len(line_ranges) <= self._max_lines:
            joined = "\n".join(source[start : start + length].strip() for start, length in line_ranges)
            return joined, False, len(line_ranges)

        visible_lines = [
            source[start : start + length].strip()
            for start, length in line_ranges[: self._max_lines - 1]
        ]
        final_start = line_ranges[self._max_lines - 1][0]
        final_source = source[final_start:].strip()
        visible_lines.append(metrics.elidedText(final_source, self._elide_mode, available_width))
        return "\n".join(visible_lines), True, self._max_lines
