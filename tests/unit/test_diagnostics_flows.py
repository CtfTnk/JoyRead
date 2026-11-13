"""Settings > About > Diagnostics dialog flows."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QWidget

from joyread.infrastructure.i18n.locale_service import t
from joyread.infrastructure.logging.log_files import LogClearResult, LogExportResult, LogFilesUnavailable
from joyread.ui.views import diagnostics_flows
from joyread.ui.views.diagnostics_flows import DiagnosticsFlows


class _Dialogs:
    def __init__(self) -> None:
        self.infos: list[tuple[str, str]] = []
        self.confirms: list[dict[str, object]] = []

    def show_info(self, title: str, message: str, button_text: str | None = None) -> None:
        self.infos.append((str(title), str(message)))

    def show_confirm(self, title, message, on_confirm, on_cancel=None, confirm_text=None, cancel_text=None, *, destructive=False, rich_text=False) -> None:  # noqa: ANN001
        self.confirms.append(
            {"title": str(title), "message": str(message), "on_confirm": on_confirm, "destructive": destructive}
        )


class _Tasks:
    """Run work inline, like the task service would on its worker."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def submit(self, name, callback, *, on_success=None, on_failure=None, **_kwargs):  # noqa: ANN001
        self.names.append(name)
        try:
            result = callback()
        except Exception as error:
            on_failure(error)
            return None
        on_success(result)
        return None


class _ViewModel:
    def __init__(self, directory: Path | None) -> None:
        self._directory = directory

    def log_directory(self) -> Path | None:
        return self._directory


def _flows(qtbot, directory: Path | None) -> tuple[DiagnosticsFlows, _Dialogs, _Tasks]:
    host = QWidget()
    qtbot.addWidget(host)
    dialogs = _Dialogs()
    tasks = _Tasks()
    return DiagnosticsFlows(dialogs, _ViewModel(directory), tasks, host), dialogs, tasks


def test_export_warns_about_paths_then_saves_a_zip(qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flows, dialogs, tasks = _flows(qtbot, tmp_path)
    chosen = tmp_path / "support-logs"
    monkeypatch.setattr(
        diagnostics_flows.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *_args: (str(chosen), "")),
    )
    exported: list[Path] = []

    def fake_export(destination: Path) -> LogExportResult:
        exported.append(destination)
        return LogExportResult(destination, 4, 1024)

    monkeypatch.setattr(diagnostics_flows, "export_logs", fake_export)

    flows.start_export()

    assert dialogs.confirms[0]["message"] == str(t("diagnostics.export_message"))
    dialogs.confirms[0]["on_confirm"]()
    assert exported == [tmp_path / "support-logs.zip"]
    assert tasks.names == ["export-logs"]
    assert dialogs.infos == [
        (
            str(t("diagnostics.export_done_title")),
            str(t("diagnostics.export_done_message", count="4", path=str(tmp_path / "support-logs.zip"))),
        )
    ]


def test_cancelled_save_dialog_exports_nothing(qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flows, dialogs, tasks = _flows(qtbot, tmp_path)
    monkeypatch.setattr(
        diagnostics_flows.QFileDialog, "getSaveFileName", staticmethod(lambda *_args: ("", ""))
    )

    flows.start_export()
    dialogs.confirms[0]["on_confirm"]()

    assert tasks.names == []
    assert dialogs.infos == []


def test_delete_is_destructive_and_reports_partial_failure(qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flows, dialogs, tasks = _flows(qtbot, tmp_path)
    monkeypatch.setattr(diagnostics_flows, "clear_logs", lambda: LogClearResult(3, 1))

    flows.start_delete()

    assert dialogs.confirms[0]["destructive"] is True
    dialogs.confirms[0]["on_confirm"]()
    assert tasks.names == ["delete-logs"]
    assert dialogs.infos[-1][1] == str(t("diagnostics.delete_partial_message", count="3", failed="1"))


def test_failures_and_missing_log_directory_are_explained(qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flows, dialogs, _tasks = _flows(qtbot, None)
    flows.start_export()
    flows.start_delete()
    flows.open_log_folder()
    assert dialogs.confirms == []
    assert [message for _title, message in dialogs.infos] == [str(t("diagnostics.unavailable"))] * 3

    flows, dialogs, _tasks = _flows(qtbot, tmp_path)

    def unavailable() -> LogClearResult:
        raise LogFilesUnavailable("gone")

    monkeypatch.setattr(diagnostics_flows, "clear_logs", unavailable)
    flows.start_delete()
    dialogs.confirms[0]["on_confirm"]()
    assert dialogs.infos[-1] == (str(t("diagnostics.delete_failed_title")), str(t("diagnostics.unavailable")))


def test_open_log_folder_reports_when_the_desktop_cannot_open_it(qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flows, dialogs, _tasks = _flows(qtbot, tmp_path)
    opened: list[str] = []

    def refuse(url) -> bool:  # noqa: ANN001
        opened.append(url.toLocalFile())
        return False

    monkeypatch.setattr(diagnostics_flows.QDesktopServices, "openUrl", staticmethod(refuse))

    flows.open_log_folder()

    assert opened == [str(tmp_path)]
    assert dialogs.infos[-1][1] == str(t("diagnostics.open_failed", path=str(tmp_path)))
