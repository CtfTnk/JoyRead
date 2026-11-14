"""Settings > About > Diagnostics dialog flows."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QWidget

from joyread.infrastructure.i18n.locale_service import t
from joyread.infrastructure.logging.log_files import (
    CrashReportResult,
    LogClearResult,
    LogFilesUnavailable,
    NoCrashRecorded,
)
from joyread.infrastructure.logging.run_files import KEPT_RUN_COUNT
from joyread.ui.views import diagnostics_flows
from joyread.ui.views.diagnostics_flows import DiagnosticsFlows


class _Dialogs:
    def __init__(self) -> None:
        self.infos: list[tuple[str, str]] = []
        self.confirms: list[dict[str, object]] = []
        self.visible = False

    def isVisible(self) -> bool:  # noqa: N802 - Qt name.
        return self.visible

    def show_info(self, title: str, message: str, button_text: str | None = None) -> None:
        self.infos.append((str(title), str(message)))

    def show_confirm(self, title, message, on_confirm, on_cancel=None, confirm_text=None, cancel_text=None, *, destructive=False, rich_text=False) -> None:  # noqa: ANN001
        self.confirms.append(
            {
                "title": str(title),
                "message": str(message),
                "on_confirm": on_confirm,
                "cancel_text": None if cancel_text is None else str(cancel_text),
                "destructive": destructive,
            }
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
    def __init__(self, directory: Path | None, crashes: tuple[str, ...] = ()) -> None:
        self._directory = directory
        self._crashes = crashes

    def log_directory(self) -> Path | None:
        return self._directory

    def has_unreported_crashes(self) -> bool:
        return bool(self._crashes)

    def take_unreported_crashes(self) -> tuple[str, ...]:
        crashes, self._crashes = self._crashes, ()
        return crashes


def _flows(
    qtbot, directory: Path | None, *, crashes: tuple[str, ...] = ()
) -> tuple[DiagnosticsFlows, _Dialogs, _Tasks]:
    host = QWidget()
    qtbot.addWidget(host)
    dialogs = _Dialogs()
    tasks = _Tasks()
    return DiagnosticsFlows(dialogs, _ViewModel(directory, crashes), tasks, host), dialogs, tasks


def test_save_crash_report_warns_then_saves_to_the_desktop(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flows, dialogs, tasks = _flows(qtbot, tmp_path)
    desktop = tmp_path / "Desktop"
    saved_to: list[Path] = []
    monkeypatch.setattr(diagnostics_flows, "recorded_crashes", lambda: ["a crash"])
    monkeypatch.setattr(diagnostics_flows, "desktop_directory", lambda: desktop)

    def fake_save(directory: Path) -> CrashReportResult:
        saved_to.append(directory)
        return CrashReportResult(directory / "JoyRead-crash-report.zip", 1, 2)

    monkeypatch.setattr(diagnostics_flows, "save_crash_report", fake_save)

    flows.start_crash_report()

    assert dialogs.confirms[0]["message"] == str(t("diagnostics.crash_save_message"))
    assert tasks.names == []
    dialogs.confirms[0]["on_confirm"]()
    assert saved_to == [desktop]
    assert tasks.names == ["save-crash-report"]
    assert dialogs.infos == [
        (
            str(t("diagnostics.crash_saved_title")),
            str(t("diagnostics.crash_saved_message", path=str(desktop / "JoyRead-crash-report.zip"))),
        )
    ]


def test_without_a_crash_settings_says_so_and_saves_nothing(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flows, dialogs, tasks = _flows(qtbot, tmp_path)
    monkeypatch.setattr(diagnostics_flows, "recorded_crashes", lambda: [])

    flows.start_crash_report()

    assert dialogs.confirms == []
    assert tasks.names == []
    assert dialogs.infos == [
        (
            str(t("diagnostics.no_crash_title")),
            str(t("diagnostics.no_crash_message", count=str(KEPT_RUN_COUNT))),
        )
    ]


def test_a_detected_crash_is_offered_once(qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flows, dialogs, tasks = _flows(qtbot, tmp_path, crashes=("joyread-20260101-090000-1",))
    monkeypatch.setattr(
        diagnostics_flows,
        "save_crash_report",
        lambda directory: CrashReportResult(directory / "report.zip", 1, 2),
    )

    flows.offer_crash_report()
    flows.offer_crash_report()

    assert len(dialogs.confirms) == 1
    assert dialogs.confirms[0]["title"] == str(t("diagnostics.crashed_title"))
    assert dialogs.confirms[0]["cancel_text"] == str(t("diagnostics.btn_not_now"))
    dialogs.confirms[0]["on_confirm"]()
    assert tasks.names == ["save-crash-report"]


def test_no_offer_without_a_detected_crash(qtbot, tmp_path: Path) -> None:
    flows, dialogs, _tasks = _flows(qtbot, tmp_path)

    flows.offer_crash_report()

    assert dialogs.confirms == []
    assert dialogs.infos == []


def test_the_offer_waits_for_another_open_dialog(qtbot, tmp_path: Path) -> None:
    flows, dialogs, _tasks = _flows(qtbot, tmp_path, crashes=("joyread-20260101-090000-1",))
    dialogs.visible = True

    flows.offer_crash_report()
    assert dialogs.confirms == []

    dialogs.visible = False
    qtbot.waitUntil(lambda: len(dialogs.confirms) == 1, timeout=3000)
    assert dialogs.confirms[0]["title"] == str(t("diagnostics.crashed_title"))


def test_a_crash_deleted_before_saving_is_explained(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flows, dialogs, _tasks = _flows(qtbot, tmp_path)
    monkeypatch.setattr(diagnostics_flows, "recorded_crashes", lambda: ["a crash"])

    def gone(_directory: Path) -> CrashReportResult:
        raise NoCrashRecorded("deleted meanwhile")

    monkeypatch.setattr(diagnostics_flows, "save_crash_report", gone)

    flows.start_crash_report()
    dialogs.confirms[0]["on_confirm"]()

    assert dialogs.infos[-1] == (
        str(t("diagnostics.crash_failed_title")),
        str(t("diagnostics.no_crash_message", count=str(KEPT_RUN_COUNT))),
    )


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
    flows.start_crash_report()
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

    # QUrl.toLocalFile() uses forward slashes on Windows; compare paths, not text.
    assert [Path(path) for path in opened] == [tmp_path]
    assert dialogs.infos[-1][1] == str(t("diagnostics.open_failed", path=str(tmp_path)))
