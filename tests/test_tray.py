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
