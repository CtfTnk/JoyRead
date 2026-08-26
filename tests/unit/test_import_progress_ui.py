"""What the user sees while an import runs.

Conversion re-reads and rewrites every page, so an import that used to finish in
a blink can now take a while. A silent wait was acceptable when import was a
file copy; it is not once the work is this long.
"""

from __future__ import annotations

from pathlib import Path

from joyread.core.services.import_service import ImportProgress, ImportStage
from joyread.infrastructure.i18n import locale_service
from joyread.ui.views.main_window import _import_progress_message
from joyread.ui.widgets.dialogs import JoyReadDialogOverlay


def _event(stage: ImportStage, **overrides) -> ImportProgress:
    fields = dict(
        stage=stage,
        source_path="/somewhere/deep/My Book.cbz",
        item_index=1,
        item_count=3,
        unit_done=0,
        unit_total=0,
    )
    fields.update(overrides)
    return ImportProgress(**fields)


def test_the_message_names_the_book_and_its_place_in_the_batch() -> None:
    message = _import_progress_message(_event(ImportStage.STAGING))

    assert "My Book.cbz" in message
    assert "/somewhere/deep" not in message  # a full path would overflow the dialog
    assert "2" in message and "3" in message  # item_index is 0-based, shown 1-based


def test_only_conversion_reports_a_page_count() -> None:
    """The other stages have no honest denominator, so they claim none."""

    converting = _import_progress_message(
        _event(ImportStage.CONVERTING, unit_done=7, unit_total=20)
    )
    inspecting = _import_progress_message(_event(ImportStage.INSPECTING))

    assert "7" in converting and "20" in converting
    assert not any(character.isdigit() for character in inspecting.splitlines()[-1])


def test_every_stage_has_a_translated_label_in_every_locale() -> None:
    """A missing key renders as the raw key, which is how "set…ify" shipped."""

    for locale in ("English", "Japanese", "Chinese"):
        locale_service.load_language(locale)
        for stage in ImportStage:
            message = _import_progress_message(_event(stage, unit_done=1, unit_total=2))
            assert "dialog." not in message, (locale, stage)
    locale_service.load_language("English")


# ----------------------------------------------------------------------
# The dialog itself
# ----------------------------------------------------------------------


def test_progress_updates_in_place_rather_than_rebuilding_the_dialog(qtbot) -> None:
    overlay = JoyReadDialogOverlay()
    qtbot.addWidget(overlay)
    overlay.resize(800, 600)
    overlay.show_progress("Importing", "Preparing")
    content = overlay.panel._content_widget

    one_line_height = overlay.panel.height()

    overlay.update_progress("2 of 4\nA Book.cbz\nConverting page 3 of 9")

    assert overlay.panel._content_widget is content  # same widget, new text
    assert "Converting page 3 of 9" in content._label.text()
    # The panel sized itself for the one-line placeholder, so a taller running
    # message has to re-run that sizing or it draws clipped.
    assert overlay.panel.height() > one_line_height


def test_a_progress_dialog_offers_nothing_to_click(qtbot) -> None:
    """The work is already running; there is no decision for the user to make."""

    from joyread.ui.widgets.dialogs import DialogTextButton

    overlay = JoyReadDialogOverlay()
    qtbot.addWidget(overlay)
    overlay.show_progress("Importing", "Preparing")

    assert overlay.panel.findChildren(DialogTextButton) == []


def test_progress_arriving_after_the_dialog_closed_is_ignored(qtbot) -> None:
    """Progress comes from a worker thread, so a late callback is normal.

    The summary dialog replaces the progress content and deletes it; an update
    that still held a reference to that widget would raise from deep inside Qt.
    """

    overlay = JoyReadDialogOverlay()
    qtbot.addWidget(overlay)
    overlay.show_progress("Importing", "Preparing")
    overlay.show_info("Import finished", "2 imported")

    overlay.update_progress("Converting page 4 of 9")

    assert "2 imported" in overlay.panel._content_widget._label.text()

    overlay.hide()
    overlay.update_progress("Converting page 5 of 9")  # must not raise


def test_the_dialog_does_not_creep_as_progress_updates(qtbot) -> None:
    """Sizing has to be idempotent, because progress calls it once per page.

    ``set_available_width`` measures the label's ``sizeHint``, which on an
    already-clamped widget reports the previous clamp -- so every call re-added
    the clip guard and the panel grew 2px a tick. Over a 500-page conversion
    that is a dialog taller than the window.
    """

    overlay = JoyReadDialogOverlay()
    qtbot.addWidget(overlay)
    overlay.resize(900, 600)
    overlay.show_progress("Importing", "Preparing…")

    sizes = set()
    for page in range(1, 60):
        overlay.update_progress(f"1 of 1\nBook.cbz\nConverting page {page} of 500")
        sizes.add((overlay.panel.width(), overlay.panel.height()))

    assert len(sizes) == 1


def test_escape_does_not_dismiss_a_running_progress_dialog(qtbot) -> None:
    """Clearing the reject callback stops the callback, not the hide.

    Escape used to make the dialog vanish while the import carried on, leaving
    the user with no progress and no way back to it — and ``update_progress``
    then went quiet, because it only writes to a visible dialog. Until there is
    a cancel control, doing nothing is the honest response.
    """

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    overlay = JoyReadDialogOverlay()
    qtbot.addWidget(overlay)
    overlay.resize(800, 600)
    overlay.show_progress("Importing", "Preparing…")

    QApplication.sendEvent(
        overlay,
        QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier),
    )

    assert overlay.isVisible()
    overlay.update_progress("Converting page 2 of 9")
    assert "Converting page 2 of 9" in overlay.panel._content_widget._label.text()

    # An ordinary dialog still closes on Escape.
    overlay.show_info("Done", "2 imported")
    QApplication.sendEvent(
        overlay,
        QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier),
    )
    assert not overlay.isVisible()


def test_the_extracting_stage_has_a_label_in_every_locale() -> None:
    """A missing key renders as the raw key, which is how "set…ify" shipped."""

    for locale in ("English", "Japanese", "Chinese"):
        locale_service.load_language(locale)
        message = _import_progress_message(_event(ImportStage.EXTRACTING))
        assert "dialog." not in message, locale
    locale_service.load_language("English")
