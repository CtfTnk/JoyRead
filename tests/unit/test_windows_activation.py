from ctypes import wintypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMainWindow

from joyread.infrastructure import windows_activation as native
from joyread.app.launch import single_instance_broker as broker_module
from joyread.app.launch.intent import LaunchIntent, decode_launch_intent
from joyread.app.windows.manager import activate_window


def test_user32_uses_pointer_sized_window_handle(monkeypatch):
    api = SimpleNamespace(AllowSetForegroundWindow=Mock(), SetForegroundWindow=Mock())
    loader = Mock(return_value=api)
    monkeypatch.setattr(native.ctypes, "WinDLL", loader, raising=False)
    native._user32.cache_clear()
    try:
        assert native._user32() is api
        assert api.SetForegroundWindow.argtypes == (wintypes.HWND,)
        assert api.AllowSetForegroundWindow.argtypes == (wintypes.DWORD,)
        assert api.SetForegroundWindow.restype == wintypes.BOOL
        loader.assert_called_once_with("user32", use_last_error=True)
    finally:
        native._user32.cache_clear()


@pytest.mark.parametrize("result", [0, 1])
def test_native_requests_preserve_pid_and_win64_handle(monkeypatch, result):
    api = SimpleNamespace(AllowSetForegroundWindow=Mock(return_value=result),
                          SetForegroundWindow=Mock(return_value=result))
    monkeypatch.setattr(native, "IS_WINDOWS", True)
    monkeypatch.setattr(native, "_user32", lambda: api)
    assert native.allow_foreground_process(1234) is bool(result)
    handle = 0x123456789ABC
    assert native.request_foreground_window(handle) is bool(result)
    api.AllowSetForegroundWindow.assert_called_once_with(1234)
    api.SetForegroundWindow.assert_called_once_with(handle)


def test_non_windows_does_not_load_native_api(monkeypatch):
    monkeypatch.setattr(native, "IS_WINDOWS", False)
    loader = Mock(side_effect=AssertionError("must not load user32"))
    monkeypatch.setattr(native, "_user32", loader)
    assert not native.allow_foreground_process(1234)
    assert not native.request_foreground_window(1234)
    loader.assert_not_called()


def test_unavailable_api_does_not_prevent_opening(monkeypatch):
    monkeypatch.setattr(native, "IS_WINDOWS", True)
    monkeypatch.setattr(native, "_user32", Mock(side_effect=OSError("unavailable")))
    assert not native.allow_foreground_process(1234)
    assert not native.request_foreground_window(1234)


@pytest.mark.parametrize("pid", [0, -1, 0xFFFFFFFF])
def test_unknown_pid_never_grants_all_processes(monkeypatch, pid):
    monkeypatch.setattr(native, "IS_WINDOWS", True)
    api = Mock()
    monkeypatch.setattr(native, "_user32", lambda: api)
    assert not native.allow_foreground_process(pid)
    api.AllowSetForegroundWindow.assert_not_called()


@pytest.mark.parametrize("pid,allowed", [(4567, True), (4567, False), (0, False)])
def test_secondary_grants_primary_before_sending_launch(monkeypatch, tmp_path, pid, allowed):
    events = []
    intent = LaunchIntent.open_files((tmp_path / "comic.cbz",))
    broker = broker_module.SingleInstanceBroker(tmp_path)
    broker._lock = SimpleNamespace(getLockInfo=lambda: (pid, "host", "JoyRead"))
    class Socket:
        def connectToServer(self, name):
            events.append("connect")
        def waitForConnected(self, timeout):
            return True
        def write(self, message):
            assert decode_launch_intent(message.rstrip(b"\n")) == intent
            events.append("send")
            return len(message)
        def waitForBytesWritten(self, timeout):
            return True
        def disconnectFromServer(self):
            events.append("disconnect")
    def grant(process_id):
        assert process_id == pid
        events.append("grant")
        return allowed
    monkeypatch.setattr(broker_module, "QLocalSocket", Socket)
    monkeypatch.setattr(native, "IS_WINDOWS", True)
    monkeypatch.setattr(native, "allow_foreground_process", grant)
    broker._forward_to_primary(intent)
    assert events == (["connect", "grant", "send", "disconnect"] if pid else ["connect", "send", "disconnect"])
    broker._lock = None
    broker.dispose()


@pytest.mark.parametrize("state", [Qt.WindowState.WindowNoState,
                                   Qt.WindowState.WindowMinimized,
                                   Qt.WindowState.WindowMaximized | Qt.WindowState.WindowMinimized])
def test_activation_shows_and_restores_before_native_request(qtbot, monkeypatch, state):
    window = QMainWindow()
    qtbot.addWidget(window)
    window.resize(700, 500)
    window.setWindowState(state)
    requests = []
    def activate(handle):
        assert handle == int(window.winId())
        assert window.isVisible() and not window.isMinimized()
        requests.append(handle)
        return True
    monkeypatch.setattr(native, "IS_WINDOWS", True)
    monkeypatch.setattr(native, "request_foreground_window", activate)
    activate_window(window)
    assert requests == [int(window.winId())]
    assert not window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    if state & Qt.WindowState.WindowMaximized:
        assert window.isMaximized()


def test_library_new_and_reused_readers_request_activation(qtbot, tmp_path, monkeypatch):
    from tests.unit.test_application_window_manager import _manager, _close_windows
    monkeypatch.setattr(native, "IS_WINDOWS", True)
    requests = []
    monkeypatch.setattr(native, "request_foreground_window", lambda hwnd: requests.append(hwnd))
    manager, _mains, _readers, _shelf = _manager()
    try:
        library = manager.show_library()
        reader = manager.open_files((tmp_path / "comic.cbz",))[0]
        assert manager.open_files((tmp_path / "comic.cbz",))[0] is reader
        assert manager.show_library() is library
        qtbot.wait(10)
        assert requests == [int(library.winId()), int(reader.winId()), int(reader.winId()), int(library.winId())]
    finally:
        _close_windows(manager, qtbot)
