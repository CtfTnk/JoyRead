"""Reader continuity and deferred Library adoption across lifecycle boundaries."""

from io import BytesIO
from pathlib import Path
from threading import Event
from zipfile import ZipFile

import pytest
from PIL import Image

from joyread.app import app_context as context_module
from joyread.app.app_context import create_app_context
from joyread.app.open_policy import LibraryState
from joyread.app.windows.manager import ApplicationWindowManager
from joyread.app.windows.requests import StandaloneReaderRequest
from joyread.core.services.archive_cache_lease import ArchiveCacheLease, ArchiveCacheScope
from joyread.infrastructure.config.settings_store import SettingsStore
from joyread.ui.views.main_window import MainWindow
from joyread.ui.views.reader_window import ReaderWindow


def _store(root: Path) -> SettingsStore:
    return SettingsStore(support_root=root / "support", default_storage_root=root / "library")


def _archive(path: Path) -> Path:
    image = BytesIO()
    Image.new("RGB", (16, 24), "#336699").save(image, format="PNG")
    with ZipFile(path, "w") as archive:
        for index in range(3):
            archive.writestr(f"{index:03}.png", image.getvalue())
    return path


@pytest.mark.parametrize("phase", ("opening", "page", "during-transition"))
def test_external_reader_work_survives_storage_transition(tmp_path, qtbot, monkeypatch, phase):
    store = _store(tmp_path)
    store.update(page_prefetch_before=0, page_prefetch_after=0)
    context = create_app_context(settings_store=store)
    source = _archive(tmp_path / "external.cbz")
    entered = Event()
    release = Event()

    def delay(callback):
        def work(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return callback(*args, **kwargs)
        return work

    if phase == "opening":
        monkeypatch.setattr(context.reader_session_service, "open_document",
                            delay(context.reader_session_service.open_document))
    if phase == "during-transition":
        context.quiesce_for_storage_transition()
    window = ReaderWindow(context, source)
    try:
        window.show()
        if phase == "page":
            qtbot.waitUntil(lambda: 0 in window.canvas._pixmaps)
            document = window.viewmodel._document
            monkeypatch.setattr(document, "prepare_page", delay(document.prepare_page))
            window.viewmodel.seek(2)
        if phase != "during-transition":
            qtbot.waitUntil(entered.is_set)
            context.quiesce_for_storage_transition()
        else:
            qtbot.waitUntil(lambda: 0 in window.canvas._pixmaps)
        # A blocked external open/page job must neither delay nor be cancelled
        # by the Library drain. No Reader is reopened to recover its state.
        qtbot.waitUntil(lambda: context.storage_transition_pending_tasks() == 0)
        context.commit_storage_transition()
        context.reload_storage_from_settings()
        context.resume_after_storage_transition()
        release.set()
        expected_page = 2 if phase == "page" else 0
        qtbot.waitUntil(lambda: expected_page in window.canvas._pixmaps)
        assert window.viewmodel.page_count == 3
        assert not window.viewmodel.is_loading
        assert not window.viewmodel.error_message
    finally:
        release.set()
        window.close()
        context.close()


def test_promoted_managed_reader_is_flushed_and_closed_before_library_selection(tmp_path, qtbot):
    target_store = _store(tmp_path / "target")
    seed = create_app_context(settings_store=target_store)
    seed.close()
    context = create_app_context(settings_store=_store(tmp_path / "original"))
    source = _archive(tmp_path / "source.cbz")
    context.import_service.import_files([source])
    manager = ApplicationWindowManager(context)
    main = manager.show_library()
    book = context.shelf_viewmodel.books[0]
    reader = manager.open_reader_from_library(StandaloneReaderRequest(Path(book.file_path), book=book))
    closed = []
    reader.closed.connect(lambda: closed.append(True))
    try:
        qtbot.waitUntil(lambda: bool(reader.canvas._pixmaps))
        assert manager.open_files((book.file_path,)) == (reader,)
        assert manager._ownership.owner_of(id(reader)) is None
        assert reader in manager.library_reader_windows
        # Seed a progress change without waiting for its asynchronous save;
        # the real transition controller must flush it before closing Reader.
        reader.viewmodel.seek(2)
        saved = []
        real_set_progress = context.library_service.set_progress

        def record_progress(*args, **kwargs):
            result = real_set_progress(*args, **kwargs)
            saved.append(args)
            return result

        context.library_service.set_progress = record_progress
        controller = manager.storage_transition_controller
        assert controller.start(lambda: context.begin_storage_select(target_store.default_storage_root))
        qtbot.waitUntil(lambda: not controller.busy)
        assert closed == [True]
        assert not manager.reader_windows
        assert context.paths.storage_root == target_store.default_storage_root
        assert saved and saved[-1][0] == book.uuid
    finally:
        main.close()
        manager.close_all_readers()
        context.close()


@pytest.mark.parametrize("strategy", ("zip_bundle", "hidden_image_files"))
def test_storage_switch_preserves_active_private_cache_lease(tmp_path, qtbot, strategy):
    store = _store(tmp_path)
    store.update(archive_cache_strategy=strategy)
    context = create_app_context(settings_store=store)
    pool = context.archive_extraction_pool
    key = "external:sha256:private-reader"
    lease = ArchiveCacheLease(pool, key, ArchiveCacheScope.PERSISTENT)
    pool.mark_session_scoped(key)
    assert lease.put("001.png", b"decrypted page")
    try:
        context.quiesce_for_storage_transition()
        qtbot.waitUntil(lambda: context.storage_transition_pending_tasks() == 0)
        context.commit_storage_transition()
        store.update(storage_location=str(tmp_path / "other-library"))
        context.reload_storage_from_settings()
        context.resume_after_storage_transition()
        assert context.archive_extraction_pool is pool
        assert lease.get("001.png") == b"decrypted page"
        lease.close()
        assert pool.get(key, "001.png") is None
        assert pool.current_bytes == 0
    finally:
        lease.close()
        context.close()


def test_window_geometry_change_does_not_reload_deferred_library(tmp_path, qtbot, monkeypatch):
    entered = Event()
    release = Event()
    created = []
    factory = context_module.create_library_runtime

    def delayed(*args, **kwargs):
        runtime = factory(*args, **kwargs)
        created.append(runtime)
        entered.set()
        assert release.wait(5)
        return runtime

    monkeypatch.setattr(context_module, "create_library_runtime", delayed)
    store = _store(tmp_path)
    context = create_app_context(settings_store=store, defer_library=True)
    try:
        context.start_library_load()
        qtbot.waitUntil(entered.is_set)
        context.settings_viewmodel.remember_window_size("library", (1111, 777))
        context.reader_runtime.preferences.remember_window_size("reader", (999, 700))
        release.set()
        qtbot.waitUntil(lambda: context.library_state is LibraryState.READY)
        assert created == [context.library_runtime]
        assert store.load().library_window_size == (1111, 777)
        assert store.load().reader_window_size == (999, 700)
    finally:
        release.set()
        context.close()


def test_retry_revalidates_the_selected_candidate_and_preserves_saved_path(tmp_path, qtbot, monkeypatch):
    store = _store(tmp_path)
    candidate = tmp_path / "candidate"
    (candidate / "Database").mkdir(parents=True)
    (candidate / "Database" / "joyread.sqlite3").write_bytes(b"damaged")
    context = create_app_context(settings_store=store, defer_library=True)
    window = MainWindow(context)
    validated = []
    validate = context.storage_validation_service.validate_full

    def record_validation(path):
        validated.append(path)
        return validate(path)

    monkeypatch.setattr(context.storage_validation_service, "validate_full", record_validation)
    try:
        window.show()
        context.start_library_load(candidate)
        qtbot.waitUntil(lambda: context.library_state is LibraryState.FAILED)
        window.dialog_overlay.panel.accepted.emit()
        qtbot.waitUntil(lambda: context.library_state is LibraryState.FAILED)
        assert context.library_attempted_path == candidate
        assert validated == [candidate, candidate]
        assert store.load().storage_location == str(store.default_storage_root)
    finally:
        window.close()
        context.close()


def test_deferred_shelf_synchronizes_saved_view_and_sort_controls(tmp_path, qtbot):
    store = _store(tmp_path)
    store.update(shelf_view_mode="list", shelf_sort_field="Title", shelf_sort_ascending=True)
    context = create_app_context(settings_store=store, defer_library=True)
    window = MainWindow(context)
    try:
        window.show()
        context.start_library_load()
        qtbot.waitUntil(lambda: context.library_state is LibraryState.READY)
        assert window.chrome._list_mode_switch.value == "list"
        assert window.chrome._current_sort_field == context.shelf_viewmodel.sort_field
        assert window.chrome._sort_ascending is True
        window.chrome._list_mode_switch._buttons["grid"].click()
        assert context.shelf_viewmodel.view_mode.value == "grid"
    finally:
        window.close()
        context.close()
