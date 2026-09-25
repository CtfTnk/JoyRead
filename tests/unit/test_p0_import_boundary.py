"""P0-only characterization of OS opens before the runtime split."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from joyread.app.app_context import create_app_context
from joyread.app.bootstrap import create_application
from joyread.core.services.import_service import ImportService
from joyread.infrastructure.config.settings_store import create_environment_settings_store
from joyread.infrastructure.logging.logging_service import shutdown_logging
from joyread.ui.views.reader_window import ReaderWindow


@pytest.fixture(autouse=True)
def _close_logging_runtime():
    yield
    shutdown_logging(timeout_seconds=2.0)


def test_os_document_open_never_imports_even_with_drop_preference_enabled(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path))
    # A real first-run Library must exist before adding the legacy setting;
    # an orphan settings file would trigger the recovery dialog instead.
    prepared = create_app_context()
    prepared.close()
    store = create_environment_settings_store()
    legacy = json.loads(store.settings_path.read_text(encoding="utf-8"))
    legacy["import_book_when_opening"] = True
    legacy["import_on_read_drop"] = True
    store.settings_path.write_text(json.dumps(legacy), encoding="utf-8")
    source = tmp_path / "external.cbz"
    source.write_bytes(b"")

    def reject_import(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("OS file requests must not reach ImportService")

    monkeypatch.setattr(ImportService, "import_files", reject_import)
    app, context, window = create_application(["joyread", str(source)])
    try:
        assert isinstance(window, ReaderWindow)
        app.processEvents()
    finally:
        window.close()
        context.close()
