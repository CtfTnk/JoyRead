"""P2 runtime ownership and construction boundaries."""

from __future__ import annotations

from dataclasses import fields
import os
from pathlib import Path
import subprocess
import sys

import pytest

from joyread.app.app_context import AppContext, create_app_context
from joyread.app.library_runtime import create_library_runtime
from joyread.app.reader_runtime import create_reader_runtime
from joyread.core.models.import_policy import CanonicalImportPolicy
from joyread.core.services.library_maintenance_service import LibraryMaintenanceService
from joyread.core.services.thumbnail_service import ThumbnailService
from joyread.infrastructure.config.app_config import AppConfig
from joyread.infrastructure.config.settings_store import SettingsStore
from joyread.infrastructure.database import DatabaseInterpreter


def _store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(
        support_root=tmp_path / "support",
        default_storage_root=tmp_path / "library",
    )


def test_reader_factory_and_window_imports_do_not_load_library_or_database(tmp_path) -> None:
    script = """
import sys
from pathlib import Path
from joyread.app.reader_runtime import create_reader_runtime
from joyread.infrastructure.config.app_config import AppConfig
from joyread.infrastructure.config.settings_store import SettingsStore

root = Path(sys.argv[1])
store = SettingsStore(support_root=root / 'support', default_storage_root=root / 'library')
reader = create_reader_runtime(AppConfig(), store.load(), store)
try:
    import joyread.ui.views.reader_window
    import joyread.app.windows.manager
    forbidden = [name for name in sys.modules if (
        name.startswith('joyread.core.repositories')
        or name.startswith('joyread.infrastructure.database')
        or name == 'joyread.ui.views.main_window'
    )]
    assert not forbidden, forbidden
    assert not list((root / 'library').rglob('*.sqlite3'))
finally:
    reader.close()
"""
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(root / "src"))
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("thumbnail_cleanup_fails", (False, True))
def test_library_factory_releases_partial_database_and_thumbnails(
    tmp_path, monkeypatch, thumbnail_cleanup_fails
) -> None:
    store = _store(tmp_path)
    reader = create_reader_runtime(AppConfig(), store.load(), store)
    closed: list[str] = []
    close_database = DatabaseInterpreter.close
    close_thumbnails = ThumbnailService.close

    def record_database(self):  # noqa: ANN001, ANN202
        closed.append("database")
        return close_database(self)

    def record_thumbnails(self):  # noqa: ANN001, ANN202
        closed.append("thumbnails")
        close_thumbnails(self)
        if thumbnail_cleanup_fails:
            raise RuntimeError("thumbnail close failed")

    def fail_recovery(self):  # noqa: ANN001, ANN202
        raise RuntimeError("recovery failed")

    monkeypatch.setattr(DatabaseInterpreter, "close", record_database)
    monkeypatch.setattr(ThumbnailService, "close", record_thumbnails)
    monkeypatch.setattr(LibraryMaintenanceService, "recover_pending_journal", fail_recovery)
    try:
        with pytest.raises(RuntimeError, match="recovery failed"):
            create_library_runtime(reader, AppConfig(), store.load(), store)
        assert closed == ["thumbnails", "database"]
    finally:
        reader.close()


def test_app_context_owns_one_reader_and_one_library_runtime(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    context = create_app_context()
    try:
        assert {field.name for field in fields(AppContext)}.isdisjoint(
            {"database_interpreter", "reader_session_service", "cache_service"}
        )
        assert context.database_interpreter is context.library_runtime.database_interpreter
        assert context.reader_session_service is context.reader_runtime.reader_session_service
        assert context.cache_service.reader_caches is context.reader_runtime.cache_service
        assert context.cache_service.library_caches is context.library_runtime.library_caches
    finally:
        context.close()


def test_storage_rebuild_commits_both_runtimes_and_keeps_viewmodels(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    context = create_app_context()
    old_reader = context.reader_runtime
    old_library = context.library_runtime
    shelf = context.shelf_viewmodel
    settings = context.settings_viewmodel
    cache = context.reader_runtime.cache_service.reader_page_cache
    try:
        context.reload_storage_from_settings()
        assert context.reader_runtime is not old_reader
        assert context.library_runtime is not old_library
        assert context.shelf_viewmodel is shelf
        assert context.settings_viewmodel is settings
        assert context.reader_runtime.cache_service.reader_page_cache is cache
        assert context.cache_service.reader_caches is context.reader_runtime.cache_service
    finally:
        context.close()


def test_storage_rebuild_preserves_saved_import_conversion_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    context = create_app_context()
    try:
        context.settings_viewmodel.set_canonical_import_policy("Never")
        assert context.import_service._canonical_import_policy is CanonicalImportPolicy.NEVER

        context.reload_storage_from_settings()

        assert context.import_service._canonical_import_policy is CanonicalImportPolicy.NEVER
        assert context.settings_store.load().canonical_import_policy == "never"
    finally:
        context.close()


def test_failed_storage_rebuild_does_not_publish_partial_runtime(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    context = create_app_context()
    old_reader = context.reader_runtime
    old_library = context.library_runtime
    old_paths = context.paths

    def fail_library(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("library construction failed")

    monkeypatch.setattr("joyread.app.app_context.create_library_runtime", fail_library)
    try:
        with pytest.raises(RuntimeError, match="library construction failed"):
            context.reload_storage_from_settings()
        assert context.reader_runtime is old_reader
        assert context.library_runtime is old_library
        assert context.paths is old_paths
    finally:
        context.close()


def test_failed_viewmodel_rebind_restores_old_services_before_closing_staged_runtime(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    context = create_app_context()
    old_reader = context.reader_runtime
    old_library = context.library_runtime
    tag_vm = context.tag_management_viewmodel
    original_replace = tag_vm.replace_service
    staged = []
    original_create = create_library_runtime

    def capture_runtime(*args, **kwargs):  # noqa: ANN002, ANN003
        runtime = original_create(*args, **kwargs)
        staged.append(runtime)
        return runtime

    def fail_after_rebind(service):  # noqa: ANN001
        original_replace(service)
        if service is not old_library.tag_service:
            raise RuntimeError("tag rebind failed")

    monkeypatch.setattr("joyread.app.app_context.create_library_runtime", capture_runtime)
    monkeypatch.setattr(tag_vm, "replace_service", fail_after_rebind)
    try:
        with pytest.raises(RuntimeError, match="tag rebind failed"):
            context.reload_storage_from_settings()

        assert context.reader_runtime is old_reader
        assert context.library_runtime is old_library
        assert context.shelf_viewmodel._library_service is old_library.library_service
        assert context.shelf_viewmodel._thumbnail_service is old_library.thumbnail_service
        assert context.settings_viewmodel._hidden_space_service is old_library.hidden_space_service
        assert tag_vm._service is old_library.tag_service
        assert context.book_repository.list_books() == []
        assert staged[0].database_interpreter._closed
    finally:
        context.close()


def test_failed_cache_strategy_rebuild_keeps_live_services(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    context = create_app_context()
    reader = context.reader_runtime
    library = context.library_runtime
    pool = context.archive_extraction_pool
    thumbnail = context.thumbnail_service
    context.settings_store.update(archive_cache_strategy="hidden_image_files")

    def fail_thumbnail(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("thumbnail rebuild failed")

    monkeypatch.setattr("joyread.app.app_context.ThumbnailService", fail_thumbnail)
    try:
        with pytest.raises(RuntimeError, match="thumbnail rebuild failed"):
            context.apply_cache_settings()
        assert context.reader_runtime is reader
        assert context.library_runtime is library
        assert context.archive_extraction_pool is pool
        assert context.thumbnail_service is thumbnail
        assert context.book_repository.list_books() == []
    finally:
        context.close()
