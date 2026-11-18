import struct

import pytest

from ai_usage_board.providers import codex


def fake_exe(path, subsystem=3):
    """Smallest file is_console_executable will read as a PE image."""
    head = bytearray(1024)
    head[:2] = b"MZ"
    struct.pack_into("<I", head, 0x3C, 0x80)
    head[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", head, 0x80 + 24 + 68, subsystem)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(head))
    return path


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """No real PATH, npm or Store app: only what each test creates."""
    monkeypatch.setattr(codex, "_cached_path", None)
    monkeypatch.setattr(codex, "_appx_install_location", lambda _name: None)
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(codex.shutil, "which", lambda _name: None)
    return tmp_path


def test_finds_the_binary_behind_npms_cmd_shim(isolated, monkeypatch):
    prefix = isolated / "appdata" / "npm"
    shim = prefix / "codex.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("@node codex.js %*")
    monkeypatch.setattr(codex.shutil, "which", lambda name: str(shim) if name == "codex" else None)
    binary = fake_exe(
        prefix / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai" / "codex-win32-x64"
        / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
    )
    assert codex.find_codex(refresh=True) == binary


def test_finds_a_hoisted_platform_package_without_path(isolated):
    binary = fake_exe(
        isolated / "appdata" / "npm" / "node_modules" / "@openai" / "codex-win32-x64"
        / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
    )
    assert codex.find_codex(refresh=True) == binary


def test_a_shim_alone_is_not_mistaken_for_the_desktop_app(isolated, monkeypatch):
    shim = isolated / "bin" / "codex.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("@echo off")
    monkeypatch.setattr(codex.shutil, "which", lambda name: str(shim) if name == "codex" else None)
    with pytest.raises(codex.CodexNotInstalled, match="찾을 수 없습니다"):
        codex.find_codex(refresh=True)


def test_gui_binary_is_rejected_with_its_own_message(isolated, monkeypatch):
    gui = fake_exe(isolated / "app" / "codex.exe", subsystem=2)
    monkeypatch.setattr(codex.shutil, "which", lambda name: str(gui) if name == "codex" else None)
    with pytest.raises(codex.CodexNotInstalled, match="데스크톱 앱만"):
        codex.find_codex(refresh=True)
