"""Windows background settings, close notice, tray, and idle memory behavior."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject, QPoint, QTimer, Signal as QtSignal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QCheckBox, QMainWindow, QPushButton, QSystemTrayIcon

from joyread.app.windows import windows_background
from joyread.app.app_context import create_app_context
from joyread.app.windows.manager import ApplicationWindowManager
from joyread.core.services.cache_service import BoundedByteCache, SharedThumbnailCache, ThumbnailCacheKey
from joyread.infrastructure.config.settings_store import SettingsStore
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.dialogs.windows_background_notice import (
    EXIT_APPLICATION,
    WindowsBackgroundNoticeDialog,
)
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.settings_viewmodel import SettingsViewModel
from joyread.ui.widgets.windows_tray import WindowsTrayIcon


class _App(QObject):
    aboutToQuit = QtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.quit_on_close = True
        self.quit_calls = 0

    def quitOnLastWindowClosed(self) -> bool:
        return self.quit_on_close

    def setQuitOnLastWindowClosed(self, value: bool) -> None:
        self.quit_on_close = value

    def windowIcon(self) -> QIcon:
        return QIcon(str(ResourceLoader().app_icon_path()))

    def quit(self) -> None:
        self.quit_calls += 1
        self.aboutToQuit.emit()


class _Windows(QObject):
    windows_changed = QtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.has_windows = True
        self.guard = None
        self.opened: list[tuple[str, ...]] = []
        self.closed = 0
        self.reopened = 0
        self.libraries = 0

    def set_last_close_guard(self, guard) -> None:  # noqa: ANN001
        self.guard = guard

    def close_all_windows(self) -> None:
        self.closed += 1
        self.has_windows = False
        self.windows_changed.emit()

    def handle_reopen(self) -> None:
        self.reopened += 1

    def show_library(self) -> None:
        self.libraries += 1

    def open_files(self, paths) -> None:  # noqa: ANN001
        self.opened.append(tuple(paths))


class _Tray(QObject):
    restore_requested = QtSignal()
    open_library_requested = QtSignal()
    open_book_requested = QtSignal()
    cleanup_requested = QtSignal()
    memory_refresh_requested = QtSignal()
    quit_requested = QtSignal()

    def __init__(self, _icon, parent: QObject) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.visible = False
        self.statuses: list[str] = []
        self.memory_mb: int | None = None

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def isVisible(self) -> bool:
        return self.visible

    def set_cleanup_status(self, status: str) -> None:
        self.statuses.append(status)

    def set_memory_usage_mb(self, mb: int | None) -> None:
        self.memory_mb = mb

    def dispose(self) -> None:
        self.hide()


class _Cache:
    def __init__(self) -> None:
        self.clears = 0

    def clear(self) -> None:
        self.clears += 1


def _store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(
        support_root=tmp_path / "support",
        default_storage_root=tmp_path / "library",
    )


def _session(qtbot, monkeypatch, tmp_path: Path, *, pending: int = 0, library: bool = False):
    monkeypatch.setattr(windows_background, "WindowsTrayIcon", _Tray)
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    app = _App()
    windows = _Windows()
    store = _store(tmp_path)
    page, thumbnails, covers = _Cache(), _Cache(), _Cache()
    task = SimpleNamespace(pending_task_count=lambda: pending)
    reader = SimpleNamespace(
        task_service=task,
        cache_service=SimpleNamespace(reader_page_cache=page, thumbnail_cache=thumbnails),
    )
    thumbnail_service = SimpleNamespace(clear_idle_memory=lambda: None)
    context = (
        SimpleNamespace(
            reader_runtime=reader,
            library_runtime=SimpleNamespace(thumbnail_service=thumbnail_service),
            cache_service=SimpleNamespace(cover_index=covers),
        )
        if library else reader
    )
    session = windows_background.WindowsBackgroundSession(
        app, windows, store, lambda: context  # type: ignore[arg-type]
    )
    return app, windows, store, session, page, thumbnails, covers


def test_background_settings_default_on_and_persisted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    defaults = store.load()
    assert defaults.windows_background_enabled
    assert not defaults.windows_background_notice_suppressed
    assert defaults.windows_background_auto_cleanup_enabled
    assert defaults.windows_background_auto_cleanup_seconds == 60

    raw = json.loads(store.settings_path.read_text(encoding="utf-8"))
    raw.pop("windows_background_enabled")
    raw.pop("windows_background_notice_suppressed")
    raw.pop("windows_background_auto_cleanup_enabled")
    raw.pop("windows_background_auto_cleanup_seconds")
    store.settings_path.write_text(json.dumps(raw), encoding="utf-8")
    assert store.load().windows_background_enabled
    assert not store.load().windows_background_notice_suppressed
    assert store.load().windows_background_auto_cleanup_enabled
    assert store.load().windows_background_auto_cleanup_seconds == 60

    vm = SettingsViewModel(store.load(), store)
    changes: list[bool] = []
    vm.windows_background_changed.connect(changes.append)
    vm.set_windows_background_enabled(False)
    assert changes == [False]
    assert not store.load().windows_background_enabled
    store.update(windows_background_notice_suppressed=True)
    vm.set_windows_background_enabled(True)
    assert changes == [False, True]
    restored = store.load()
    assert restored.windows_background_enabled
    assert not restored.windows_background_notice_suppressed


def test_auto_cleanup_preferences_persist_and_clamp_seconds(tmp_path: Path) -> None:
    store = _store(tmp_path)
    vm = SettingsViewModel(store.load(), store)
    changes: list[tuple[bool, int]] = []
    vm.windows_background_cleanup_changed.connect(changes.append)

    vm.set_windows_background_auto_cleanup_enabled(False)
    vm.set_windows_background_auto_cleanup_seconds(90)
    assert changes == [(False, 60), (False, 90)]
    saved = store.load()
    assert not saved.windows_background_auto_cleanup_enabled
    assert saved.windows_background_auto_cleanup_seconds == 90

    store.update(windows_background_auto_cleanup_seconds=-5)
    assert store.load().windows_background_auto_cleanup_seconds == 1
    store.update(windows_background_auto_cleanup_seconds=99999)
    assert store.load().windows_background_auto_cleanup_seconds == 3600
    store.update(windows_background_auto_cleanup_seconds="invalid")
    assert store.load().windows_background_auto_cleanup_seconds == 60


def test_notice_checkbox_is_above_confirmation_button(qtbot) -> None:
    parent = QMainWindow()
    qtbot.addWidget(parent)
    dialog = WindowsBackgroundNoticeDialog(parent)
    dialog.show()
    qtbot.wait(0)
    box = dialog.findChild(QCheckBox, "WindowsBackgroundDontShowAgain")
    button = dialog.findChild(QPushButton, "WindowsBackgroundConfirm")
    exit_button = dialog.findChild(QPushButton, "WindowsBackgroundQuit")
    assert box is not None and button is not None and exit_button is not None
    assert box.geometry().bottom() < button.geometry().top()
    assert box.geometry().bottom() < exit_button.geometry().top()
    assert exit_button.geometry().right() < button.geometry().left()
    dialog.reject()


def test_notice_exit_button_returns_distinct_choice(qtbot) -> None:
    parent = QMainWindow()
    qtbot.addWidget(parent)
    dialog = WindowsBackgroundNoticeDialog(parent)
    dialog.show()
    qtbot.wait(0)
    dialog.findChild(QPushButton, "WindowsBackgroundQuit").click()
    assert dialog.result() == EXIT_APPLICATION


def test_notice_rejects_close_and_confirm_persists_suppression(qtbot, monkeypatch, tmp_path: Path) -> None:
    app, windows, store, session, *_ = _session(qtbot, monkeypatch, tmp_path)
    responses = [(0, False), (1, True)]

    class _Notice:
        def __init__(self, _parent) -> None:  # noqa: ANN001
            self.response, checked = responses.pop(0)
            self.dont_show_again = SimpleNamespace(isChecked=lambda: checked)

        def exec(self) -> int:
            return self.response

        def deleteLater(self) -> None:
            pass

    monkeypatch.setattr(windows_background, "WindowsBackgroundNoticeDialog", _Notice)
    try:
        assert not windows.guard(QMainWindow())
        assert not store.load().windows_background_notice_suppressed
        assert windows.guard(QMainWindow())
        assert store.load().windows_background_notice_suppressed
        assert windows.guard(QMainWindow())
        assert responses == []
    finally:
        session.dispose()


def test_notice_exit_quits_without_saving_suppression(qtbot, monkeypatch, tmp_path: Path) -> None:
    app, windows, store, session, *_ = _session(qtbot, monkeypatch, tmp_path)

    class _Notice:
        def __init__(self, _parent) -> None:  # noqa: ANN001
            self.dont_show_again = SimpleNamespace(isChecked=lambda: True)

        def exec(self) -> int:
            return EXIT_APPLICATION

        def deleteLater(self) -> None:
            pass

    monkeypatch.setattr(windows_background, "WindowsBackgroundNoticeDialog", _Notice)
    try:
        assert windows.guard(QMainWindow())
        assert session._exiting
        assert windows.closed == 0  # The guarded close must finish first.
        assert not store.load().windows_background_notice_suppressed
        qtbot.waitUntil(lambda: app.quit_calls == 1)
        assert windows.closed == 1
        assert windows.guard is None
        assert not session.tray.isVisible()
    finally:
        session.dispose()


def test_notice_exit_closes_real_last_window_without_reentering_guard(
    qtbot, monkeypatch, tmp_path: Path
) -> None:
    qt_app = QApplication.instance()
    assert qt_app is not None
    previous_quit_on_close = qt_app.quitOnLastWindowClosed()
    qt_app.setQuitOnLastWindowClosed(False)
    monkeypatch.setattr(windows_background, "WindowsTrayIcon", _Tray)
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    store = _store(tmp_path)
    context = create_app_context(settings_store=store)
    manager = ApplicationWindowManager(context)
    app = _App()
    session = windows_background.WindowsBackgroundSession(app, manager, store, lambda: context)
    main = manager.show_library()
    try:
        QTimer.singleShot(
            0,
            lambda: main.findChild(WindowsBackgroundNoticeDialog)
            .findChild(QPushButton, "WindowsBackgroundQuit")
            .click(),
        )
        assert main.close()
        qtbot.waitUntil(lambda: app.quit_calls == 1)
        assert not manager.has_windows
        assert not store.load().windows_background_notice_suppressed
    finally:
        session.dispose()
        manager.close_all_windows()
        context.close()
        qt_app.setQuitOnLastWindowClosed(previous_quit_on_close)


def test_setting_switch_immediately_changes_residency(qtbot, monkeypatch, tmp_path: Path) -> None:
    app, windows, store, session, *_ = _session(qtbot, monkeypatch, tmp_path)
    context = SimpleNamespace(settings_viewmodel=SettingsViewModel(store.load(), store))
    session._context_provider = lambda: context
    session._bind_settings_viewmodel()
    try:
        assert session.tray.isVisible()
        assert not app.quit_on_close
        context.settings_viewmodel.set_windows_background_enabled(False)
        assert not session.tray.isVisible()
        assert app.quit_on_close
        context.settings_viewmodel.set_windows_background_enabled(True)
        assert session.tray.isVisible()
        assert not app.quit_on_close
    finally:
        session.dispose()


def test_reenabling_background_shows_the_close_notice_again(qtbot, monkeypatch, tmp_path: Path) -> None:
    _app, windows, store, session, *_ = _session(qtbot, monkeypatch, tmp_path)
    prompts: list[bool] = []

    class _Notice:
        def __init__(self, _parent) -> None:  # noqa: ANN001
            self.checked = not prompts
            self.dont_show_again = SimpleNamespace(isChecked=lambda: self.checked)

        def exec(self) -> int:
            prompts.append(self.checked)
            return 1

        def deleteLater(self) -> None:
            pass

    monkeypatch.setattr(windows_background, "WindowsBackgroundNoticeDialog", _Notice)
    try:
        assert windows.guard(QMainWindow())
        assert prompts == [True]
        assert store.load().windows_background_notice_suppressed

        context = SimpleNamespace(settings_viewmodel=SettingsViewModel(store.load(), store))
        session._context_provider = lambda: context
        session._bind_settings_viewmodel()
        context.settings_viewmodel.set_windows_background_enabled(False)
        assert store.load().windows_background_notice_suppressed
        context.settings_viewmodel.set_windows_background_enabled(True)
        assert not store.load().windows_background_notice_suppressed

        assert windows.guard(QMainWindow())
        assert prompts == [True, False]
    finally:
        session.dispose()


def test_unavailable_tray_falls_back_to_normal_exit(qtbot, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    app = _App()
    windows = _Windows()
    session = windows_background.WindowsBackgroundSession(
        app, windows, _store(tmp_path), lambda: SimpleNamespace()  # type: ignore[arg-type]
    )
    try:
        assert session.tray is None
        assert app.quit_on_close
        assert windows.guard(QMainWindow())
    finally:
        session.dispose()


def test_manual_cleanup_waits_for_windows_and_tasks_then_clears_memory(
    qtbot, monkeypatch, tmp_path: Path
) -> None:
    app, windows, _store_, session, page, thumbnails, covers = _session(
        qtbot, monkeypatch, tmp_path, pending=1, library=True
    )
    reader = session._reader_runtime()
    try:
        session.tray.cleanup_requested.emit()
        assert session.tray.statuses[-1] == "wait_windows"
        windows.has_windows = False
        windows.windows_changed.emit()
        qtbot.waitUntil(lambda: session.tray.statuses[-1] == "wait_tasks")
        reader.task_service.pending_task_count = lambda: 0
        session._task_poll.timeout.emit()
        qtbot.waitUntil(lambda: session.tray.statuses[-1] == "done")
        assert (page.clears, thumbnails.clears, covers.clears) == (1, 1, 1)
        assert app.quit_calls == 0
    finally:
        session.dispose()


def test_automatic_cleanup_waits_60_seconds_and_new_window_cancels_it(
    qtbot, monkeypatch, tmp_path: Path
) -> None:
    app, windows, _store_, session, page, thumbnails, _covers = _session(
        qtbot, monkeypatch, tmp_path
    )
    try:
        windows.has_windows = False
        windows.windows_changed.emit()
        assert session._idle_timer.isActive()
        assert session._idle_timer.interval() == 60_000
        windows.has_windows = True
        windows.windows_changed.emit()
        assert not session._idle_timer.isActive()
        assert (page.clears, thumbnails.clears) == (0, 0)

        windows.has_windows = False
        windows.windows_changed.emit()
        session._idle_timer.stop()
        session._on_idle_timeout()
        qtbot.waitUntil(lambda: page.clears == 1)
        assert thumbnails.clears == 1
        assert not session._idle_timer.isActive()
        assert app.quit_calls == 0
    finally:
        session.dispose()


def test_auto_cleanup_toggle_and_seconds_reconfigure_idle_wait(
    qtbot, monkeypatch, tmp_path: Path
) -> None:
    _app, windows, store, session, pages, thumbnails, _covers = _session(
        qtbot, monkeypatch, tmp_path
    )
    reader = session._reader_runtime()
    context = SimpleNamespace(
        settings_viewmodel=SettingsViewModel(store.load(), store), reader_runtime=reader
    )
    session._context_provider = lambda: context
    session._bind_settings_viewmodel()
    vm = context.settings_viewmodel
    try:
        windows.has_windows = False
        windows.windows_changed.emit()
        assert session._idle_timer.interval() == 60_000

        vm.set_windows_background_auto_cleanup_seconds(3)
        assert session._idle_timer.isActive()
        assert session._idle_timer.interval() == 3_000

        vm.set_windows_background_auto_cleanup_enabled(False)
        assert not session._idle_timer.isActive()
        session._on_idle_timeout()  # A stale timeout cannot re-arm cleanup.
        assert not session._auto_due
        assert (pages.clears, thumbnails.clears) == (0, 0)

        session.request_cleanup()
        qtbot.waitUntil(lambda: pages.clears == 1)
        assert thumbnails.clears == 1
        assert not session._idle_timer.isActive()

        vm.set_windows_background_auto_cleanup_enabled(True)
        assert session._idle_timer.isActive()
        assert session._idle_timer.interval() == 3_000
    finally:
        session.dispose()


def test_disabling_auto_cleanup_cancels_task_wait_without_cancelling_manual_wait(
    qtbot, monkeypatch, tmp_path: Path
) -> None:
    _app, windows, store, session, pages, _thumbnails, _covers = _session(
        qtbot, monkeypatch, tmp_path, pending=1
    )
    reader = session._reader_runtime()
    context = SimpleNamespace(
        settings_viewmodel=SettingsViewModel(store.load(), store), reader_runtime=reader
    )
    session._context_provider = lambda: context
    session._bind_settings_viewmodel()
    try:
        windows.has_windows = False
        windows.windows_changed.emit()
        session._idle_timer.stop()
        session._on_idle_timeout()
        assert session._auto_due and session._task_poll.isActive()

        context.settings_viewmodel.set_windows_background_auto_cleanup_enabled(False)
        assert not session._auto_due
        assert not session._task_poll.isActive()
        assert pages.clears == 0

        session.request_cleanup()
        assert session._manual_pending and session._task_poll.isActive()
        session._reader_runtime().task_service.pending_task_count = lambda: 0
        session._task_poll.timeout.emit()
        qtbot.waitUntil(lambda: pages.clears == 1)
    finally:
        session.dispose()


def test_idle_cleanup_empties_real_rebuildable_memory_caches(qtbot, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(windows_background, "WindowsTrayIcon", _Tray)
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    app, windows, store = _App(), _Windows(), _store(tmp_path)
    pages = BoundedByteCache(4096)
    thumbnails = SharedThumbnailCache(4096)
    covers = BoundedByteCache(4096)
    pages.put(("reader", 0), b"page")
    thumbnails.put(ThumbnailCacheKey("reader", 0, 80, 120), b"thumbnail")
    covers.put("book", "cover.png")
    reader = SimpleNamespace(
        task_service=SimpleNamespace(pending_task_count=lambda: 0),
        cache_service=SimpleNamespace(reader_page_cache=pages, thumbnail_cache=thumbnails),
    )
    context = SimpleNamespace(
        reader_runtime=reader,
        library_runtime=SimpleNamespace(thumbnail_service=SimpleNamespace(clear_idle_memory=lambda: None)),
        cache_service=SimpleNamespace(cover_index=covers),
    )
    session = windows_background.WindowsBackgroundSession(
        app, windows, store, lambda: context  # type: ignore[arg-type]
    )
    try:
        windows.has_windows = False
        windows.windows_changed.emit()
        session._idle_timer.stop()
        session._on_idle_timeout()
        qtbot.waitUntil(lambda: not session._cleaning)
        assert (len(pages), len(thumbnails), len(covers)) == (0, 0, 0)
        assert (pages.current_bytes, thumbnails.current_bytes, covers.current_bytes) == (0, 0, 0)
    finally:
        session.dispose()


def test_explicit_tray_quit_closes_windows_without_notice(qtbot, monkeypatch, tmp_path: Path) -> None:
    app, windows, _store_, session, *_ = _session(qtbot, monkeypatch, tmp_path)
    try:
        tray = session.tray
        assert tray is not None
        tray.quit_requested.emit()
        assert windows.closed == 1
        assert app.quit_calls == 1
        assert windows.guard is None
        assert app.quit_on_close
        assert not tray.visible
    finally:
        session.dispose()


def test_tray_book_picker_routes_read_only_and_cancel_keeps_residency(
    qtbot, monkeypatch, tmp_path: Path
) -> None:
    app, windows, _store_, session, *_ = _session(qtbot, monkeypatch, tmp_path)
    selected = iter([([str(tmp_path / "one.cbz"), str(tmp_path / "two.pdf")], ""), ([], "")])
    monkeypatch.setattr(windows_background.QFileDialog, "getOpenFileNames", lambda *_: next(selected))
    try:
        session.tray.open_book_requested.emit()
        session.tray.open_book_requested.emit()
        assert windows.opened == [(str(tmp_path / "one.cbz"), str(tmp_path / "two.pdf"))]
        assert app.quit_calls == 0
    finally:
        session.dispose()


def test_tray_actions_emit_commands_without_context_click_restoring(qtbot) -> None:
    app = QApplication.instance()
    assert app is not None
    tray = WindowsTrayIcon(QIcon(str(ResourceLoader().app_icon_path())), app)
    commands: list[str] = []
    tray.restore_requested.connect(lambda: commands.append("restore"))
    tray.open_library_requested.connect(lambda: commands.append("library"))
    tray.open_book_requested.connect(lambda: commands.append("book"))
    tray.cleanup_requested.connect(lambda: commands.append("cleanup"))
    tray.quit_requested.connect(lambda: commands.append("quit"))
    tray.memory_refresh_requested.connect(lambda: commands.append("memory"))
    try:
        tray.open_library_action.trigger()
        tray.open_book_action.trigger()
        tray.cleanup_action.trigger()
        tray.quit_action.trigger()
        tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
        tray.activated.emit(QSystemTrayIcon.ActivationReason.Context)
        assert commands == ["library", "book", "cleanup", "quit", "restore"]
        tray._menu.aboutToShow.emit()
        assert commands[-1] == "memory"
        tray.set_memory_usage_mb(321)
        assert "321 MB" in tray.memory_action.text()
        assert tray.memory_action.font().pixelSize() == Theme.menu_font_size - 1
        assert not tray.memory_action.isEnabled()
        tray.set_cleanup_status("cleaning")
        assert not tray.cleanup_action.isEnabled()
        tray.set_cleanup_status("ready")
        assert tray.cleanup_action.isEnabled()
    finally:
        tray.dispose()


def test_tray_menu_reserves_right_padding_for_action_text(qtbot) -> None:
    app = QApplication.instance()
    assert app is not None
    previous_style = app.styleSheet()
    resources = ResourceLoader()
    app.setStyleSheet(resources.load_stylesheet())
    tray = WindowsTrayIcon(QIcon(str(resources.app_icon_path())), app)
    menu = tray.contextMenu()
    assert menu is not None
    try:
        tray.open_book_action.setText("Open a fairly long comic filename")
        natural_width = menu.sizeHint().width()
        menu.popup(QPoint(0, 0))
        qtbot.wait(0)
        action_rect = menu.actionGeometry(tray.open_book_action)
        text_width = menu.fontMetrics().horizontalAdvance(tray.open_book_action.text())
        assert menu.width() >= natural_width + Theme.tray_menu_item_right_padding
        assert action_rect.width() - text_width >= Theme.tray_menu_item_right_padding
        assert menu.width() - action_rect.right() <= 2  # Selection reaches the border.
        first_width = menu.width()
        menu.hide()
        menu.popup(QPoint(0, 0))
        qtbot.wait(0)
        assert menu.width() == first_width  # Reopening must not add another 16 px.
        menu.hide()
        tray.open_book_action.setText(
            "Open a fairly long comic filename with a much longer translated label"
        )
        menu.popup(QPoint(0, 0))
        qtbot.wait(0)
        assert menu.width() > first_width
        expanded_width = menu.width()
        menu.hide()
        menu.popup(QPoint(0, 0))
        qtbot.wait(0)
        assert menu.width() == expanded_width
    finally:
        menu.hide()
        tray.dispose()
        app.setStyleSheet(previous_style)


def test_tray_memory_refresh_uses_current_process_working_set(qtbot, monkeypatch, tmp_path: Path) -> None:
    _app, _windows, _store_, session, *_ = _session(qtbot, monkeypatch, tmp_path)
    monkeypatch.setattr(windows_background, "current_process_working_set_mb", lambda: 246)
    try:
        session.tray.memory_refresh_requested.emit()
        assert session.tray.memory_mb == 246
    finally:
        session.dispose()
