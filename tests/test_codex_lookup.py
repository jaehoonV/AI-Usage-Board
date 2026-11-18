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


#: Stands in for `codex app-server --stdio`: initialize works, the rate limit
#: read fails, and account/read answers with or without an account.
FAKE_SERVER = r"""
import json, os, sys
signed_in = os.environ["FAKE_CODEX_SIGNED_IN"] == "1"
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        reply = {"result": {}}
    elif method == "account/rateLimits/read":
        reply = {"error": {"code": -32603, "message": "backend unavailable"}}
    elif method == "account/read":
        reply = {"result": {"account": {"type": "chatgpt"} if signed_in else None}}
    else:
        reply = {"error": {"code": -32600, "message": "unknown method"}}
    print(json.dumps({"id": message["id"], **reply}), flush=True)
"""


@pytest.fixture
def fake_server(tmp_path, monkeypatch):
    import subprocess
    import sys
    from pathlib import Path

    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    real_popen = subprocess.Popen
    monkeypatch.setattr(codex, "find_codex", lambda: Path(sys.executable))
    monkeypatch.setattr(
        codex.subprocess, "Popen",
        lambda _command, **kwargs: real_popen([sys.executable, str(script)], **kwargs),
    )
    return monkeypatch


def test_an_rpc_error_while_signed_in_is_a_retryable_failure(fake_server):
    fake_server.setenv("FAKE_CODEX_SIGNED_IN", "1")
    with pytest.raises(codex.CodexError) as caught:
        codex.read_codex(timeout=20)
    assert not isinstance(caught.value, codex.CodexLoginRequired)
    assert caught.value.code == -32603


def test_an_rpc_error_with_no_account_asks_for_login(fake_server):
    fake_server.setenv("FAKE_CODEX_SIGNED_IN", "0")
    with pytest.raises(codex.CodexLoginRequired):
        codex.read_codex(timeout=20)


def test_shutdown_does_not_wait_on_a_silent_cli():
    """A CLI that ignores EOF and never writes must not hold shutdown hostage
    while the reader thread is blocked on its stdout."""
    import subprocess
    import sys
    import threading
    import time

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8",
    )
    codex._Channel(process, threading.Event())  # starts the blocked reader
    time.sleep(0.3)
    done = threading.Event()
    threading.Thread(target=lambda: (codex._shutdown(process), done.set()), daemon=True).start()
    try:
        assert done.wait(10), "_shutdown hung behind the reader thread"
        assert process.poll() is not None
    finally:
        process.kill()


def test_gui_binary_is_rejected_with_its_own_message(isolated, monkeypatch):
    gui = fake_exe(isolated / "app" / "codex.exe", subsystem=2)
    monkeypatch.setattr(codex.shutil, "which", lambda name: str(gui) if name == "codex" else None)
    with pytest.raises(codex.CodexNotInstalled, match="데스크톱 앱만"):
        codex.find_codex(refresh=True)
