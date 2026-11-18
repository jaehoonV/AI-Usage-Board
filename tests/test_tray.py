"""The tray icon must come back after Explorer restarts."""
import ctypes
import os
import time

import pytest

from ai_usage_board.ui import win32

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows notification area only")


def test_icon_is_added_again_when_the_taskbar_comes_back(monkeypatch):
    calls = []
    # Recorded instead of shown: the test never puts a real icon in the tray.
    monkeypatch.setattr(ctypes.windll.shell32, "Shell_NotifyIconW", lambda message, _data: calls.append(message) or 1)

    tray = win32.TrayIcon("AI Usage Board test", None, on_activate=lambda: None)
    assert tray.start()
    try:
        assert calls == [win32.NIM_ADD]
        user32 = ctypes.windll.user32
        user32.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
        user32.RegisterWindowMessageW.restype = ctypes.c_uint
        taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        user32.PostMessageW(tray._hwnd, taskbar_created, 0, 0)

        deadline = time.monotonic() + 5
        while calls.count(win32.NIM_ADD) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert calls.count(win32.NIM_ADD) == 2
    finally:
        tray.stop()
        tray._thread.join(5)
    assert calls[-1] == win32.NIM_DELETE


def test_an_unchanged_tooltip_is_not_sent_again(monkeypatch):
    calls = []
    monkeypatch.setattr(ctypes.windll.shell32, "Shell_NotifyIconW", lambda message, _data: calls.append(message) or 1)
    tray = win32.TrayIcon("AI Usage Board", None, on_activate=lambda: None)
    # Registering the window class twice in one process fails, so the
    # message loop is not started here; a handle and data are enough.
    tray._hwnd, tray._nid = 1, type("Nid", (), {"szTip": ""})()
    monkeypatch.setattr(ctypes, "byref", lambda value: value)

    for _ in range(3):
        tray.set_tooltip("AI Usage Board\nCL 4%")
    tray.set_tooltip("AI Usage Board\nCL 5%")
    assert calls == [win32.NIM_MODIFY, win32.NIM_MODIFY]
    assert tray._nid.szTip == "AI Usage Board\nCL 5%"
