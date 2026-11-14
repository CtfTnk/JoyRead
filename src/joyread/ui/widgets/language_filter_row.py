"""Language chips shown above the tag browser in the Library filter."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping

from PySide6.QtCore import Signal as QtSignal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from joyread.core.models.language import Language
from joyread.infrastructure.i18n.locale_service import book_language_display_name, t
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.tag_browser import TagBrowserSectionHeader
from joyread.ui.widgets.tag_chip import TagChipWidget


class LanguageFilterRow(QWidget):
    """Render-only: the owning dialog decides selection and supplies counts.

    Every language keeps its place even at zero books, so the chips do not
    shift under the pointer as the tag selection changes the counts.
    """

    language_clicked = QtSignal(str, bool)

    def __init__(self, languages: Iterable[Language], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("LanguageFilterRow")
        self.setFixedHeight(Theme.book_filter_language_row_height)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(Theme.tag_browser_group_gap)

        self._header = TagBrowserSectionHeader(t("tags.filter_language_header"))
        self._header.setFixedHeight(Theme.book_filter_language_header_height)
        root.addWidget(self._header)

        chips = QHBoxLayout()
        chips.setContentsMargins(0, 0, 0, 0)
        chips.setSpacing(Theme.tag_chip_gap)
        self._chips: dict[str, TagChipWidget] = {}
        for language in languages:
            chip = TagChipWidget(
                language.iso_code,
                book_language_display_name(language.iso_code, language.plain_text),
                show_count=True,
            )
            chip.chip_clicked.connect(self.language_clicked.emit)
            chips.addWidget(chip)
            self._chips[language.iso_code] = chip
        chips.addStretch(1)
        root.addLayout(chips)

    @property
    def chips(self) -> dict[str, TagChipWidget]:
        return dict(self._chips)

    def set_state(self, selected_codes: Collection[str], counts: Mapping[str, int]) -> None:
        for code, chip in self._chips.items():
            chip.set_selected(code in selected_codes)
            chip.set_count(counts.get(code, 0))
        self._header.set_trailing(
            t(
                "tags.tray_count",
                count=str(len(selected_codes)),
                total=str(len(self._chips)),
            )
        )


__all__ = ["LanguageFilterRow"]
