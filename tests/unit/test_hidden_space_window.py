"""Hidden Space in the Library window: the launch prompt, the dialogs, and locking.

These run a real Library (``create_app_context`` under a temporary runtime
directory) because the behaviour under test is the hand-off between the
window, the ViewModels, the task service and the vault. The vault's scrypt
cost is lowered so each unlock takes milliseconds, not a fifth of a second.
"""

from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit
import pytest
from shiboken6 import isValid

from joyread.app.app_context import create_app_context
from joyread.core.services.hidden_space_service import HiddenSpaceState
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.views.main_window import MainWindow
from joyread.ui.viewmodels.shelf_viewmodel import ShelfKey
from joyread.ui.widgets.dialogs import DialogRecoveryCodeContent
from tests.support.hidden_space_fakes import seed_legacy_password
from tests.support.vault_library import FAST_SCRYPT

PASSPHRASE = "Pass1234"


def apply_theme() -> None:
    QApplication.instance().setStyleSheet(ResourceLoader().load_stylesheet())


@pytest.fixture
def context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    apply_theme()
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path / "runtime"))
    source = tmp_path / "Night Notes.cbz"
    page = tmp_path / "page.png"
    Image.new("RGB", (20, 30), "#336699").save(page, format="PNG")
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(page, "001.png")
    context = create_app_context()
    context.import_service.import_files([source])
    context.library_runtime.vault_service._scrypt_cost = FAST_SCRYPT
    context.test_windows = []
    yield context
    # MainWindow deletes itself on close, so the test owns closing it.
    for window in context.test_windows:
        if isValid(window) and window.isVisible():
            window.close()
    QApplication.processEvents()
    context.close()


def _book_id(context) -> str:  # noqa: ANN001
    context.shelf_viewmodel.load_books()
    return context.shelf_viewmodel.books[0].uuid


def _set_up_vault(context, *, shown: bool, locked: bool) -> None:  # noqa: ANN001
    service = context.hidden_space_service
    draft = service.prepare_vault(PASSPHRASE, PASSPHRASE)
    service.create_vault(draft, draft.recovery_code.formatted())
    context.settings_store.update(show_hidden_collection=shown)
    if locked:
        service.lock()
    context.settings_viewmodel.handle_hidden_space_locked()  # Re-read the preference.


def _storage_kind(context, book: str) -> str:  # noqa: ANN001
    row = context.library_runtime.database_interpreter.execute(
        lambda connection: connection.execute(
            "SELECT storage_kind FROM book_files WHERE file_id = (SELECT file_id FROM books WHERE book_id = ?)",
            (book,),
        ).fetchone()
    )
    return row["storage_kind"]


def _wait_sealed(qtbot, context, book: str) -> None:  # noqa: ANN001
    sealing = context.library_runtime.sealing_service
    qtbot.waitUntil(
        lambda: _storage_kind(context, book) == "sealed" and not sealing.jobs_by_book(), timeout=10_000
    )


def _window(qtbot, context) -> MainWindow:  # noqa: ANN001
    window = MainWindow(context)
    context.test_windows.append(window)
    window.resize(1200, 860)
    window.show()
    QApplication.processEvents()
    return window


def _settle(qtbot, window: MainWindow) -> None:
    """Wait for a progress panel to give way to whatever comes next."""

    qtbot.waitUntil(lambda: not window.dialog_overlay.panel.is_showing_progress(), timeout=10_000)
    QApplication.processEvents()


def _fields(window: MainWindow) -> list[QLineEdit]:
    return window.dialog_overlay.findChildren(QLineEdit)


def _accept(window: MainWindow) -> None:
    window.dialog_overlay._accept()
    QApplication.processEvents()


# -- the launch prompt -----------------------------------------------------------


def test_the_launch_prompt_appears_only_when_hidden_space_was_left_shown(qtbot, context) -> None:  # noqa: ANN001
    _set_up_vault(context, shown=False, locked=True)
    window = _window(qtbot, context)
    assert window._lock_overlay is None
    window.close()

    context.settings_viewmodel.set_show_hidden_collection(True)
    window = _window(qtbot, context)
    assert window._lock_overlay is not None and not window._lock_overlay.isHidden()
    window.close()


def test_unlocking_from_the_prompt_runs_off_the_ui_thread_and_reports_back(qtbot, context) -> None:  # noqa: ANN001
    _set_up_vault(context, shown=True, locked=True)
    window = _window(qtbot, context)
    overlay = window._lock_overlay

    overlay._password.setText("not it")
    overlay._on_verify_clicked()
    assert overlay.is_busy  # The answer comes later, from the task service.
    qtbot.waitUntil(lambda: not overlay.is_busy, timeout=10_000)
    assert window._lock_overlay is overlay
    assert overlay._state_label.text() == "Incorrect passphrase."
    assert context.settings_viewmodel.hidden_space_state is HiddenSpaceState.LOCKED

    overlay._password.setText(PASSPHRASE)
    overlay._on_verify_clicked()
    qtbot.waitUntil(lambda: window._lock_overlay is None, timeout=10_000)
    assert context.settings_viewmodel.hidden_space_visible
    assert context.shelf_viewmodel.show_hidden_collection
    window.close()


def test_hide_on_the_prompt_turns_show_collections_off_and_stays_locked(qtbot, context) -> None:  # noqa: ANN001
    _set_up_vault(context, shown=True, locked=True)
    window = _window(qtbot, context)

    window._lock_overlay._on_hide_clicked()

    assert window._lock_overlay is None
    assert context.settings_store.load().show_hidden_collection is False
    assert context.settings_viewmodel.hidden_space_state is HiddenSpaceState.LOCKED
    window.close()


def test_a_legacy_library_upgrades_from_the_prompt_with_its_old_password(qtbot, context) -> None:  # noqa: ANN001
    book = _book_id(context)
    context.library_runtime.book_repository.set_book_hidden(book, True)  # Soft-hidden earlier.
    seed_legacy_password(context.settings_store, "abcd")
    context.settings_viewmodel.set_show_hidden_collection(True)
    window = _window(qtbot, context)
    overlay = window._lock_overlay
    assert overlay is not None and overlay._recovery_link.isHidden()  # No code exists yet.

    overlay._password.setText("abcd")
    overlay._on_verify_clicked()
    qtbot.waitUntil(lambda: window._lock_overlay is None, timeout=10_000)

    # The password was right; now the recovery code, shown once and typed back.
    content = window.dialog_overlay.findChild(DialogRecoveryCodeContent)
    assert content is not None
    code = content.findChild(QLabel, "DialogRecoveryCode").text()
    _accept(window)
    _fields(window)[0].setText(code)
    _accept(window)
    _settle(qtbot, window)

    assert context.settings_viewmodel.hidden_space_state is HiddenSpaceState.UNLOCKED
    assert context.settings_store.load().hidden_space_password_hash is None
    _wait_sealed(qtbot, context, book)  # The upgrade encrypts what was soft-hidden.
    window.close()


# -- the dialogs ------------------------------------------------------------------


def test_setting_up_from_the_switch_cautions_a_short_passphrase_and_confirms_the_code(qtbot, context) -> None:  # noqa: ANN001
    window = _window(qtbot, context)
    window._hidden_space_flows.request_show()

    passphrase, confirm = _fields(window)
    passphrase.setText(PASSPHRASE)
    confirm.setText("Pass1235")
    _accept(window)
    assert window.dialog_overlay.isVisible()  # Kept open: they differ.
    confirm.setText(PASSPHRASE)
    _accept(window)

    # Eight characters: the caution, then on to preparing the vault.
    assert window.dialog_overlay.panel.title_text == "Short Passphrase"
    _accept(window)
    _settle(qtbot, window)
    content = window.dialog_overlay.findChild(DialogRecoveryCodeContent)
    code = content.findChild(QLabel, "DialogRecoveryCode").text()
    _accept(window)

    field = _fields(window)[0]
    field.setText(code.replace(" ", "")[:-1] + ("0" if code[-1] != "0" else "1"))
    _accept(window)
    assert window.dialog_overlay.isVisible()  # A wrong code keeps the confirmation open.
    field.setText(code.lower())  # Case and spacing are forgiven.
    _accept(window)
    _settle(qtbot, window)

    assert context.settings_viewmodel.hidden_space_state is HiddenSpaceState.UNLOCKED
    assert context.settings_viewmodel.hidden_space_visible
    assert window.sidebar._hidden_item.isVisibleTo(window.sidebar)
    window.close()


def test_cancelling_setup_stores_nothing(qtbot, context) -> None:  # noqa: ANN001
    window = _window(qtbot, context)
    window._hidden_space_flows.request_show()
    for field in _fields(window):
        field.setText("a long enough passphrase")
    _accept(window)
    _settle(qtbot, window)
    assert window.dialog_overlay.findChild(DialogRecoveryCodeContent) is not None

    window.dialog_overlay._reject()

    assert context.settings_viewmodel.hidden_space_state is HiddenSpaceState.NONE
    assert not context.settings_viewmodel.hidden_space_visible
    window.close()


# -- locking ------------------------------------------------------------------------


def test_locking_closes_the_embedded_reader_on_a_hidden_book_and_forgets_covers(qtbot, context) -> None:  # noqa: ANN001
    book = _book_id(context)
    _set_up_vault(context, shown=True, locked=False)
    context.settings_viewmodel.handle_hidden_space_locked()
    window = _window(qtbot, context)
    assert context.shelf_viewmodel.show_hidden_collection
    context.shelf_viewmodel.hide_books((book,))
    _wait_sealed(qtbot, context, book)
    context.shelf_viewmodel.load_books()
    context.shelf_viewmodel.set_current_shelf(ShelfKey.HIDDEN.value)
    hidden = next(item for item in context.shelf_viewmodel.books if item.uuid == book)
    assert hidden.is_hidden and hidden.file_path.endswith(".jrl")

    window.open_reader_for_book(book)
    assert window._embedded_reader is not None

    context.settings_viewmodel.set_show_hidden_collection(False)  # Show Collections off.
    qtbot.waitUntil(lambda: window._embedded_reader is None, timeout=5_000)

    assert context.settings_viewmodel.hidden_space_state is HiddenSpaceState.LOCKED
    assert context.shelf_viewmodel.current_shelf == ShelfKey.ALL.value
    assert not any(cover.is_in_memory for cover in context.shelf_viewmodel.covers.values())
    assert context.library_runtime.sealed_cover_store.cached_bytes == 0
    window.close()


def test_closing_the_library_window_locks_but_keeps_the_preference(qtbot, context) -> None:  # noqa: ANN001
    _set_up_vault(context, shown=True, locked=False)
    window = _window(qtbot, context)

    window.close()

    assert context.settings_viewmodel.hidden_space_state is HiddenSpaceState.LOCKED
    assert context.settings_store.load().show_hidden_collection is True
