import socket
from pathlib import Path

import pytest

from ai_usage_board.providers import chrome_session
from ai_usage_board.providers.chrome_session import BrowserError, BrowserSession, browser_command

AWAKE = (
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
)


def features(command):
    flags = [flag for flag in command if flag.startswith("--disable-features=")]
    # Chrome honours only the last one, so there must be exactly one.
    assert len(flags) == 1
    return flags[0].split("=", 1)[1].split(",")


def test_reader_keeps_its_hidden_page_awake():
    command = browser_command(Path("chrome.exe"), Path("profile"), "https://claude.ai/settings/usage",
                              minimized=True, debug=True)
    assert all(flag in command for flag in AWAKE)
    assert {"Translate", "MediaRouter", "IntensiveWakeUpThrottling"} <= set(features(command))


def test_sign_in_window_stays_an_ordinary_browser():
    command = browser_command(Path("chrome.exe"), Path("profile"), "https://claude.ai/login",
                              minimized=False, debug=False)
    assert not any(flag in command for flag in AWAKE)
    assert not any(flag.startswith("--remote-debugging") for flag in command)
    assert "IntensiveWakeUpThrottling" not in features(command)


class FakeProcess:
    pid = 4242

    def __init__(self):
        self.running = True
        self.terminated = False

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.terminated = True
        self.running = False

    def kill(self):
        self.running = False

    def wait(self, timeout=None):
        return 0


def test_a_browser_that_never_got_ready_is_not_left_running(tmp_path, monkeypatch):
    process = FakeProcess()
    monkeypatch.setattr(chrome_session, "find_browser", lambda: Path("chrome.exe"))
    monkeypatch.setattr(chrome_session.subprocess, "Popen", lambda *_a, **_k: process)

    def never_ready(self, _port_file, _debug):
        raise BrowserError("BrowserStartTimeout")

    monkeypatch.setattr(BrowserSession, "_await_ready", never_ready)
    session = BrowserSession(tmp_path / "profile")
    with pytest.raises(BrowserError, match="BrowserStartTimeout"):
        session.start(minimized=True, landing="https://claude.ai/settings/usage")
    assert process.terminated


def test_a_page_that_never_answers_is_a_browser_error(monkeypatch):
    class Silent:
        def __init__(self, *_args, **_kwargs):
            pass

        def send_json(self, _value):
            pass

        def recv_json(self):
            raise socket.timeout("timed out")

        def close(self):
            pass

    monkeypatch.setattr(chrome_session, "WebSocket", Silent)
    session = BrowserSession(Path("profile"))
    session.port = 9222
    session.socket_url = "ws://127.0.0.1:9222/devtools/browser/x"
    with pytest.raises(BrowserError, match="BrowserReplyTimeout"):
        session.call("Runtime.evaluate")
