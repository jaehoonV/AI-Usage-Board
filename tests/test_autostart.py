import os
import sys
from pathlib import Path

import pytest

from ai_usage_board import autostart

windows_only = pytest.mark.skipif(os.name != "nt", reason="Startup shortcuts are a Windows thing")


def test_source_build_starts_without_a_console():
    target, arguments, working_dir, _icon = autostart.launch_command()
    assert Path(target).name.lower() in ("pythonw.exe", Path(sys.executable).name.lower())
    assert arguments.strip('"').endswith("run.py")
    assert Path(arguments.strip('"')).is_file()
    assert Path(working_dir) == Path(arguments.strip('"')).parent


def test_frozen_build_points_at_its_exe(monkeypatch, tmp_path):
    exe = tmp_path / "AIUsageBoard.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    target, arguments, working_dir, icon = autostart.launch_command()
    assert Path(target) == exe.resolve()
    assert arguments == ""
    assert Path(working_dir) == tmp_path.resolve()
    assert icon == ""  # the exe carries its own


@windows_only
def test_toggle_writes_and_removes_the_shortcut(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "startup_dir", lambda: tmp_path)
    assert autostart.is_enabled() is False

    assert autostart.set_enabled(True) is True
    shortcut = tmp_path / autostart.SHORTCUT_NAME
    # A real shell link starts with its 0x4C header size and the ShellLink CLSID.
    assert shortcut.read_bytes()[:4] == b"\x4c\x00\x00\x00"

    assert autostart.set_enabled(False) is False
    assert not shortcut.exists()
    # Turning off twice is harmless.
    assert autostart.set_enabled(False) is False


def test_state_comes_from_the_file_not_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "startup_dir", lambda: tmp_path)
    (tmp_path / autostart.SHORTCUT_NAME).write_bytes(b"")
    assert autostart.is_enabled() is True
